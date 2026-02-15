from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Tuple

from tm.governance import GovernanceDecision, GovernanceManager, RequestDescriptor
from tm.governance.hitl import PendingApproval
from tm.guard import GuardBlockedError

from .correlate import CorrelationHub
from .flow import Flow
from .operations import Operation, ResponseMode
from .policies import FlowPolicies
from .spec import FlowSpec, StepDef
from .trace_store import FlowTraceSink, TraceSpanLike
from tm.obs.recorder import Recorder

logger = logging.getLogger(__name__)


@dataclass
class _Request:
    run_id: str
    flow_name: str
    flow_id: str
    flow_rev: str
    spec: FlowSpec
    inputs: Dict[str, Any]
    response_mode: ResponseMode
    ctx: Dict[str, Any]
    model_name: Optional[str]
    future: asyncio.Future
    enqueue_ts: float
    governance_decision: Optional[GovernanceDecision] = None
    governance_descriptor: Optional[RequestDescriptor] = None


@dataclass
class FlowRunRecord:
    """Normalized run completion payload."""

    flow: str
    flow_id: str
    flow_rev: str
    run_id: str
    selected_flow: str
    binding: Optional[str]
    status: str
    outcome: Optional[str]
    queued_ms: float
    exec_ms: float
    duration_ms: float
    start_ts: float
    end_ts: float
    cost_usd: Optional[float]
    user_rating: Optional[float]
    reward: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


class FlowRuntime:
    """Async-first runtime with bounded concurrency and idempotency support."""

    def __init__(
        self,
        flows: Mapping[str, Flow] | None = None,
        *,
        policies: FlowPolicies | None = None,
        correlator: CorrelationHub | None = None,
        trace_sink: FlowTraceSink | None = None,
        max_concurrency: int = 8,
        queue_capacity: int = 1024,
        queue_wait_timeout_ms: int = 0,
        idempotency_ttl_sec: float = 30.0,
        idempotency_cache_size: int = 1024,
        run_listeners: Sequence[Callable[[FlowRunRecord], Awaitable[None] | None]] | None = None,
        governance: GovernanceManager | None = None,
    ) -> None:
        self._flows: Dict[str, Flow] = dict(flows or {})
        self._policies = policies or FlowPolicies()
        self._correlator = correlator or CorrelationHub()
        self._trace_sink = trace_sink
        self._governance = governance or GovernanceManager()
        self._recorder = Recorder.default()
        self._run_end_callbacks: list[Callable[[FlowRunRecord], Awaitable[None] | None]] = list(run_listeners or [])

        self._max_concurrency = max(1, int(max_concurrency))
        self._queue_capacity = max(0, int(queue_capacity))
        self._queue_wait_timeout = max(0.0, queue_wait_timeout_ms / 1000.0)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_capacity)

        self._workers: list[asyncio.Task] = []
        self._started = False
        self._active = 0

        self._stats: Dict[str, Any] = {
            "queue_depth_peak": 0,
            "queue_depth_current": 0,
            "active_peak": 0,
            "rejected": 0,
            "rejected_reason": defaultdict(int, {"QUEUE_FULL": 0, "QUEUE_TIMEOUT": 0}),
            "queued_ms": [],
            "exec_ms": [],
            "success": 0,
            "error": 0,
        }

        self._idempotency_ttl = max(0.0, float(idempotency_ttl_sec))
        self._idempotency_cache_size = max(0, int(idempotency_cache_size))
        self._inflight: Dict[str, asyncio.Future] = {}
        self._cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()

    def register(self, flow: Flow) -> None:
        self._flows[flow.name] = flow

    def choose_flow(self, name: str) -> Flow:
        try:
            return self._flows[name]
        except KeyError as exc:  # pragma: no cover - defensive guard
            raise KeyError(f"Unknown flow: {name}") from exc

    def build_dag(self, flow: Flow) -> FlowSpec:
        return flow.spec()

    async def run(
        self,
        name: str,
        *,
        inputs: Optional[Mapping[str, Any]] = None,
        response_mode: Optional[ResponseMode] = None,
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute ``name`` and return a structured result envelope."""
        return await self.execute(name, inputs=inputs, response_mode=response_mode, ctx=ctx)

    async def execute(
        self,
        name: str,
        *,
        inputs: Optional[Mapping[str, Any]] = None,
        response_mode: Optional[ResponseMode] = None,
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        flow = self.choose_flow(name)
        spec = self.build_dag(flow)

        payload = dict(inputs or {})
        meta = dict(ctx or {})

        maybe_run_id = meta.get("run_id")
        run_id: str = maybe_run_id if isinstance(maybe_run_id, str) else uuid.uuid4().hex
        model_name = None
        maybe_model = payload.get("model") or meta.get("model")
        if isinstance(maybe_model, str):
            model_name = maybe_model
        flow_id = spec.flow_id or spec.name

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        request = _Request(
            run_id=run_id,
            flow_name=name,
            flow_id=flow_id,
            flow_rev=spec.flow_revision(),
            spec=spec,
            inputs=payload,
            response_mode=response_mode or self._policies.response_mode,
            ctx=meta,
            model_name=model_name,
            future=future,
            enqueue_ts=time.perf_counter(),
        )

        idem_key = meta.get("idempotency_key") if isinstance(meta.get("idempotency_key"), str) else None
        if idem_key:
            cached = self._get_cached_result(idem_key)
            if cached is not None:
                return self._clone_result(cached)

            inflight = self._inflight.get(idem_key)
            if inflight is not None:
                result = await asyncio.shield(inflight)
                return self._clone_result(result)

            self._inflight[idem_key] = future
            request.ctx["idempotency_key"] = idem_key

        descriptor = _build_request_descriptor(request)

        if self._governance is not None:
            guard_decision = self._governance.evaluate_guard(request.inputs, descriptor)
            if not guard_decision.allowed:
                self._stats["rejected"] += 1
                reason = "GUARD_BLOCKED"
                reasons = self._stats["rejected_reason"]
                reasons[reason] += 1
                first = guard_decision.first
                if first:
                    self._recorder.on_guard_block(first.rule, spec.name)
                self._recorder.on_flow_finished(spec.name, request.model_name, "rejected")
                if idem_key:
                    self._inflight.pop(idem_key, None)
                meta = first.as_dict() if first else {"reason": "blocked"}
                meta.setdefault("scope", descriptor.flow)
                response = {
                    "status": "error",
                    "run_id": run_id,
                    "queued_ms": 0.0,
                    "exec_ms": 0.0,
                    "output": {"meta": meta},
                    "error_code": "GUARD_BLOCKED",
                    "error_message": "request blocked by guard",
                    "flow": spec.name,
                    "flow_name": spec.name,
                    "flow_id": spec.flow_id,
                    "flow_rev": spec.flow_revision(),
                }
                return response

        if self._governance is not None:
            decision = self._governance.check(descriptor)
            if not decision.allowed:
                self._stats["rejected"] += 1
                reason = decision.error_code or "GOVERNANCE_REJECTED"
                reasons = self._stats["rejected_reason"]
                reasons[reason] += 1
                self._recorder.on_flow_finished(spec.name, request.model_name, "rejected")
                if idem_key:
                    self._inflight.pop(idem_key, None)
                meta = dict(decision.meta)
                if decision.scope:
                    meta.setdefault("scope", decision.scope)
                response = {
                    "status": "rejected",
                    "run_id": run_id,
                    "queued_ms": 0.0,
                    "exec_ms": 0.0,
                    "output": {"meta": meta},
                    "error_code": decision.error_code,
                    "error_message": "request rejected by governance",
                    "flow": spec.name,
                    "flow_name": spec.name,
                    "flow_id": spec.flow_id,
                    "flow_rev": spec.flow_revision(),
                }
                return response
            request.governance_decision = decision
            request.governance_descriptor = descriptor

        accepted = await self._enqueue(request)
        if accepted != "OK":
            self._stats["rejected"] += 1
            self._stats["rejected_reason"][accepted] += 1
            self._recorder.on_flow_finished(spec.name, model_name, "rejected")
            if request.governance_decision is not None:
                self._governance.cancel(request.governance_decision)
            if idem_key:
                self._inflight.pop(idem_key, None)
            return {
                "status": "rejected",
                "run_id": run_id,
                "queued_ms": 0.0,
                "exec_ms": 0.0,
                "output": {},
                "error_code": accepted,
                "error_message": "request rejected",
                "flow": spec.name,
                "flow_name": spec.name,
                "flow_id": spec.flow_id,
                "flow_rev": spec.flow_revision(),
            }

        return await future

    async def _enqueue(self, request: _Request) -> str:
        await self._ensure_workers()
        put_started = time.perf_counter()
        queue_timeout = self._queue_wait_timeout
        try:
            if self._queue_capacity == 0:
                await self._queue.put(request)
            elif queue_timeout <= 0:
                self._queue.put_nowait(request)
            else:
                await asyncio.wait_for(self._queue.put(request), timeout=queue_timeout)
        except asyncio.QueueFull:
            return "QUEUE_FULL"
        except asyncio.TimeoutError:
            return "QUEUE_TIMEOUT"
        else:
            request.enqueue_ts = put_started
            self._record_queue_depth()
            return "OK"

    async def _ensure_workers(self) -> None:
        if self._started:
            return
        loop = asyncio.get_running_loop()
        for _ in range(self._max_concurrency):
            self._workers.append(loop.create_task(self._worker()))
        self._started = True

    async def _worker(self) -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                self._queue.task_done()
                self._record_queue_depth()
                break

            start_ts = time.perf_counter()
            queued_ms = (start_ts - request.enqueue_ts) * 1000.0
            self._stats["queued_ms"].append(queued_ms)

            self._active += 1
            self._stats["active_peak"] = max(self._stats["active_peak"], self._active)

            status = "ok"
            error_code = None
            error_message = None
            output: Dict[str, Any] = {}

            self._recorder.on_flow_started(request.spec.name, request.model_name)
            if request.governance_decision is not None:
                self._governance.activate(request.governance_decision)

            run_start_ts = time.time()
            exec_start = time.perf_counter()
            try:
                output = await self._run_flow(request)
            except PendingApproval as pending:
                status = "pending"
                error_code = "APPROVAL_REQUIRED"
                error_message = pending.record.reason
                output = pending.payload()
                self._recorder.on_flow_finished(request.spec.name, request.model_name, "pending")
                if self._governance.audit.enabled:
                    self._governance.audit.record(
                        "hitl_pending",
                        {
                            "flow": request.flow_name,
                            "approval_id": pending.record.approval_id,
                            "reason": pending.record.reason,
                        },
                    )
            except GuardBlockedError as exc:
                status = "error"
                error_code = "GUARD_BLOCKED"
                error_message = exc.violation.reason
                output = {
                    "status": "error",
                    "violation": exc.violation.as_dict(),
                }
                self._recorder.on_guard_block(exc.violation.rule, request.flow_name)
                self._recorder.on_flow_finished(request.spec.name, request.model_name, "error")
                if self._governance.audit.enabled:
                    self._governance.audit.record(
                        "guard_block",
                        {
                            "flow": request.flow_name,
                            "rule": exc.violation.rule,
                            "reason": exc.violation.reason,
                        },
                    )
            except Exception as exc:
                status = "error"
                error_code = exc.__class__.__name__
                error_message = str(exc)
                output = {}
                self._recorder.on_flow_finished(request.spec.name, request.model_name, "error")
                self._stats["error"] += 1
            else:
                self._recorder.on_flow_finished(request.spec.name, request.model_name, "ok")
                self._stats["success"] += 1
            finally:
                self._active -= 1

            exec_ms = (time.perf_counter() - exec_start) * 1000.0
            run_end_ts = time.time()
            self._stats["exec_ms"].append(exec_ms)

            result = {
                "status": status,
                "run_id": request.run_id,
                "queued_ms": queued_ms,
                "exec_ms": exec_ms,
                "output": output,
                "error_code": error_code,
                "error_message": error_message,
                "flow": request.flow_name,
                "flow_id": request.flow_id,
                "flow_rev": request.flow_rev,
                "flow_name": request.flow_name,
            }

            tokens_used, cost_used = _extract_usage(output)
            if request.governance_decision is not None and request.governance_descriptor is not None:
                self._governance.finalize(
                    request.governance_decision,
                    request.governance_descriptor,
                    status=status,
                    error_code=error_code,
                    tokens=tokens_used,
                    cost=cost_used,
                )

            idem_key = request.ctx.get("idempotency_key")
            if isinstance(idem_key, str):
                self._inflight.pop(idem_key, None)
                self._maybe_cache_result(idem_key, result)

            if not request.future.done():
                request.future.set_result(result)

            record = self._build_run_record(
                request,
                status=status,
                output=output,
                queued_ms=queued_ms,
                exec_ms=exec_ms,
                run_start_ts=run_start_ts,
                run_end_ts=run_end_ts,
                error_code=error_code,
                error_message=error_message,
            )
            self._schedule_run_end(record)

            self._queue.task_done()
            self._record_queue_depth()

    async def _run_flow(self, request: _Request) -> Dict[str, Any]:

        if request.response_mode is ResponseMode.DEFERRED:
            return await self._run_deferred(request)
        result = await self._execute_immediately(request)
        return {
            "mode": "immediate",
            "steps": result["steps"],
            "state": result["state"],
        }

    async def _run_deferred(self, request: _Request) -> Dict[str, Any]:
        spec = request.spec
        payload = request.inputs
        if not self._policies.allow_deferred:
            raise RuntimeError("Deferred execution is disabled by policy")
        token = self._correlator.reserve(spec.name, payload)
        req_id = payload.get("req_id")
        ready: Optional[Dict[str, Any]] = None
        if isinstance(req_id, str):
            ready = self._correlator.consume_signal(req_id)
            wait_s = max(float(getattr(self._policies, "short_wait_s", 0.0)), 0.0)
            if ready is None and wait_s > 0:
                deadline = time.time() + wait_s
                while ready is None and time.time() < deadline:
                    await asyncio.sleep(min(0.005, max(0.0, deadline - time.time())))
                    ready = self._correlator.consume_signal(req_id)
        if ready is not None:
            self._correlator.consume(token)
            return {
                "mode": "deferred",
                "status": "ready",
                "token": token,
                "result": ready,
            }
        return {
            "mode": "deferred",
            "status": "pending",
            "token": token,
        }

    async def _execute_immediately(
        self,
        request: _Request,
    ) -> Dict[str, Any]:
        spec = request.spec
        inputs = request.inputs
        executed: list[Dict[str, Any]] = []
        current = spec.entrypoint or (next(iter(spec.steps)) if spec.steps else None)
        visited = set()
        state: Any = dict(inputs or {})

        while current:
            step_def = spec.step(current)
            start_ts = time.time()
            next_step: Optional[str] = None
            step_ctx: Dict[str, Any] = {
                "flow": spec.name,
                "flow_id": request.flow_id,
                "flow_rev": request.flow_rev,
                "step": current,
                "index": len(executed),
                "executed": list(executed),
                "run_id": request.run_id,
                "config": dict(step_def.config),
                "governance": self._governance,
            }
            caught_exc: Optional[BaseException] = None
            status = "ok"
            error_code: Optional[str] = None
            error_message: Optional[str] = None
            try:
                seq = len(executed)
                step_ctx["seq"] = seq
                step_ctx["step_id"] = spec.step_id(current)
                step_ctx["event_id"] = f"{request.run_id}:{seq}"
                await self._invoke_hook(step_def.before, step_ctx)
                state = await self._run_step(step_def, step_ctx, state)
                await self._invoke_after(step_def.after, step_ctx, state)
                next_step = self._next_step(step_def, inputs)
            except Exception as exc:
                status = "error"
                error_code = exc.__class__.__name__
                error_message = str(exc)
                await self._invoke_error(step_def.on_error, step_ctx, exc)
                caught_exc = exc
            end_ts = time.time()

            executed.append({"name": current, "step_id": step_ctx["step_id"], "seq": step_ctx["seq"]})
            visited.add(current)

            if self._trace_sink is not None:
                span = TraceSpanLike(
                    flow=spec.name,
                    flow_id=request.flow_id,
                    flow_rev=request.flow_rev,
                    run_id=request.run_id,
                    step=current,
                    step_id=step_ctx.get("step_id", current),
                    seq=step_ctx.get("seq", 0),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    rule=spec.name,
                )
                self._trace_sink.append(span)

            if caught_exc is not None:
                raise caught_exc

            if not next_step or next_step in visited:
                break
            current = next_step

        return {"steps": executed, "state": state}

    def _next_step(self, step: StepDef, inputs: Optional[Dict[str, Any]]) -> Optional[str]:  # noqa: ARG002
        if not step.next_steps:
            return None
        if step.operation is Operation.SWITCH:
            cfg = dict(step.config) if hasattr(step.config, "items") else {}
            key = cfg.get("key")
            if isinstance(key, str) and key in step.next_steps:
                return key
            default = cfg.get("default")
            if isinstance(default, str) and default in step.next_steps:
                return default
        return step.next_steps[0]

    async def _run_step(self, step: StepDef, ctx: Dict[str, Any], state: Any) -> Any:
        if step.operation is Operation.TASK:
            if step.run is not None:
                result = await self._invoke_callable(step.run, ctx, state)
                return state if result is None else result
            return state
        return state

    async def _invoke_hook(self, hook: Callable[..., Any] | None, *args: Any) -> Any:
        if hook is None:
            return None
        return await self._invoke_callable(hook, *args)

    async def _invoke_after(self, hook: Callable[..., Any] | None, ctx: Dict[str, Any], output: Any) -> Any:
        if hook is None:
            return None
        return await self._invoke_callable(hook, ctx, output)

    async def _invoke_error(self, hook: Callable[..., Any] | None, ctx: Dict[str, Any], exc: BaseException) -> Any:
        if hook is None:
            return None
        return await self._invoke_callable(hook, ctx, exc)

    async def _invoke_callable(self, fn: Callable[..., Any], *args: Any) -> Any:
        if inspect.iscoroutinefunction(fn):
            return await fn(*args)
        result = await asyncio.to_thread(fn, *args)
        if inspect.isawaitable(result):
            return await result  # pragma: no cover
        return result

    def _close_trace_sink(self) -> None:
        if self._trace_sink is None:
            return
        close = getattr(self._trace_sink, "close", None)
        if callable(close):
            try:
                close()
            except TypeError:
                try:
                    close(flush=True)
                except TypeError:
                    pass

    async def aclose(self) -> None:
        if not self._started:
            self._close_trace_sink()
            return
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._started = False
        self._workers.clear()
        self._close_trace_sink()

    def get_stats(self) -> Dict[str, Any]:
        def percentile(data: list[float], pct: float) -> float:
            if not data:
                return 0.0
            ordered = sorted(data)
            idx = int(round((pct / 100.0) * (len(ordered) - 1)))
            return ordered[idx]

        queued = list(self._stats["queued_ms"])
        execs = list(self._stats["exec_ms"])

        return {
            "queue_depth_peak": self._stats["queue_depth_peak"],
            "queue_depth_current": self._stats["queue_depth_current"],
            "active_peak": self._stats["active_peak"],
            "rejected": self._stats["rejected"],
            "rejected_reason": dict(self._stats["rejected_reason"]),
            "success": self._stats["success"],
            "error": self._stats["error"],
            "queued_ms_p50": percentile(queued, 50.0),
            "queued_ms_p95": percentile(queued, 95.0),
            "queued_ms_p99": percentile(queued, 99.0),
            "exec_ms_p50": percentile(execs, 50.0),
            "exec_ms_p95": percentile(execs, 95.0),
            "exec_ms_p99": percentile(execs, 99.0),
        }

    def _maybe_cache_result(self, key: str, result: Dict[str, Any]) -> None:
        if self._idempotency_ttl <= 0 or self._idempotency_cache_size == 0:
            return
        if result.get("status") != "ok":
            return
        expires = time.monotonic() + self._idempotency_ttl
        self._cache[key] = (expires, self._clone_result(result))
        self._cache.move_to_end(key)
        while len(self._cache) > self._idempotency_cache_size:
            self._cache.popitem(last=False)

    def _get_cached_result(self, key: str) -> Optional[Dict[str, Any]]:
        if self._idempotency_ttl <= 0 or self._idempotency_cache_size == 0:
            return None
        entry = self._cache.get(key)
        if not entry:
            return None
        expires, value = entry
        if expires < time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return self._clone_result(value)

    @staticmethod
    def _clone_result(result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": result.get("status"),
            "run_id": result.get("run_id"),
            "queued_ms": result.get("queued_ms"),
            "exec_ms": result.get("exec_ms"),
            "output": copy.deepcopy(result.get("output")),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
            "flow": result.get("flow"),
            "flow_id": result.get("flow_id"),
            "flow_rev": result.get("flow_rev"),
            "flow_name": result.get("flow_name"),
        }

    def _record_queue_depth(self) -> None:
        depth = self._queue.qsize()
        self._stats["queue_depth_current"] = depth
        self._stats["queue_depth_peak"] = max(self._stats["queue_depth_peak"], depth)

    def add_run_listener(self, listener: Callable[[FlowRunRecord], Awaitable[None] | None]) -> None:
        self._run_end_callbacks.append(listener)

    def _schedule_run_end(self, record: FlowRunRecord) -> None:
        if not self._run_end_callbacks:
            return
        loop = asyncio.get_running_loop()
        for callback in self._run_end_callbacks:
            try:
                result = callback(record)
            except Exception:  # pragma: no cover - defensive guard
                logger.exception("run_end listener invocation failed")
                continue
            if asyncio.iscoroutine(result):
                loop.create_task(self._guard_run_end(result))

    async def _guard_run_end(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("run_end listener coroutine failed")

    def _build_run_record(
        self,
        request: _Request,
        *,
        status: str,
        output: Dict[str, Any],
        queued_ms: float,
        exec_ms: float,
        run_start_ts: float,
        run_end_ts: float,
        error_code: Optional[str],
        error_message: Optional[str],
    ) -> FlowRunRecord:
        outcome = _extract_str(output, "status")
        state = output.get("state") if isinstance(output, Mapping) else None
        if outcome is None and isinstance(state, Mapping):
            outcome = _extract_str(state, "outcome") or _extract_str(state, "status")

        cost = _extract_float(output, "cost_usd")
        rating = _extract_float(output, "user_rating")

        if cost is None and isinstance(state, Mapping):
            cost = _extract_float(state, "cost_usd")
        if rating is None and isinstance(state, Mapping):
            rating = _extract_float(state, "user_rating")

        binding = _extract_binding(request.ctx)
        selected_flow = _extract_selected_flow(request)

        meta: Dict[str, Any] = {
            "model": _extract_str(request.ctx, "model"),
            "operation": _extract_operation(request.ctx),
            "error_code": error_code,
            "error_message": error_message,
            "policy_arm": selected_flow,
        }
        if isinstance(request.inputs, Mapping):
            meta["inputs_size"] = len(request.inputs)

        duration_ms = max(0.0, (run_end_ts - run_start_ts) * 1000.0)

        return FlowRunRecord(
            flow=request.flow_name,
            flow_id=request.flow_id,
            flow_rev=request.flow_rev,
            run_id=request.run_id,
            selected_flow=selected_flow,
            binding=binding,
            status=status,
            outcome=outcome,
            queued_ms=queued_ms,
            exec_ms=exec_ms,
            duration_ms=duration_ms,
            start_ts=run_start_ts,
            end_ts=run_end_ts,
            cost_usd=cost,
            user_rating=rating,
            meta={k: v for k, v in meta.items() if v is not None},
        )


def _extract_str(source: Any, key: str) -> Optional[str]:
    if isinstance(source, Mapping):
        value = source.get(key)
        if isinstance(value, str):
            return value
    return None


def _extract_float(source: Any, key: str) -> Optional[float]:
    if isinstance(source, Mapping):
        value = source.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _extract_binding(ctx: Any) -> Optional[str]:
    if isinstance(ctx, Mapping):
        value = ctx.get("binding")
        if isinstance(value, str):
            return value
    return None


def _extract_selected_flow(request: _Request) -> str:
    ctx = request.ctx
    if isinstance(ctx, Mapping):
        selected = ctx.get("selected_flow")
        if isinstance(selected, str):
            return selected
    return request.flow_name


def _extract_operation(ctx: Any) -> Optional[str]:
    if isinstance(ctx, Mapping):
        op = ctx.get("operation") or ctx.get("op")
        if isinstance(op, str):
            return op
        if hasattr(op, "value"):
            return getattr(op, "value")
    return None


def _build_request_descriptor(request: _Request) -> RequestDescriptor:
    binding = _extract_binding(request.ctx)
    policy_arm = _extract_selected_flow(request) if binding else None
    return RequestDescriptor(flow=request.flow_name, binding=binding, policy_arm=policy_arm)


def _extract_usage(source: Any) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(source, Mapping):
        return None, None
    tokens, cost = _extract_usage_from_mapping(source)
    if tokens is None or cost is None:
        state = source.get("state")
        if isinstance(state, Mapping):
            state_tokens, state_cost = _extract_usage_from_mapping(state)
            tokens = tokens if tokens is not None else state_tokens
            cost = cost if cost is not None else state_cost
    return tokens, cost


def _extract_usage_from_mapping(mapping: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    tokens = _coerce_float(mapping.get("total_tokens") or mapping.get("tokens"))
    cost = _coerce_float(mapping.get("cost_usd") or mapping.get("usd_cost"))

    usage = mapping.get("usage")
    if isinstance(usage, Mapping):
        total = _coerce_float(usage.get("total_tokens"))
        if total is None:
            prompt = _coerce_float(usage.get("prompt_tokens"))
            completion = _coerce_float(usage.get("completion_tokens"))
            if prompt is not None or completion is not None:
                total = (prompt or 0.0) + (completion or 0.0)
        if total is not None:
            tokens = total if tokens is None else tokens
        cost_value = _coerce_float(usage.get("cost_usd") or usage.get("usd"))
        if cost_value is not None:
            cost = cost if cost is not None else cost_value

    return tokens, cost


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
