# tm/flow/core.py
"""
TraceMind Flow — Single-File Core (v0.3)
----------------------------------------
- Single-file runnable; only depends on `networkx`.
- Extensible by design: Operator registry + metadata, Check registry, tiny policies, Airflow-style tracer, and a static analyzer.
- Flow selection/instantiation: provide `FlowBase` and `FlowRepo` so repos can register multiple flows and instantiate by name.

Install:
  pip install networkx

Quick usage:
  from tm.flow.core import registry, checks, FlowRepo, Engine, AirflowStyleTracer
  # 1) Register operators (see demo at bottom of this file)
  # 2) Define your Flow (subclass FlowBase or build manually) and register it into FlowRepo
  # 3) Run StaticAnalyzer().check(flow) first to catch basic issues
  # 4) Engine(tracer=AirflowStyleTracer()).run(flow, inputs={...})
"""

# TODO add some unit-tests for all class and methods in this file
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Type
import time
import uuid
import networkx as nx


# =============================================================
# 0) Public enums & dataclasses
# =============================================================
class NodeKind(Enum):
    TASK = auto()  # run an operator
    FINISH = auto()  # terminal marker
    SWITCH = auto()  # route by key
    PARALLEL = auto()  # run multiple operators concurrently


@dataclass
class Step:
    """A node in the flow graph.

    For TASK: `uses` is the operator name; other kinds ignore `uses`.
    `cfg` may contain:
      - SWITCH: {"key_from": "$.vars.validate.ok", "default": "_DEFAULT"}
                 → use link_case(src, dst, case=...) to label branches
      - PARALLEL: {"uses": ["op.a", "op.b"], "max_workers": 4}
      - Common policies: retry/timeout, before/after checks (see below)
    """

    id: str
    kind: NodeKind
    uses: Optional[str] = None
    cfg: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecContext:
    """Execution context provided to operators and checks."""

    flow_name: str
    trace_id: str
    vars: Dict[str, Any] = field(default_factory=dict)
    # You can extend this at Engine instantiation time (e.g., ctx.logger / ctx.clients)


@dataclass
class StepResult:
    status: str  # "ok" | "error"
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


# =============================================================
# 1) Operator registry (+ metadata for static race checks)
# =============================================================
OperatorFn = Callable[[ExecContext, Dict[str, Any]], Dict[str, Any]]


class OperatorRegistry:
    def __init__(self):
        self._ops: Dict[str, OperatorFn] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}

    def operator(self, name: str):
        """Decorator to register a function under `name`.
        Function signature: fn(ctx: ExecContext, inputs: Dict[str, Any]) -> Dict[str, Any]
        """

        def deco(fn: OperatorFn):
            if name in self._ops:
                raise ValueError(f"Operator already registered: {name}")
            self._ops[name] = fn
            return fn

        return deco

    def set_meta(
        self,
        name: str,
        *,
        reads: List[str] | None = None,
        writes: List[str] | None = None,
        externals: List[str] | None = None,
        pure: bool = False,
    ):
        if name not in self._ops:
            raise KeyError(f"set_meta for unknown operator: {name}")
        self._meta[name] = {
            "reads": set(reads or []),
            "writes": set(writes or []),
            "externals": set(externals or []),  # e.g., "db.users", "kafka.topicA"
            "pure": bool(pure),
        }

    def meta(self, name: str) -> Dict[str, Any]:
        return self._meta.get(name, {"reads": set(), "writes": set(), "externals": set(), "pure": False})

    def get(self, name: str) -> OperatorFn:
        if name not in self._ops:
            raise KeyError(f"Unknown operator: {name}")
        return self._ops[name]


registry = OperatorRegistry()

# =============================================================
# 2) Check registry (before/after)
# =============================================================
CheckFn = Callable[[ExecContext, Dict[str, Any]], None]


class CheckRegistry:
    def __init__(self):
        self._checks: Dict[str, CheckFn] = {}

    def check(self, name: str):
        def deco(fn: CheckFn):
            if name in self._checks:
                raise ValueError(f"Check already registered: {name}")
            self._checks[name] = fn
            return fn

        return deco

    def get(self, name: str) -> CheckFn:
        if name not in self._checks:
            raise KeyError(f"Unknown check: {name}")
        return self._checks[name]


checks = CheckRegistry()


# =============================================================
# 3) FlowGraph: minimal DAG wrapper over networkx
# =============================================================
class FlowGraph:
    def __init__(self, name: str):
        self.name = name
        self._g = nx.DiGraph()
        self._entry: Optional[str] = None

    # ---- node builders ----
    def task(self, node_id: str, *, uses: str, **cfg) -> str:
        self._add_node(Step(node_id, NodeKind.TASK, uses=uses, cfg=cfg))
        return node_id

    def finish(self, node_id: str) -> str:
        self._add_node(Step(node_id, NodeKind.FINISH))
        return node_id

    def switch(self, node_id: str, *, key_from: str, default: str = "_DEFAULT") -> str:
        self._add_node(Step(node_id, NodeKind.SWITCH, cfg={"key_from": key_from, "default": default}))
        return node_id

    def parallel(self, node_id: str, *, uses: List[str], max_workers: int = 4, **cfg) -> str:
        cfg = {**cfg, "uses": list(uses), "max_workers": int(max_workers)}
        self._add_node(Step(node_id, NodeKind.PARALLEL, cfg=cfg))
        return node_id

    # ---- edges ----
    def link(self, src: str, dst: str) -> None:
        self._g.add_edge(src, dst)

    def link_case(self, src: str, dst: str, *, case: Any) -> None:
        self._g.add_edge(src, dst, case=case)

    def set_entry(self, node_id: str) -> None:
        if node_id not in self._g:
            raise KeyError(f"Unknown node: {node_id}")
        self._entry = node_id

    def entry(self) -> str:
        if not self._entry:
            raise RuntimeError("Entry node not set")
        return self._entry

    def successors(self, node_id: str) -> List[str]:
        return list(self._g.successors(node_id))

    def node(self, node_id: str) -> Step:
        return self._g.nodes[node_id]["step"]

    def edge_attr(self, src: str, dst: str, key: str, default: Any = None) -> Any:
        return self._g.get_edge_data(src, dst, {}).get(key, default)

    def to_networkx(self) -> nx.DiGraph:
        return self._g

    # ---- helpers ----
    def _add_node(self, step: Step) -> None:
        if step.id in self._g:
            raise ValueError(f"Duplicate node id: {step.id}")
        self._g.add_node(step.id, step=step)
        if self._entry is None:
            self._entry = step.id


# Tiny sugar to chain edges: chain(f, "a", "b", "c")
def chain(flow: FlowGraph, *ids: str) -> str:
    for i in range(len(ids) - 1):
        flow.link(ids[i], ids[i + 1])
    return ids[-1]


# =============================================================
# 4) FlowBase & FlowRepo (to select/instantiate flows)
# =============================================================
class FlowBase:
    """Recommended base class: implement build(self, **params) -> FlowGraph."""

    name: str = ""

    def build(self, **params) -> FlowGraph:  # override in subclasses
        raise NotImplementedError


class FlowRepo:
    def __init__(self):
        self._flows: Dict[str, Type[FlowBase]] = {}

    def register(self, flow_cls: Type[FlowBase]):
        name = getattr(flow_cls, "name", None)
        if not name:
            raise ValueError("Flow class must define a non-empty `name`")
        if name in self._flows:
            raise ValueError(f"Flow already registered: {name}")
        self._flows[name] = flow_cls
        return flow_cls

    def instantiate(self, name: str, **params) -> FlowGraph:
        if name not in self._flows:
            raise KeyError(f"Unknown flow: {name}")
        return self._flows[name]().build(**params)

    def list(self) -> List[str]:
        return sorted(self._flows.keys())


flowrepo = FlowRepo()


# =============================================================
# 5) Static Analyzer (cycles, reachability, operator existence, basic race)
# =============================================================
class StaticAnalyzer:
    def __init__(self, registry: OperatorRegistry):
        self.registry = registry

    def check(self, flow: FlowGraph) -> List[Dict[str, Any]]:
        g = flow.to_networkx()
        issues: List[Dict[str, Any]] = []
        # 1) DAG acyclicity
        if not nx.is_directed_acyclic_graph(g):
            issues.append({"kind": "cycle", "msg": "Graph contains cycles"})
        # 2) entry + reachability
        try:
            entry = flow.entry()
        except Exception:
            issues.append({"kind": "entry", "msg": "Entry node is not set"})
            entry = None
        if entry is not None:
            reachable = set(nx.descendants(g, entry)) | {entry}
            for n in g.nodes:
                if n not in reachable:
                    issues.append({"kind": "unreachable", "node": n, "msg": f"Node '{n}' is not reachable from entry"})
        # 3) operator existence & node sanity
        for n, data in g.nodes(data=True):
            step_obj = data.get("step")
            if not isinstance(step_obj, Step):
                continue
            if step_obj.kind == NodeKind.TASK and step_obj.uses is not None:
                try:
                    self.registry.get(step_obj.uses)
                except KeyError:
                    issues.append({"kind": "operator_missing", "node": n, "op": step_obj.uses})
            if step_obj.kind == NodeKind.SWITCH:
                succ = list(g.successors(n))
                if not succ:
                    issues.append({"kind": "switch_no_branch", "node": n})
                has_default = any(
                    g.get_edge_data(n, s, {}).get("case") == step_obj.cfg.get("default", "_DEFAULT") for s in succ
                )
                if not has_default:
                    issues.append({"kind": "switch_no_default", "node": n, "msg": "Consider adding default branch"})
            if step_obj.kind == NodeKind.PARALLEL:
                uses = step_obj.cfg.get("uses", [])
                if not uses:
                    issues.append({"kind": "parallel_empty", "node": n})
        # 4) basic data race within a PARALLEL block (write/write or read/write)
        for n, data in g.nodes(data=True):
            step_obj = data.get("step")
            if not isinstance(step_obj, Step) or step_obj.kind != NodeKind.PARALLEL:
                continue
            uses = [u for u in list(step_obj.cfg.get("uses", [])) if isinstance(u, str)]
            metas = [(u, self.registry.meta(u)) for u in uses]
            for i in range(len(metas)):
                ui, mi = metas[i]
                for j in range(i + 1, len(metas)):
                    uj, mj = metas[j]
                    # external resource conflicts
                    ext_conf = mi["externals"] & mj["externals"]
                    if ext_conf:
                        issues.append(
                            {"kind": "race_external", "node": n, "ops": [ui, uj], "resource": sorted(ext_conf)}
                        )
                    # write/write on same logical key
                    ww = mi["writes"] & mj["writes"]
                    if ww:
                        issues.append({"kind": "race_write_write", "node": n, "ops": [ui, uj], "keys": sorted(ww)})
                    # read/write hazards
                    rw = (mi["reads"] & mj["writes"]) | (mj["reads"] & mi["writes"])
                    if rw:
                        issues.append({"kind": "race_read_write", "node": n, "ops": [ui, uj], "keys": sorted(rw)})
        return issues


# =============================================================
# 6) Airflow-style Tracer (no Airflow deps)
# =============================================================
class AirflowStyleTracer:
    """Collects run artifacts in an Airflow-like shape."""

    def __init__(self, dag_id_prefix: str = "tm_flow", capture_outputs: bool = True, xcom_bytes_limit: int = 16_000):
        self.dag_id_prefix = dag_id_prefix
        self.capture_outputs = capture_outputs
        self.xcom_bytes_limit = xcom_bytes_limit
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._tis: Dict[str, Dict[str, Any]] = {}
        self._edges: Dict[str, List[Tuple[str, str]]] = {}

    def begin(self, flow_name: str) -> str:
        run_id = f"af_{uuid.uuid4().hex}"
        now = time.time()
        dag_id = f"{self.dag_id_prefix}.{flow_name}"
        self._runs[run_id] = {
            "dag_id": dag_id,
            "run_id": run_id,
            "conf": {},
            "start_date": now,
            "end_date": None,
            "state": "running",
        }
        self._tis[run_id] = {}
        self._edges.setdefault(run_id, [])
        return run_id

    def on_step(self, run_id: str, step: Step, result: StepResult, inputs: Dict[str, Any]):
        now = time.time()
        ti = {
            "dag_id": self._runs[run_id]["dag_id"],
            "run_id": run_id,
            "task_id": step.id,
            "try_number": 1,
            "state": "success" if result.status == "ok" else "failed",
            "start_date": now - (result.duration_ms / 1000.0),
            "end_date": now,
            "duration": result.duration_ms / 1000.0,
            "operator": step.uses,
            "kind": step.kind.name,
            "in": inputs,
            "out": None,
        }
        if self.capture_outputs and result.output is not None:
            import json

            s = json.dumps(result.output, ensure_ascii=False)
            if len(s) > self.xcom_bytes_limit:
                ti["out"] = {"truncated": True, "size": len(s)}
            else:
                ti["out"] = result.output
        self._tis[run_id][step.id] = ti

    def end(self, run_id: str, status: str):
        self._runs[run_id]["end_date"] = time.time()
        self._runs[run_id]["state"] = "success" if status == "ok" else ("failed" if status == "error" else status)

    def record_edges(self, run_id: str, edges: List[Tuple[str, str]]):
        self._edges[run_id] = list(edges)

    def get_run(self, run_id: str):
        dag_run = self._runs.get(run_id, {})
        tis = list((self._tis.get(run_id) or {}).values())
        edges = self._edges.get(run_id, [])
        return dag_run, tis, edges


# =============================================================
# 7) Tiny policies (retry + timeout) & Engine
# =============================================================
@dataclass
class RetryPolicy:
    max_attempts: int = 1  # 1 = no retry
    backoff_ms: int = 0  # fixed backoff


@dataclass
class TimeoutPolicy:
    timeout_ms: Optional[int] = None  # None = no timeout


@dataclass
class StepPolicies:
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)


def _parse_policies_from_cfg(cfg: Dict[str, Any]) -> StepPolicies:
    r_raw = cfg.get("retry", {})
    r = r_raw if isinstance(r_raw, dict) else {}
    t = cfg.get("timeout_ms", None)
    timeout_cfg = cfg.get("timeout")
    if isinstance(timeout_cfg, dict):
        t = timeout_cfg.get("timeout_ms", t)
    return StepPolicies(
        retry=RetryPolicy(
            max_attempts=_as_int(r.get("max_attempts"), default=1),
            backoff_ms=_as_int(r.get("backoff_ms"), default=0),
        ),
        timeout=TimeoutPolicy(
            timeout_ms=None if t in (None, "", 0) else _as_int(t, default=0),
        ),
    )


def _as_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


class Engine:
    def __init__(self, tracer: Optional[Any] = None):
        self.tracer = tracer or AirflowStyleTracer()

    # ---------------- internal helpers ----------------
    @staticmethod
    def _get_path(root: Dict[str, Any], dotted: Any) -> Any:
        """Very small JSONPath-lite: $.vars.*, $.inputs.*, $.cfg.*"""
        if not isinstance(dotted, str) or not dotted.startswith("$"):
            return dotted
        cur: Any = {"vars": root.get("vars", {}), "inputs": root.get("inputs", {}), "cfg": root.get("cfg", {})}
        parts = dotted.lstrip("$").lstrip(".").split(".")
        cur = cur.get(parts[0], {}) if parts else {}
        for p in parts[1:]:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                cur = None
                break
        return cur

    def _run_checks(self, names: List[str] | None, ctx: ExecContext, call_in: Dict[str, Any]):
        if not names:
            return
        for name in names:
            checks.get(name)(ctx, call_in)

    def _run_operator(
        self, op_name: str, ctx: ExecContext, call_in: Dict[str, Any], pol: StepPolicies
    ) -> Dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError

        attempts = 0
        last_exc: Optional[Exception] = None
        while attempts < pol.retry.max_attempts:
            attempts += 1
            try:
                if pol.timeout.timeout_ms:
                    with ThreadPoolExecutor(max_workers=1) as ex:
                        fut = ex.submit(registry.get(op_name), ctx, call_in)
                        return fut.result(timeout=pol.timeout.timeout_ms / 1000.0)
                else:
                    return registry.get(op_name)(ctx, call_in)
            except TimeoutError as e:
                last_exc = e
            except Exception as e:
                last_exc = e
            if attempts < pol.retry.max_attempts and pol.retry.backoff_ms > 0:
                time.sleep(pol.retry.backoff_ms / 1000.0)
        raise last_exc if last_exc else RuntimeError("operator failed without exception?")

    # ---------------- public API ----------------
    def run(self, flow: FlowGraph, *, inputs: Dict[str, Any] | None = None) -> Tuple[str, Dict[str, Any]]:
        run_id = self.tracer.begin(flow.name)
        ctx = ExecContext(flow_name=flow.name, trace_id=run_id, vars={})
        inputs = inputs or {}

        # record edges for later visualization
        try:
            self.tracer.record_edges(run_id, list(flow.to_networkx().edges()))
        except Exception:
            pass

        current = flow.entry()
        while True:
            step = flow.node(current)
            t0 = time.time()

            if step.kind is NodeKind.TASK:
                call_in = {"inputs": inputs, "cfg": step.cfg, "vars": ctx.vars}
                pol = _parse_policies_from_cfg(step.cfg)
                try:
                    self._run_checks(step.cfg.get("before"), ctx, call_in)
                    if not isinstance(step.uses, str):
                        raise ValueError("Task step is missing operator 'uses'")
                    out = self._run_operator(step.uses, ctx, call_in, pol)
                    self._run_checks(step.cfg.get("after"), ctx, {"**out": out, **call_in})
                    res = StepResult(status="ok", output=out, duration_ms=(time.time() - t0) * 1000)
                    ctx.vars[step.id] = out
                except Exception as e:
                    res = StepResult(status="error", error=str(e), duration_ms=(time.time() - t0) * 1000)
                self.tracer.on_step(run_id, step, res, call_in)
                if res.status != "ok":
                    self.tracer.end(run_id, "error")
                    break

            elif step.kind is NodeKind.SWITCH:
                root = {"vars": ctx.vars, "inputs": inputs, "cfg": step.cfg}
                key = self._get_path(root, step.cfg.get("key_from"))
                chosen = None
                for succ in flow.successors(current):
                    casev = flow.edge_attr(current, succ, "case", None)
                    if casev == key:
                        chosen = succ
                        break
                if chosen is None:
                    for succ in flow.successors(current):
                        if flow.edge_attr(current, succ, "case", None) == step.cfg.get("default", "_DEFAULT"):
                            chosen = succ
                            break
                res = StepResult(
                    status="ok",
                    output={"key": key, "chosen_case": (key if chosen else step.cfg.get("default"))},
                    duration_ms=(time.time() - t0) * 1000,
                )
                self.tracer.on_step(run_id, step, res, {"key_from": step.cfg.get("key_from")})
                if chosen is None:
                    self.tracer.end(run_id, "error")
                    break
                current = chosen
                continue

            elif step.kind is NodeKind.PARALLEL:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                uses_raw = step.cfg.get("uses", [])
                uses = [u for u in uses_raw if isinstance(u, str)]
                max_workers = _as_int(step.cfg.get("max_workers"), default=4)
                call_in_base = {"inputs": inputs, "cfg": step.cfg, "vars": ctx.vars}
                pol = _parse_policies_from_cfg(step.cfg)
                outputs: Dict[str, Any] = {}
                try:

                    def _one(op_name: str):
                        self._run_checks(step.cfg.get("before"), ctx, call_in_base)
                        out = self._run_operator(op_name, ctx, call_in_base, pol)
                        self._run_checks(step.cfg.get("after"), ctx, {"**out": out, **call_in_base})
                        return op_name, out

                    with ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futs = {ex.submit(_one, u): u for u in uses}
                        for fut in as_completed(futs):
                            name, out = fut.result()
                            outputs[name] = out
                    res = StepResult(status="ok", output={"parallel": outputs}, duration_ms=(time.time() - t0) * 1000)
                    ctx.vars[step.id] = outputs
                except Exception as e:
                    res = StepResult(status="error", error=str(e), duration_ms=(time.time() - t0) * 1000)
                self.tracer.on_step(run_id, step, res, call_in_base)
                if res.status != "ok":
                    self.tracer.end(run_id, "error")
                    break

            elif step.kind is NodeKind.FINISH:
                self.tracer.on_step(run_id, step, StepResult(status="ok", duration_ms=(time.time() - t0) * 1000), {})
                self.tracer.end(run_id, "ok")
                break

            # move to next (single successor semantics except SWITCH)
            next_nodes: List[str] = flow.successors(current)
            if not next_nodes:
                self.tracer.end(run_id, "ok")
                break
            current = next_nodes[0]

        return run_id, ctx.vars


# =============================================================
# 8) Demo operators & flows
# =============================================================
@registry.operator("core.validate_payload")
def op_validate_payload(ctx: ExecContext, call_in: Dict[str, Any]) -> Dict[str, Any]:
    payload = call_in["inputs"].get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    return {"ok": bool(payload), "fields": list(payload.keys())}


registry.set_meta("core.validate_payload", reads=["inputs.payload"], writes=["vars.validate"], externals=[], pure=True)


@registry.operator("adapters.echo.annotate")
def op_echo_annotate(ctx: ExecContext, call_in: Dict[str, Any]) -> Dict[str, Any]:
    payload = call_in["inputs"].get("payload", {})
    validated = call_in["vars"].get("validate", {})
    return {"payload": payload, "validated": validated, "trace": ctx.trace_id}


registry.set_meta(
    "adapters.echo.annotate",
    reads=["inputs.payload", "vars.validate"],
    writes=["vars.annotate"],
    externals=[],
    pure=True,
)


@registry.operator("parallel.add_one")
def op_add_one(ctx: ExecContext, call_in: Dict[str, Any]) -> Dict[str, Any]:
    x = call_in["inputs"].get("x", 0)
    return {"x_plus_one": x + 1}


registry.set_meta("parallel.add_one", reads=["inputs.x"], writes=["vars.fanout.add_one"], externals=[], pure=True)


@registry.operator("parallel.square")
def op_square(ctx: ExecContext, call_in: Dict[str, Any]) -> Dict[str, Any]:
    x = call_in["inputs"].get("x", 0)
    return {"x_square": x * x}


registry.set_meta("parallel.square", reads=["inputs.x"], writes=["vars.fanout.square"], externals=[], pure=True)


# ---- Checks (demo) ----
@checks.check("chk.require_payload")
def chk_require_payload(ctx: ExecContext, call_in: Dict[str, Any]) -> None:
    if not call_in.get("inputs", {}).get("payload"):
        raise ValueError("missing payload")


@checks.check("chk.emit_metric")
def chk_emit_metric(ctx: ExecContext, call_in: Dict[str, Any]) -> None:
    # attach your logger/metrics here if needed
    return


# ---- Flow definitions ----
class DemoSwitchParallel(FlowBase):
    name = "demo_switch_parallel"

    def build(self, **params) -> FlowGraph:
        f = FlowGraph(self.name)
        v = f.task("validate", uses="core.validate_payload", before=["chk.require_payload"])  # before-check
        s = f.switch("route", key_from="$.vars.validate.ok", default="_DEFAULT")
        p = f.parallel(
            "fanout",
            uses=["parallel.add_one", "parallel.square"],
            max_workers=4,
            timeout_ms=800,
        )
        a = f.task("annotate", uses="adapters.echo.annotate", after=["chk.emit_metric"])  # after-check
        d = f.finish("done")
        chain(f, v, s)
        f.link_case(s, p, case=True)
        f.link_case(s, a, case=False)
        f.link_case(s, a, case="_DEFAULT")
        chain(f, p, a, d)
        f.set_entry(v)
        return f


# Register into the repo
flowrepo.register(DemoSwitchParallel)

# Also expose a convenience builder


def build_demo_flow() -> FlowGraph:
    return DemoSwitchParallel().build()


# =============================================================
# 9) CLI / quick test
# =============================================================
if __name__ == "__main__":
    # Select/instantiate by repo
    print("AVAILABLE FLOWS:", flowrepo.list())
    flow = flowrepo.instantiate("demo_switch_parallel")

    # Static checks
    analyzer = StaticAnalyzer(registry)
    issues = analyzer.check(flow)
    print("STATIC ISSUES:", issues)

    # Run (Airflow-style tracing)
    tracer = AirflowStyleTracer()
    engine = Engine(tracer=tracer)
    run_id, ctx_vars = engine.run(flow, inputs={"payload": {"name": "Alice"}, "x": 3})

    dag_run, task_instances, edges = tracer.get_run(run_id)
