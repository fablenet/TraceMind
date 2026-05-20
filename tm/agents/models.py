from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Mapping, Sequence


def _require_field(data: Mapping[str, Any], key: str) -> Any:
    if key not in data or data[key] is None:
        raise ValueError(f"missing required field: '{key}'")
    return data[key]


def _ensure_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return dict(value)


def _ensure_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _ensure_sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"{name} must be a sequence")
    return value


def _force_list(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"{name} must be a list of strings")
    return [str(item) for item in value]


def _ensure_schema(value: Any, name: str) -> Mapping[str, Any] | str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{name} must be a string or mapping")


def _ensure_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


@dataclass
class IORef:
    ref: str
    kind: str
    schema: Mapping[str, Any] | str
    required: bool
    mode: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IORef":
        return cls(
            ref=_ensure_str(_require_field(data, "ref"), "ref"),
            kind=_ensure_str(_require_field(data, "kind"), "kind"),
            schema=_ensure_schema(_require_field(data, "schema"), "schema"),
            required=_ensure_bool(_require_field(data, "required"), "required"),
            mode=_ensure_str(_require_field(data, "mode"), "mode"),
        )


@dataclass
class EffectIdempotency:
    type: str
    key_fields: List[str]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EffectIdempotency":
        key_fields = _force_list(data.get("key_fields"), "idempotency.key_fields")
        return cls(
            type=_ensure_str(_require_field(data, "type"), "idempotency.type"),
            key_fields=key_fields,
        )


@dataclass
class EffectRef:
    name: str
    kind: str
    target: str
    idempotency: EffectIdempotency
    rollback: str | None
    evidence: Dict[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EffectRef":
        evidence = _ensure_dict(_require_field(data, "evidence"), "evidence")
        rollback = data.get("rollback")
        return cls(
            name=_ensure_str(_require_field(data, "name"), "name"),
            kind=_ensure_str(_require_field(data, "kind"), "kind"),
            target=_ensure_str(_require_field(data, "target"), "target"),
            idempotency=EffectIdempotency.from_mapping(
                _ensure_dict(_require_field(data, "idempotency"), "idempotency")
            ),
            rollback=_ensure_str(rollback, "rollback") if rollback is not None else None,
            evidence=evidence,
        )


@dataclass
class AgentContract:
    inputs: List[IORef]
    outputs: List[IORef]
    effects: List[EffectRef]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentContract":
        inputs_raw = _ensure_sequence(_require_field(data, "inputs"), "contract.inputs")
        outputs_raw = _ensure_sequence(_require_field(data, "outputs"), "contract.outputs")
        effects_raw = _ensure_sequence(_require_field(data, "effects"), "contract.effects")
        inputs = [IORef.from_mapping(_ensure_dict(item, "contract.input")) for item in inputs_raw]
        outputs = [IORef.from_mapping(_ensure_dict(item, "contract.output")) for item in outputs_raw]
        effects = [EffectRef.from_mapping(_ensure_dict(item, "contract.effect")) for item in effects_raw]
        return cls(inputs=inputs, outputs=outputs, effects=effects)


@dataclass
class RetryPolicySpec:
    """Declarative retry policy for cross-node ``Transport`` calls.

    Mirrors :class:`tm.transport.http.RetryPolicy` at the schema layer so an
    AgentBundle YAML can declare its desired retry behavior without
    importing transport-specific Python types. The remote ``AgentRuntime``
    resolver (Stage 6-2.3) converts this spec into a live ``RetryPolicy``
    when wiring the transport.
    """

    max_attempts: int = 3
    base_backoff_s: float = 0.1
    max_backoff_s: float = 2.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RetryPolicySpec":
        if data is None:
            return cls()
        raw = _ensure_dict(data, "runtime.retry_policy")
        return cls(
            max_attempts=int(raw.get("max_attempts", cls.max_attempts)),
            base_backoff_s=float(raw.get("base_backoff_s", cls.base_backoff_s)),
            max_backoff_s=float(raw.get("max_backoff_s", cls.max_backoff_s)),
        )


@dataclass
class AgentRuntime:
    """Declarative runtime descriptor for an agent.

    Phase 5 shipped only ``kind="inprocess"``; the additional fields below
    are introduced in Phase 6 Stage 6-2.2 to describe ``kind="remote"``
    agents (those executed on another process / host via a ``Transport``).
    The ``inprocess`` case ignores the remote-specific fields, so existing
    YAMLs that only specify ``kind`` + ``config`` continue to parse
    unchanged (additive evolution).

    Validation invariants enforced by :meth:`from_mapping`:

    - ``kind == "remote"`` requires both ``endpoint`` and ``transport_kind``
    - ``transport_kind`` must be one of ``inprocess`` / ``http`` / ``file_queue``
    - ``timeout_ms`` (when provided) must be a positive integer
    """

    SUPPORTED_KINDS: ClassVar[frozenset[str]] = frozenset({"inprocess", "remote"})
    SUPPORTED_TRANSPORT_KINDS: ClassVar[frozenset[str]] = frozenset({"inprocess", "http", "file_queue"})

    kind: str
    config: Dict[str, Any]
    endpoint: str | None = None
    transport_kind: str | None = None
    retry_policy: RetryPolicySpec | None = None
    timeout_ms: int | None = None
    auth_ref: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentRuntime":
        config = _ensure_dict(_require_field(data, "config"), "runtime.config")
        kind = _ensure_str(_require_field(data, "kind"), "runtime.kind")
        endpoint = data.get("endpoint")
        transport_kind = data.get("transport_kind")
        retry_policy_raw = data.get("retry_policy")
        timeout_ms = data.get("timeout_ms")
        auth_ref = data.get("auth_ref")

        endpoint_str = _ensure_str(endpoint, "runtime.endpoint") if endpoint is not None else None
        transport_kind_str = (
            _ensure_str(transport_kind, "runtime.transport_kind") if transport_kind is not None else None
        )
        auth_ref_str = _ensure_str(auth_ref, "runtime.auth_ref") if auth_ref is not None else None
        retry_policy = RetryPolicySpec.from_mapping(retry_policy_raw) if retry_policy_raw is not None else None
        timeout_ms_int: int | None
        if timeout_ms is None:
            timeout_ms_int = None
        else:
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
                raise TypeError("runtime.timeout_ms must be an integer")
            if timeout_ms <= 0:
                raise ValueError("runtime.timeout_ms must be a positive integer")
            timeout_ms_int = timeout_ms

        if transport_kind_str is not None and transport_kind_str not in cls.SUPPORTED_TRANSPORT_KINDS:
            raise ValueError(
                f"runtime.transport_kind must be one of {sorted(cls.SUPPORTED_TRANSPORT_KINDS)}, "
                f"got '{transport_kind_str}'"
            )

        if kind == "remote":
            if not endpoint_str:
                raise ValueError("runtime.endpoint is required when runtime.kind == 'remote'")
            if not transport_kind_str:
                raise ValueError("runtime.transport_kind is required when runtime.kind == 'remote'")

        return cls(
            kind=kind,
            config=config,
            endpoint=endpoint_str,
            transport_kind=transport_kind_str,
            retry_policy=retry_policy,
            timeout_ms=timeout_ms_int,
            auth_ref=auth_ref_str,
        )

    def is_remote(self) -> bool:
        return self.kind == "remote"


@dataclass
class AgentEvidenceOutput:
    name: str
    description: str | None = None
    target: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentEvidenceOutput":
        return cls(
            name=_ensure_str(_require_field(data, "name"), "evidence_outputs.name"),
            description=(
                _ensure_str(data.get("description"), "evidence_outputs.description")
                if data.get("description") is not None
                else None
            ),
            target=(
                _ensure_str(data.get("target"), "evidence_outputs.target") if data.get("target") is not None else None
            ),
        )


@dataclass
class AgentSpec:
    agent_id: str
    name: str
    version: str
    runtime: AgentRuntime
    contract: AgentContract
    config_schema: Dict[str, Any]
    evidence_outputs: List[AgentEvidenceOutput]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentSpec":
        runtime = AgentRuntime.from_mapping(_ensure_dict(_require_field(data, "runtime"), "runtime"))
        contract = AgentContract.from_mapping(_ensure_dict(_require_field(data, "contract"), "contract"))
        config_schema = _ensure_dict(_require_field(data, "config_schema"), "config_schema")
        evidence_outputs_raw = _ensure_sequence(_require_field(data, "evidence_outputs"), "evidence_outputs")
        evidence_outputs = [
            AgentEvidenceOutput.from_mapping(_ensure_dict(output, "evidence_outputs"))
            for output in evidence_outputs_raw
        ]
        return cls(
            agent_id=_ensure_str(_require_field(data, "agent_id"), "agent_id"),
            name=_ensure_str(_require_field(data, "name"), "name"),
            version=_ensure_str(_require_field(data, "version"), "version"),
            runtime=runtime,
            contract=contract,
            config_schema=config_schema,
            evidence_outputs=evidence_outputs,
        )


__all__ = [
    "AgentContract",
    "AgentEvidenceOutput",
    "AgentRuntime",
    "AgentSpec",
    "EffectIdempotency",
    "EffectRef",
    "IORef",
    "RetryPolicySpec",
]
