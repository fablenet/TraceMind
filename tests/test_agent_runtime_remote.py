"""Tests for the new ``AgentRuntime.kind = 'remote'`` configuration — Phase 6 Stage 6-2.2.

Validates both the Python dataclass (``AgentRuntime.from_mapping``) and the
JSON Schema in ``tm/agents/schemas/agent_runtime.json``. The inplace
inlined definition under ``$defs/agent_runtime`` of ``agent_bundle.json``
is checked for parity by a dedicated test.

Additivity is paramount: every existing ``kind="inprocess"`` shape must
parse + validate unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from jsonschema import Draft202012Validator

from tm.agents.models import AgentRuntime, RetryPolicySpec

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "tm" / "agents" / "schemas" / "agent_runtime.json"
BUNDLE_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "tm" / "artifacts" / "schemas" / "v0" / "agent_bundle.json"
)


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _minimal_inprocess() -> Dict[str, Any]:
    return {"kind": "inprocess", "config": {}}


def _minimal_remote(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "kind": "remote",
        "config": {},
        "endpoint": "https://leaf-1.example.test:8080",
        "transport_kind": "http",
    }
    payload.update(overrides)
    return payload


# ─── Backward-compatible inprocess ────────────────────────────────


class TestInprocessBackwardCompat:
    def test_minimal_inprocess_parses(self) -> None:
        runtime = AgentRuntime.from_mapping(_minimal_inprocess())
        assert runtime.kind == "inprocess"
        assert runtime.config == {}
        assert runtime.endpoint is None
        assert runtime.transport_kind is None
        assert runtime.retry_policy is None
        assert runtime.timeout_ms is None
        assert runtime.auth_ref is None
        assert runtime.is_remote() is False

    def test_minimal_inprocess_validates_against_schema(self) -> None:
        v = _validator()
        v.validate(_minimal_inprocess())

    def test_inprocess_with_config_only(self) -> None:
        payload = {"kind": "inprocess", "config": {"threads": 4}}
        runtime = AgentRuntime.from_mapping(payload)
        assert runtime.config == {"threads": 4}
        _validator().validate(payload)


# ─── Minimal remote ───────────────────────────────────────────────


class TestRemoteMinimal:
    def test_minimal_remote_parses(self) -> None:
        runtime = AgentRuntime.from_mapping(_minimal_remote())
        assert runtime.kind == "remote"
        assert runtime.endpoint == "https://leaf-1.example.test:8080"
        assert runtime.transport_kind == "http"
        assert runtime.retry_policy is None
        assert runtime.is_remote() is True

    def test_minimal_remote_validates_against_schema(self) -> None:
        v = _validator()
        v.validate(_minimal_remote())


# ─── Required-field enforcement for remote ────────────────────────


class TestRemoteRequiredFields:
    def test_remote_without_endpoint_rejected_by_dataclass(self) -> None:
        payload = _minimal_remote()
        payload.pop("endpoint")
        with pytest.raises(ValueError, match="endpoint is required"):
            AgentRuntime.from_mapping(payload)

    def test_remote_without_transport_kind_rejected_by_dataclass(self) -> None:
        payload = _minimal_remote()
        payload.pop("transport_kind")
        with pytest.raises(ValueError, match="transport_kind is required"):
            AgentRuntime.from_mapping(payload)

    def test_remote_without_endpoint_rejected_by_schema(self) -> None:
        v = _validator()
        payload = _minimal_remote()
        payload.pop("endpoint")
        with pytest.raises(Exception):
            v.validate(payload)

    def test_remote_without_transport_kind_rejected_by_schema(self) -> None:
        v = _validator()
        payload = _minimal_remote()
        payload.pop("transport_kind")
        with pytest.raises(Exception):
            v.validate(payload)


# ─── Transport-kind enum constraint ───────────────────────────────


class TestTransportKindEnum:
    @pytest.mark.parametrize("kind", ["inprocess", "http", "file_queue"])
    def test_accepted_transport_kinds(self, kind: str) -> None:
        payload = _minimal_remote(transport_kind=kind)
        runtime = AgentRuntime.from_mapping(payload)
        assert runtime.transport_kind == kind
        _validator().validate(payload)

    def test_unknown_transport_kind_rejected_by_dataclass(self) -> None:
        with pytest.raises(ValueError, match="transport_kind"):
            AgentRuntime.from_mapping(_minimal_remote(transport_kind="grpc"))

    def test_unknown_transport_kind_rejected_by_schema(self) -> None:
        v = _validator()
        with pytest.raises(Exception):
            v.validate(_minimal_remote(transport_kind="grpc"))


# ─── Retry policy spec ────────────────────────────────────────────


class TestRetryPolicy:
    def test_defaults_applied_when_omitted(self) -> None:
        spec = RetryPolicySpec.from_mapping(None)
        assert spec.max_attempts == 3
        assert spec.base_backoff_s == pytest.approx(0.1)
        assert spec.max_backoff_s == pytest.approx(2.0)

    def test_overrides_applied(self) -> None:
        spec = RetryPolicySpec.from_mapping({"max_attempts": 5, "base_backoff_s": 0.2, "max_backoff_s": 1.5})
        assert spec.max_attempts == 5
        assert spec.base_backoff_s == pytest.approx(0.2)
        assert spec.max_backoff_s == pytest.approx(1.5)

    def test_retry_policy_on_remote_runtime(self) -> None:
        payload = _minimal_remote(retry_policy={"max_attempts": 5})
        runtime = AgentRuntime.from_mapping(payload)
        assert isinstance(runtime.retry_policy, RetryPolicySpec)
        assert runtime.retry_policy.max_attempts == 5
        _validator().validate(payload)

    def test_retry_policy_unknown_field_rejected_by_schema(self) -> None:
        v = _validator()
        with pytest.raises(Exception):
            v.validate(_minimal_remote(retry_policy={"backoff_strategy": "linear"}))


# ─── timeout_ms ──────────────────────────────────────────────────


class TestTimeoutMs:
    def test_valid_timeout(self) -> None:
        payload = _minimal_remote(timeout_ms=5000)
        runtime = AgentRuntime.from_mapping(payload)
        assert runtime.timeout_ms == 5000
        _validator().validate(payload)

    def test_negative_timeout_rejected_by_dataclass(self) -> None:
        with pytest.raises(ValueError):
            AgentRuntime.from_mapping(_minimal_remote(timeout_ms=-1))

    def test_zero_timeout_rejected_by_dataclass(self) -> None:
        with pytest.raises(ValueError):
            AgentRuntime.from_mapping(_minimal_remote(timeout_ms=0))

    def test_string_timeout_rejected_by_dataclass(self) -> None:
        with pytest.raises(TypeError):
            AgentRuntime.from_mapping(_minimal_remote(timeout_ms="500"))

    def test_bool_timeout_rejected_by_dataclass(self) -> None:
        # ``True`` is technically ``int``, but a bool sneaking in as a
        # timeout is almost certainly a mistake.
        with pytest.raises(TypeError):
            AgentRuntime.from_mapping(_minimal_remote(timeout_ms=True))


# ─── auth_ref ────────────────────────────────────────────────────


class TestAuthRef:
    def test_auth_ref_optional(self) -> None:
        runtime = AgentRuntime.from_mapping(_minimal_remote())
        assert runtime.auth_ref is None

    def test_auth_ref_passthrough(self) -> None:
        payload = _minimal_remote(auth_ref="vault:secret/leaf-1/token")
        runtime = AgentRuntime.from_mapping(payload)
        assert runtime.auth_ref == "vault:secret/leaf-1/token"
        _validator().validate(payload)

    def test_empty_auth_ref_rejected_by_schema(self) -> None:
        v = _validator()
        with pytest.raises(Exception):
            v.validate(_minimal_remote(auth_ref=""))


# ─── Schema parity ───────────────────────────────────────────────


class TestSchemaParity:
    def test_standalone_and_inlined_definitions_match(self) -> None:
        standalone = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        bundle = json.loads(BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))
        inlined = bundle["$defs"]["agent_runtime"]
        # The standalone has $schema + $id + title + description not
        # present on the inlined version; compare the substantive shape.
        for key in ("type", "properties", "required", "additionalProperties", "allOf"):
            assert (
                standalone[key] == inlined[key]
            ), f"agent_runtime schema mismatch at key '{key}' between standalone and inlined"

    def test_unknown_top_level_field_rejected(self) -> None:
        v = _validator()
        with pytest.raises(Exception):
            v.validate({**_minimal_inprocess(), "extra_field": "nope"})


# ─── End-to-end via AgentBundle artifact ──────────────────────────


class TestE2EViaAgentBundle:
    """Spot-check that remote AgentRuntime payloads pass through the full
    ``AgentBundle`` JSON schema (not just the runtime sub-schema)."""

    def test_remote_runtime_inside_bundle(self) -> None:
        bundle_schema = json.loads(BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(bundle_schema)
        bundle_payload: Dict[str, Any] = {
            "bundle_id": "bundle-remote-1",
            "agents": [
                {
                    "agent_id": "leaf.observer",
                    "name": "leaf-observer",
                    "version": "0.0.1",
                    "runtime": _minimal_remote(timeout_ms=10000, retry_policy={"max_attempts": 5}),
                    "contract": {"inputs": [], "outputs": [], "effects": []},
                    "config_schema": {},
                    "evidence_outputs": [],
                }
            ],
            "plan": [{"step": "observe", "agent_id": "leaf.observer"}],
        }
        validator.validate(bundle_payload)
