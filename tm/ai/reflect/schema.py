from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

REFLECTION_VERSION = "reflect.v1"


class ReflectionValidationError(ValueError):
    """Raised when reflection payload does not conform to schema."""


@dataclass(frozen=True)
class PlanPatchOp:
    op: str
    path: str
    value: Any = field(default=None)

    def as_dict(self) -> Dict[str, Any]:
        data = {"op": self.op, "path": self.path}
        if self.op in {"add", "replace"} and "value" in self.__dict__:
            data["value"] = self.value
        if self.op == "remove" and "value" in data:
            data.pop("value", None)
        return data


@dataclass(frozen=True)
class PlanPatch:
    ops: Sequence[PlanPatchOp]

    def as_dict(self) -> Dict[str, Any]:
        return {"ops": [op.as_dict() for op in self.ops]}


@dataclass(frozen=True)
class Reflection:
    version: str
    summary: str
    issues: Sequence[str]
    guidance: Optional[str]
    plan_patch: Optional[PlanPatch]
    policy_update: Mapping[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "version": self.version,
            "summary": self.summary,
            "issues": list(self.issues),
            "policy_update": dict(self.policy_update),
        }
        if self.guidance is not None:
            data["guidance"] = self.guidance
        if self.plan_patch is not None:
            data["plan_patch"] = self.plan_patch.as_dict()
        return data


_ALLOWED_REFLECTION_KEYS = {"version", "summary", "issues", "guidance", "plan_patch", "policy_update"}
_ALLOWED_PLAN_PATCH_KEYS = {"ops"}
_ALLOWED_PATCH_OP_KEYS = {"op", "path", "value"}
_ALLOWED_PATCH_OPS = {"add", "replace", "remove"}
_ALLOWED_PATCH_FIELDS = {"inputs", "on_error"}


def validate_reflection(data: Mapping[str, Any]) -> Reflection:
    _ensure_allowed_keys(data, _ALLOWED_REFLECTION_KEYS, "reflection")

    version = data.get("version") or REFLECTION_VERSION
    if version != REFLECTION_VERSION:
        raise ReflectionValidationError(f"Unsupported reflection version: {version}")

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ReflectionValidationError("reflection.summary must be a non-empty string")

    issues_raw = data.get("issues", [])
    if not isinstance(issues_raw, Sequence) or isinstance(issues_raw, (str, bytes)):
        raise ReflectionValidationError("reflection.issues must be an array of strings")
    issues: list[str] = []
    for idx, item in enumerate(issues_raw):
        if not isinstance(item, str) or not item.strip():
            raise ReflectionValidationError(f"reflection.issues[{idx}] must be a non-empty string")
        issues.append(item.strip())

    guidance = data.get("guidance")
    if guidance is not None and not isinstance(guidance, str):
        raise ReflectionValidationError("reflection.guidance must be a string when provided")

    policy_update = data.get("policy_update", {})
    if not isinstance(policy_update, Mapping):
        raise ReflectionValidationError("reflection.policy_update must be an object")

    plan_patch = None
    if data.get("plan_patch") is not None:
        if not isinstance(data["plan_patch"], Mapping):
            raise ReflectionValidationError("reflection.plan_patch must be an object")
        plan_patch = _parse_plan_patch(data["plan_patch"])

    return Reflection(
        version=version,
        summary=summary.strip(),
        issues=tuple(issues),
        guidance=guidance.strip() if isinstance(guidance, str) else None,
        plan_patch=plan_patch,
        policy_update=dict(policy_update),
    )


def _parse_plan_patch(raw: Mapping[str, Any]) -> PlanPatch:
    _ensure_allowed_keys(raw, _ALLOWED_PLAN_PATCH_KEYS, "plan_patch")
    ops_raw = raw.get("ops")
    if not isinstance(ops_raw, Sequence) or isinstance(ops_raw, (str, bytes)) or not ops_raw:
        raise ReflectionValidationError("plan_patch.ops must be a non-empty array")

    ops: list[PlanPatchOp] = []
    for idx, item in enumerate(ops_raw):
        if not isinstance(item, Mapping):
            raise ReflectionValidationError(f"plan_patch.ops[{idx}] must be an object")
        _ensure_allowed_keys(item, _ALLOWED_PATCH_OP_KEYS, f"plan_patch.ops[{idx}]")
        op_name = item.get("op")
        if op_name not in _ALLOWED_PATCH_OPS:
            raise ReflectionValidationError(f"plan_patch.ops[{idx}].op must be one of {_ALLOWED_PATCH_OPS}")
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("/steps/"):
            raise ReflectionValidationError(
                "plan_patch path must target /steps/<index>/inputs or /steps/<index>/on_error"
            )
        segments = path.split("/")
        if len(segments) < 4:
            raise ReflectionValidationError("plan_patch path must include step index and field")
        try:
            int(segments[2])
        except ValueError as exc:
            raise ReflectionValidationError("plan_patch path must use numeric step index") from exc
        if segments[3] not in _ALLOWED_PATCH_FIELDS:
            raise ReflectionValidationError("plan_patch may only modify inputs or on_error")
        value = item.get("value")
        ops.append(PlanPatchOp(op=op_name, path=path, value=value))
    return PlanPatch(ops=tuple(ops))


def _ensure_allowed_keys(obj: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    invalid = [key for key in obj.keys() if key not in allowed]
    if invalid:
        raise ReflectionValidationError(f"Unexpected fields in {label}: {', '.join(sorted(invalid))}")


__all__ = [
    "REFLECTION_VERSION",
    "Reflection",
    "PlanPatch",
    "PlanPatchOp",
    "ReflectionValidationError",
    "validate_reflection",
]
