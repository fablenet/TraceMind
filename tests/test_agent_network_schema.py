"""Tests for AgentNetwork v0.3 — schema, AST validator, and dataclass body.

Covers Stage 6-1.2 of Phase 6: the new ``kind: AgentNetwork`` artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tm.artifacts import (
    AgentNetworkBody,
    AgentNetworkEdge,
    ArtifactType,
    ArtifactValidationError,
    validate_agent_network_spec,
)

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "tm" / "artifacts" / "schemas" / "v0"


@pytest.fixture(scope="module")
def file_schema_validator() -> Draft202012Validator:
    payload = json.loads((_SCHEMAS_DIR / "agent_network.json").read_text())
    return Draft202012Validator(payload, format_checker=FormatChecker())


def _minimal_payload() -> Dict[str, Any]:
    return {
        "network_id": "network.demo.v1",
        "topology": "star",
        "center_bundle_ref": "bundle.center",
        "leaf_bundle_refs": ["bundle.leaf_a"],
        "edges": [
            {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
        ],
        "transport_default": "http",
    }


def _full_payload() -> Dict[str, Any]:
    return {
        "network_id": "network.cross_domain.demo",
        "topology": "star",
        "center_bundle_ref": "bundle.governance.center",
        "leaf_bundle_refs": ["bundle.anti_sybil", "bundle.k8s_hpa_fairness"],
        "edges": [
            {
                "from": "bundle.anti_sybil",
                "to": "bundle.governance.center",
                "kpi_keys": ["sybil_burst_rate", "quarantine_pending_count"],
                "allowed_patches": [],
                "transport": "http",
                "description": "Anti-sybil leaf reports up",
            },
            {
                "from": "bundle.k8s_hpa_fairness",
                "to": "bundle.governance.center",
                "kpi_keys": ["tenant_starvation_seconds"],
                "transport": "file_queue",
            },
            {
                "from": "bundle.governance.center",
                "to": "bundle.anti_sybil",
                "kpi_keys": ["policy.target"],
                "allowed_patches": ["policy_override"],
            },
        ],
        "transport_default": "http",
        "description": "Cross-domain governance star.",
        "metadata": {"owner": "phase6.demo"},
    }


class TestFileLevelSchema:
    def test_accepts_minimal(self, file_schema_validator: Draft202012Validator) -> None:
        errors = list(file_schema_validator.iter_errors(_minimal_payload()))
        assert errors == []

    def test_accepts_full(self, file_schema_validator: Draft202012Validator) -> None:
        errors = list(file_schema_validator.iter_errors(_full_payload()))
        assert errors == []

    @pytest.mark.parametrize(
        "missing_field",
        [
            "network_id",
            "topology",
            "center_bundle_ref",
            "leaf_bundle_refs",
            "edges",
            "transport_default",
        ],
    )
    def test_rejects_missing_required(self, file_schema_validator: Draft202012Validator, missing_field: str) -> None:
        payload = _minimal_payload()
        del payload[missing_field]
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors, f"expected schema error for missing {missing_field}"

    def test_rejects_unknown_topology(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["topology"] = "mesh"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_accepts_tree_topology_at_file_level(self, file_schema_validator: Draft202012Validator) -> None:
        # File schema permits `tree` (reserved) — lint + verifier reject it.
        payload = _minimal_payload()
        payload["topology"] = "tree"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors == []

    def test_rejects_unknown_transport(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["transport_default"] = "carrier_pigeon"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_empty_leaves(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["leaf_bundle_refs"] = []
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_empty_edges(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["edges"] = []
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_edge_missing_kpi(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["edges"][0]["kpi_keys"] = []
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_unknown_network_id_pattern(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["network_id"] = "Network With Spaces"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_additional_property(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["unexpected"] = "field"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_edge_additional_property(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["edges"][0]["unknown"] = "field"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors


class TestAstValidator:
    def test_validate_agent_network_spec_minimal(self) -> None:
        validate_agent_network_spec(_minimal_payload())

    def test_validate_agent_network_spec_full(self) -> None:
        validate_agent_network_spec(_full_payload())

    def test_validate_rejects_missing_required(self) -> None:
        payload = _minimal_payload()
        del payload["edges"]
        with pytest.raises(ArtifactValidationError):
            validate_agent_network_spec(payload)

    def test_validate_rejects_unknown_topology(self) -> None:
        payload = _minimal_payload()
        payload["topology"] = "mesh"
        with pytest.raises(ArtifactValidationError):
            validate_agent_network_spec(payload)


class TestDataclassRoundTrip:
    def test_minimal_round_trip(self) -> None:
        body = AgentNetworkBody.from_mapping(_minimal_payload())
        assert body.artifact_type == ArtifactType.AGENT_NETWORK
        assert body.network_id == "network.demo.v1"
        assert body.topology == "star"
        assert body.center_bundle_ref == "bundle.center"
        assert body.leaf_bundle_refs == ["bundle.leaf_a"]
        assert len(body.edges) == 1
        assert isinstance(body.edges[0], AgentNetworkEdge)
        assert body.edges[0].source == "bundle.leaf_a"
        assert body.edges[0].target == "bundle.center"
        assert body.edges[0].kpi_keys == ["kpi.alpha"]
        assert body.edges[0].allowed_patches == []
        assert body.edges[0].transport is None
        assert body.transport_default == "http"
        assert body.description is None
        assert body.metadata == {}

    def test_full_round_trip(self) -> None:
        body = AgentNetworkBody.from_mapping(_full_payload())
        assert body.network_id == "network.cross_domain.demo"
        assert len(body.edges) == 3
        assert body.edges[2].source == "bundle.governance.center"
        assert body.edges[2].allowed_patches == ["policy_override"]
        assert body.edges[1].transport == "file_queue"
        assert body.metadata == {"owner": "phase6.demo"}
        assert body.description.startswith("Cross-domain")

    def test_rejects_unknown_topology(self) -> None:
        payload = _minimal_payload()
        payload["topology"] = "mesh"
        with pytest.raises(ValueError, match="topology"):
            AgentNetworkBody.from_mapping(payload)

    def test_rejects_unknown_transport_default(self) -> None:
        payload = _minimal_payload()
        payload["transport_default"] = "carrier_pigeon"
        with pytest.raises(ValueError, match="transport_default"):
            AgentNetworkBody.from_mapping(payload)

    def test_rejects_missing_edges(self) -> None:
        payload = _minimal_payload()
        del payload["edges"]
        with pytest.raises(ValueError, match="edges"):
            AgentNetworkBody.from_mapping(payload)

    def test_rejects_empty_edges(self) -> None:
        payload = _minimal_payload()
        payload["edges"] = []
        with pytest.raises(ValueError, match="edges"):
            AgentNetworkBody.from_mapping(payload)

    def test_rejects_missing_leaves(self) -> None:
        payload = _minimal_payload()
        del payload["leaf_bundle_refs"]
        with pytest.raises(ValueError, match="leaf_bundle_refs"):
            AgentNetworkBody.from_mapping(payload)

    def test_edge_kpi_keys_required_nonempty(self) -> None:
        payload = _minimal_payload()
        payload["edges"][0]["kpi_keys"] = []
        with pytest.raises(ValueError, match="kpi_keys"):
            AgentNetworkBody.from_mapping(payload)
