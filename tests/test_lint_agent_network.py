"""Tests for ``lint_agent_network`` — K-Ontology v0.3 / Phase 6 Stage 6-1.4.

Covers every issue code defined in
``tm/lint/agent_network_lint.py``::

    AN_TOPOLOGY_UNSUPPORTED    AN_CENTER_IN_LEAVES       AN_LEAF_EMPTY
    AN_LEAF_DUPLICATE          AN_EDGE_EMPTY             AN_EDGE_UNKNOWN_NODE
    AN_EDGE_LEAF_TO_LEAF       AN_EDGE_SELF_LOOP         AN_EDGE_LEAF_PATCHES_CENTER
    AN_EDGE_KPI_EMPTY          AN_EDGE_KPI_NAME          AN_EDGE_TRANSPORT_UNKNOWN
    AN_LEAF_MISSING_EDGE
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from tm.artifacts import AgentNetworkBody
from tm.lint import lint_agent_network


def _minimal() -> Dict[str, Any]:
    return {
        "network_id": "network.demo.v1",
        "topology": "star",
        "center_bundle_ref": "bundle.center",
        "leaf_bundle_refs": ["bundle.leaf_a"],
        "edges": [
            {
                "from": "bundle.leaf_a",
                "to": "bundle.center",
                "kpi_keys": ["kpi.alpha"],
            },
        ],
        "transport_default": "http",
    }


def _two_leaf() -> Dict[str, Any]:
    return {
        "network_id": "network.demo.two_leaf",
        "topology": "star",
        "center_bundle_ref": "bundle.center",
        "leaf_bundle_refs": ["bundle.leaf_a", "bundle.leaf_b"],
        "edges": [
            {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
            {"from": "bundle.leaf_b", "to": "bundle.center", "kpi_keys": ["kpi.beta"]},
        ],
        "transport_default": "http",
    }


def _codes(issues: List[Any]) -> List[str]:
    return [issue.code for issue in issues]


class TestCleanBaseline:
    def test_minimal_passes(self) -> None:
        assert lint_agent_network(_minimal()) == []

    def test_two_leaf_passes(self) -> None:
        assert lint_agent_network(_two_leaf()) == []

    def test_dataclass_form_passes(self) -> None:
        body = AgentNetworkBody.from_mapping(_two_leaf())
        assert lint_agent_network(body) == []

    def test_full_body_with_optional_fields_passes(self) -> None:
        payload = _two_leaf()
        payload["description"] = "Cross-domain governance star"
        payload["metadata"] = {"owner": "platform-team"}
        for edge in payload["edges"]:
            edge["description"] = "leaf reports up"
            edge["transport"] = "http"
            edge["allowed_patches"] = []
        assert lint_agent_network(payload) == []


class TestTopologyUnsupported:
    def test_tree_is_reserved_but_not_implemented(self) -> None:
        payload = _minimal()
        payload["topology"] = "tree"
        codes = _codes(lint_agent_network(payload))
        assert "AN_TOPOLOGY_UNSUPPORTED" in codes

    def test_star_is_accepted(self) -> None:
        codes = _codes(lint_agent_network(_minimal()))
        assert "AN_TOPOLOGY_UNSUPPORTED" not in codes


class TestCenterInLeaves:
    def test_center_in_leaves_rejected(self) -> None:
        payload = _minimal()
        payload["leaf_bundle_refs"] = ["bundle.center", "bundle.leaf_a"]
        payload["edges"].append({"from": "bundle.center", "to": "bundle.center", "kpi_keys": ["kpi.self"]})
        codes = _codes(lint_agent_network(payload))
        assert "AN_CENTER_IN_LEAVES" in codes


class TestLeafEmpty:
    def test_empty_leaves_rejected(self) -> None:
        payload = _minimal()
        payload["leaf_bundle_refs"] = []
        codes = _codes(lint_agent_network(payload))
        assert "AN_LEAF_EMPTY" in codes


class TestLeafDuplicate:
    def test_duplicate_leaf_rejected(self) -> None:
        payload = _two_leaf()
        payload["leaf_bundle_refs"] = ["bundle.leaf_a", "bundle.leaf_a"]
        codes = _codes(lint_agent_network(payload))
        assert "AN_LEAF_DUPLICATE" in codes

    def test_three_leaves_one_duplicate(self) -> None:
        payload = _two_leaf()
        payload["leaf_bundle_refs"] = ["bundle.leaf_a", "bundle.leaf_b", "bundle.leaf_a"]
        payload["edges"].append({"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.dup"]})
        codes = _codes(lint_agent_network(payload))
        assert codes.count("AN_LEAF_DUPLICATE") >= 1


class TestEdgeEmpty:
    def test_no_edges_rejected(self) -> None:
        payload = _minimal()
        payload["edges"] = []
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_EMPTY" in codes


class TestEdgeUnknownNode:
    def test_from_not_in_topology(self) -> None:
        payload = _minimal()
        payload["edges"][0]["from"] = "bundle.ghost"
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_UNKNOWN_NODE" in codes

    def test_to_not_in_topology(self) -> None:
        payload = _minimal()
        payload["edges"][0]["to"] = "bundle.ghost"
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_UNKNOWN_NODE" in codes


class TestEdgeLeafToLeaf:
    def test_leaf_to_leaf_rejected_in_star(self) -> None:
        payload = _two_leaf()
        payload["edges"].append({"from": "bundle.leaf_a", "to": "bundle.leaf_b", "kpi_keys": ["kpi.gossip"]})
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_LEAF_TO_LEAF" in codes

    def test_no_leaf_to_leaf_in_minimal(self) -> None:
        assert "AN_EDGE_LEAF_TO_LEAF" not in _codes(lint_agent_network(_minimal()))


class TestEdgeSelfLoop:
    def test_self_loop_rejected(self) -> None:
        payload = _minimal()
        payload["edges"][0]["to"] = payload["edges"][0]["from"]
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_SELF_LOOP" in codes


class TestEdgeLeafPatchesCenter:
    def test_leaf_to_center_with_patches_rejected(self) -> None:
        payload = _minimal()
        payload["edges"][0]["allowed_patches"] = ["policy_override"]
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_LEAF_PATCHES_CENTER" in codes

    def test_center_to_leaf_with_patches_accepted(self) -> None:
        payload = _minimal()
        payload["edges"].append(
            {
                "from": "bundle.center",
                "to": "bundle.leaf_a",
                "kpi_keys": ["kpi.patch_target"],
                "allowed_patches": ["policy_override"],
            }
        )
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_LEAF_PATCHES_CENTER" not in codes


class TestEdgeKpiEmpty:
    def test_empty_kpi_rejected(self) -> None:
        payload = _minimal()
        payload["edges"][0]["kpi_keys"] = []
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_KPI_EMPTY" in codes


class TestEdgeKpiName:
    @pytest.mark.parametrize(
        "bad_kpi",
        ["KPI.upper", "1leading_digit", "kpi space", "kpi-with-dash"],
    )
    def test_kpi_pattern_violations(self, bad_kpi: str) -> None:
        payload = _minimal()
        payload["edges"][0]["kpi_keys"] = [bad_kpi]
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_KPI_NAME" in codes

    def test_dotted_lowercase_accepted(self) -> None:
        payload = _minimal()
        payload["edges"][0]["kpi_keys"] = ["kpi.alpha", "kpi.beta_2"]
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_KPI_NAME" not in codes


class TestEdgeTransportUnknown:
    def test_unknown_transport_rejected(self) -> None:
        payload = _minimal()
        payload["edges"][0]["transport"] = "carrier_pigeon"
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_TRANSPORT_UNKNOWN" in codes

    @pytest.mark.parametrize("transport", ["inprocess", "http", "file_queue"])
    def test_supported_transports_accepted(self, transport: str) -> None:
        payload = _minimal()
        payload["edges"][0]["transport"] = transport
        codes = _codes(lint_agent_network(payload))
        assert "AN_EDGE_TRANSPORT_UNKNOWN" not in codes


class TestLeafMissingEdge:
    def test_warns_when_leaf_has_no_edge_to_center(self) -> None:
        payload = _two_leaf()
        payload["edges"] = [
            {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
        ]
        issues = lint_agent_network(payload)
        codes = [issue.code for issue in issues]
        assert "AN_LEAF_MISSING_EDGE" in codes
        warn_issue = next(issue for issue in issues if issue.code == "AN_LEAF_MISSING_EDGE")
        assert warn_issue.severity == "warning"

    def test_no_warning_when_all_leaves_have_edge(self) -> None:
        codes = _codes(lint_agent_network(_two_leaf()))
        assert "AN_LEAF_MISSING_EDGE" not in codes


class TestMultipleIssues:
    def test_collects_all_violations(self) -> None:
        payload = _minimal()
        payload["topology"] = "tree"
        payload["leaf_bundle_refs"] = ["bundle.center", "bundle.center"]
        payload["edges"] = [
            {"from": "bundle.center", "to": "bundle.center", "kpi_keys": []},
        ]
        codes = _codes(lint_agent_network(payload))
        for required in (
            "AN_TOPOLOGY_UNSUPPORTED",
            "AN_CENTER_IN_LEAVES",
            "AN_LEAF_DUPLICATE",
            "AN_EDGE_SELF_LOOP",
            "AN_EDGE_KPI_EMPTY",
        ):
            assert required in codes, f"missing expected code {required}; got {codes}"
