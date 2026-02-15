"""Routes model operations into flow executions."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, Mapping, MutableMapping, Optional, Protocol, Sequence

from tm.flow.operations import ResponseMode


from tm.ai.hooks import DecisionHook, NullDecisionHook
from tm.ai.policy_adapter import BindingPolicy, PolicyAdapter, PolicyDecision
from tm.ai.tuner import BanditTuner
from tm.obs.recorder import Recorder
from .binding import BindingSpec, Operation, _coerce_operation

logger = logging.getLogger(__name__)


class RuntimeLike(Protocol):  # pragma: no cover - structural typing helper
    async def run(
        self,
        name: str,
        *,
        inputs: Optional[Mapping[str, Any]] = None,
        response_mode: Optional[ResponseMode] = None,
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]: ...


class OperationRouter:
    """Resolve operations to flows and invoke the runtime."""

    def __init__(
        self,
        runtime: RuntimeLike,
        bindings: Mapping[str, BindingSpec],
        *,
        hook: Optional[DecisionHook] = None,
        tuner: Optional[BanditTuner] = None,
        policy_adapter: Optional[PolicyAdapter] = None,
    ):
        self._runtime = runtime
        self._bindings = dict(bindings)
        self._hook = hook or NullDecisionHook()
        self._tuner = tuner or BanditTuner()
        self._policy_adapter = policy_adapter
        self._registered: set[str] = set()

    async def dispatch(
        self,
        *,
        model: str,
        operation: Operation | str,
        payload: MutableMapping[str, object],
        context: Optional[Dict[str, object]] = None,
        response_mode: Optional[ResponseMode] = None,
    ) -> Dict[str, object]:
        spec = self._bindings.get(model)
        if spec is None:
            raise KeyError(f"No bindings registered for model '{model}'")

        op_enum = _coerce_operation(operation)
        Recorder.default().on_service_request(model, op_enum.value)
        ctx: Dict[str, object] = {"model": model, "op": op_enum, "payload": payload}
        if context:
            ctx.update(context)

        before = getattr(self._hook, "before_route", None)
        if callable(before):
            maybe = before(ctx)
            if inspect.isawaitable(maybe):
                await maybe
        else:  # pragma: no cover - defensive fallback when attribute shadowed
            fallback_before = getattr(type(self._hook), "before_route", None)
            if callable(fallback_before):
                maybe = fallback_before(self._hook, ctx)
                if inspect.isawaitable(maybe):
                    await maybe

        candidates = list(_matching_flows(spec, op_enum, ctx))
        if not candidates:
            raise LookupError(f"No binding rule matched for op '{op_enum.value}' on model '{model}'")

        binding_key = f"{spec.model}:{op_enum.value}"
        self._ensure_binding_registered(binding_key, spec)

        config = await self._tuner.config(binding_key)

        decision: PolicyDecision | None = None
        arm_list = list(dict.fromkeys(candidates))
        if self._policy_adapter is not None:
            decision = await self._policy_adapter.prepare(binding_key, tuple(arm_list), ctx, config.version)
            if decision.arms:
                arm_list = list(dict.fromkeys(decision.arms)) or arm_list
        remote_version = decision.remote_version if decision else None
        fallback = decision.fallback if decision else False

        override = ctx.get("flow_override") if isinstance(ctx, dict) else None
        if isinstance(override, str) and override in arm_list:
            flow_name = override
        elif len(arm_list) == 1:
            flow_name = arm_list[0]
        else:
            flow_name = await self._tuner.choose(binding_key, arm_list)

        logger.info(
            "policy.select binding=%s local_version=%s remote_version=%s fallback=%s choice=%s candidates=%s",
            binding_key,
            config.version,
            remote_version or config.version,
            fallback,
            flow_name,
            arm_list,
        )

        inputs = dict(payload)
        inputs.setdefault("model", model)
        inputs.setdefault("op", op_enum.value)
        inputs.setdefault("binding", binding_key)
        if context:
            inputs.setdefault("context", context)

        runtime_ctx = {
            "model": model,
            "operation": op_enum.value,
            "binding": binding_key,
            "selected_flow": flow_name,
            "candidates": tuple(arm_list),
            "policy": {
                "local_version": config.version,
                "remote_version": remote_version or config.version,
                "fallback": fallback,
            },
        }
        result = await self._runtime.run(
            flow_name,
            inputs=inputs,
            response_mode=response_mode,
            ctx=runtime_ctx,
        )
        enriched = {"flow": flow_name, **result}
        after = getattr(self._hook, "after_result", None)
        if callable(after):
            maybe = after(enriched)
            if inspect.isawaitable(maybe):
                await maybe
        else:  # pragma: no cover - defensive fallback when attribute shadowed
            fallback_after = getattr(type(self._hook), "after_result", None)
            if callable(fallback_after):
                maybe = fallback_after(self._hook, enriched)
                if inspect.isawaitable(maybe):
                    await maybe
        return enriched

    def _ensure_binding_registered(self, binding_key: str, spec: BindingSpec) -> None:
        if self._policy_adapter is None or binding_key in self._registered:
            return
        metadata = BindingPolicy(endpoint=spec.policy_endpoint, policy_ref=spec.policy_ref)
        self._policy_adapter.register_binding(binding_key, metadata)
        self._registered.add(binding_key)


def _matching_flows(spec: BindingSpec, operation: Operation, ctx: Mapping[str, Any]) -> Sequence[str]:
    specific: list[str] = []
    fallback: list[str] = []
    for rule in getattr(spec, "_rules", ()):
        if rule.matches(operation, ctx):
            if getattr(rule, "predicate", None) is None:
                fallback.append(rule.flow_name)
            else:
                specific.append(rule.flow_name)
    if specific:
        return specific
    if fallback:
        return fallback
    flow = spec.resolve(operation, dict(ctx))
    if flow is not None:
        return [flow]
    return []
