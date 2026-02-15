from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

PLAN_VERSION = "plan.v1"


class PlanValidationError(ValueError):
    """Raised when plan payload does not conform to Plan v1 schema."""


@dataclass(frozen=True)
class PlanConstraints:
    max_steps: Optional[int] = None
    budget_usd: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if self.max_steps is not None:
            data["max_steps"] = self.max_steps
        if self.budget_usd is not None:
            data["budget_usd"] = self.budget_usd
        return data


@dataclass(frozen=True)
class RetryPolicy:
    max: int
    backoff_ms: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"max": self.max}
        if self.backoff_ms is not None:
            data["backoff_ms"] = self.backoff_ms
        return data


@dataclass(frozen=True)
class OnErrorAction:
    retry: Optional[RetryPolicy] = None
    fallback: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if self.retry is not None:
            data["retry"] = self.retry.as_dict()
        if self.fallback is not None:
            data["fallback"] = self.fallback
        return data


@dataclass(frozen=True)
class PlanStep:
    id: str
    kind: str
    ref: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    on_error: Optional[OnErrorAction] = None

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "ref": self.ref,
            "inputs": dict(self.inputs),
        }
        if self.on_error is not None:
            data["on_error"] = self.on_error.as_dict()
        return data


@dataclass(frozen=True)
class AllowList:
    tools: Tuple[str, ...] = field(default_factory=tuple)
    flows: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> Dict[str, Sequence[str]]:
        return {"tools": list(self.tools), "flows": list(self.flows)}


@dataclass(frozen=True)
class Plan:
    version: str
    goal: str
    constraints: PlanConstraints
    allow: AllowList
    steps: Tuple[PlanStep, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "goal": self.goal,
            "constraints": self.constraints.as_dict(),
            "allow": self.allow.as_dict(),
            "steps": [step.as_dict() for step in self.steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"))


_ALLOWED_PLAN_KEYS = {"version", "goal", "constraints", "allow", "steps"}
_ALLOWED_CONSTRAINT_KEYS = {"max_steps", "budget_usd"}
_ALLOWED_ALLOW_KEYS = {"tools", "flows"}
_ALLOWED_STEP_KEYS = {"id", "kind", "ref", "inputs", "on_error"}
_ALLOWED_ON_ERROR_KEYS = {"retry", "fallback"}
_ALLOWED_RETRY_KEYS = {"max", "backoff_ms"}
_STEP_KINDS = {"tool", "flow"}


def load_plan_json(payload: str) -> Plan:
    """Parse JSON string into Plan and validate it."""

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - simple passthrough
        raise PlanValidationError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise PlanValidationError("Plan payload must be a JSON object")
    return validate_plan(data)


def validate_plan(data: Mapping[str, Any]) -> Plan:
    _ensure_allowed_keys(data, _ALLOWED_PLAN_KEYS, "plan")

    version = data.get("version")
    if version != PLAN_VERSION:
        raise PlanValidationError(f"Unsupported plan version: {version!r}")

    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise PlanValidationError("plan.goal must be a non-empty string")

    constraints = _parse_constraints(data.get("constraints"))
    allow = _parse_allow(data.get("allow"))
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, (str, bytes)):
        raise PlanValidationError("plan.steps must be a non-empty list")
    if not steps_raw:
        raise PlanValidationError("plan.steps must contain at least one entry")

    steps: list[PlanStep] = []
    seen_ids: set[str] = set()
    for index, raw_step in enumerate(steps_raw):
        if not isinstance(raw_step, Mapping):
            raise PlanValidationError(f"plan.steps[{index}] must be an object")
        step = _parse_step(raw_step, allow, index)
        if step.id in seen_ids:
            raise PlanValidationError(f"Duplicate step id: {step.id}")
        seen_ids.add(step.id)
        steps.append(step)

    return Plan(version=PLAN_VERSION, goal=goal.strip(), constraints=constraints, allow=allow, steps=tuple(steps))


def _parse_constraints(raw: Any) -> PlanConstraints:
    if raw is None:
        return PlanConstraints()
    if not isinstance(raw, Mapping):
        raise PlanValidationError("plan.constraints must be an object if present")
    _ensure_allowed_keys(raw, _ALLOWED_CONSTRAINT_KEYS, "plan.constraints")

    max_steps = raw.get("max_steps")
    if max_steps is not None:
        if not isinstance(max_steps, int) or max_steps <= 0:
            raise PlanValidationError("plan.constraints.max_steps must be a positive integer")

    budget_usd = raw.get("budget_usd")
    if budget_usd is not None:
        if not isinstance(budget_usd, (int, float)) or float(budget_usd) < 0:
            raise PlanValidationError("plan.constraints.budget_usd must be a non-negative number")
        budget_usd = float(budget_usd)

    return PlanConstraints(max_steps=max_steps, budget_usd=budget_usd)


def _parse_allow(raw: Any) -> AllowList:
    if raw is None:
        return AllowList()
    if not isinstance(raw, Mapping):
        raise PlanValidationError("plan.allow must be an object")
    _ensure_allowed_keys(raw, _ALLOWED_ALLOW_KEYS, "plan.allow")

    tools = _parse_allow_list(raw.get("tools"), "plan.allow.tools")
    flows = _parse_allow_list(raw.get("flows"), "plan.allow.flows")
    return AllowList(tools=tuple(tools), flows=tuple(flows))


def _parse_allow_list(raw: Any, label: str) -> Tuple[str, ...]:
    if raw is None:
        return tuple()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PlanValidationError(f"{label} must be a list of strings")
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise PlanValidationError(f"{label} entries must be non-empty strings")
        normalized = item.strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return tuple(values)


def _parse_step(raw: Mapping[str, Any], allow: AllowList, index: int) -> PlanStep:
    _ensure_allowed_keys(raw, _ALLOWED_STEP_KEYS, f"plan.steps[{index}]")

    step_id = raw.get("id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise PlanValidationError(f"plan.steps[{index}].id must be a non-empty string")
    step_id = step_id.strip()

    kind = raw.get("kind")
    if kind not in _STEP_KINDS:
        raise PlanValidationError(f"plan.steps[{index}].kind must be one of {sorted(_STEP_KINDS)}")

    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise PlanValidationError(f"plan.steps[{index}].ref must be a non-empty string")
    ref = ref.strip()

    inputs = raw.get("inputs", {})
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, Mapping):
        raise PlanValidationError(f"plan.steps[{index}].inputs must be an object if provided")

    on_error = _parse_on_error(raw.get("on_error"), index)

    if kind == "tool" and ref not in allow.tools:
        raise PlanValidationError(f"plan.steps[{index}].ref '{ref}' not present in allow.tools")
    if kind == "flow" and ref not in allow.flows:
        raise PlanValidationError(f"plan.steps[{index}].ref '{ref}' not present in allow.flows")

    return PlanStep(id=step_id, kind=kind, ref=ref, inputs=dict(inputs), on_error=on_error)


def _parse_on_error(raw: Any, index: int) -> Optional[OnErrorAction]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlanValidationError(f"plan.steps[{index}].on_error must be an object if provided")
    _ensure_allowed_keys(raw, _ALLOWED_ON_ERROR_KEYS, f"plan.steps[{index}].on_error")

    retry = raw.get("retry")
    retry_policy: Optional[RetryPolicy] = None
    if retry is not None:
        if not isinstance(retry, Mapping):
            raise PlanValidationError(f"plan.steps[{index}].on_error.retry must be an object")
        _ensure_allowed_keys(retry, _ALLOWED_RETRY_KEYS, f"plan.steps[{index}].on_error.retry")
        max_attempts = retry.get("max")
        if not isinstance(max_attempts, int) or max_attempts < 0:
            raise PlanValidationError(f"plan.steps[{index}].on_error.retry.max must be a non-negative integer")
        backoff_ms = retry.get("backoff_ms")
        if backoff_ms is not None:
            if not isinstance(backoff_ms, int) or backoff_ms < 0:
                raise PlanValidationError(
                    f"plan.steps[{index}].on_error.retry.backoff_ms must be a non-negative integer"
                )
        retry_policy = RetryPolicy(max=max_attempts, backoff_ms=backoff_ms)

    fallback = raw.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, str) or not fallback.strip():
            raise PlanValidationError(f"plan.steps[{index}].on_error.fallback must be a non-empty string when provided")
        fallback = fallback.strip()

    return OnErrorAction(retry=retry_policy, fallback=fallback)


def _ensure_allowed_keys(obj: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    allowed_set = set(allowed)
    unknown = [key for key in obj.keys() if key not in allowed_set]
    if unknown:
        raise PlanValidationError(f"Unexpected fields in {label}: {', '.join(sorted(unknown))}")


__all__ = [
    "PLAN_VERSION",
    "Plan",
    "PlanConstraints",
    "PlanStep",
    "AllowList",
    "OnErrorAction",
    "RetryPolicy",
    "PlanValidationError",
    "validate_plan",
    "load_plan_json",
]
