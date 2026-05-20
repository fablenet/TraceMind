"""Tests for AgentNetwork ↔ Governance integration (Phase 6 Stage 6-1.6).

AgentNetwork artifacts are first-class K-Ontology v0.3 artifacts and must
flow through the same governance lifecycle as every other artifact kind:

  candidate  →  ``tm.artifacts.verify``  →  accepted

Plus: changes to an accepted AgentNetwork go through ``ProposedChangePlanBody``
(a PatchProposal), identical to how IntentBody / PropertyPattern changes
go through governance in v0.2.

DoD coverage (from .plan/phase-6-agent-network.md Stage 6-1.6):
- A clean candidate AgentNetwork verifies to ``accepted`` without errors
- Body hash is stamped on the envelope after successful verification
- Schema violations (malformed body) and lint violations (leaf-to-leaf edges,
  reserved tree topology, leaf-patches-center, etc.) cause verification
  failure with informative error messages
- A "new AgentNetwork" can be wrapped in a ProposedChangePlanBody and that
  proposal itself verifies as a proper candidate
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from tm.artifacts import (
    AgentNetworkBody,
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    ProposedChangePlanBody,
    verify,
)


def _envelope(network_id: str, *, status: str = "candidate", version: str = "v0.3") -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=f"network.{network_id}",
        status=ArtifactStatus(status),
        artifact_type=ArtifactType.AGENT_NETWORK,
        version=version,
        created_by="tester",
        created_at="2026-05-19T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )


def _clean_body_raw() -> Dict[str, Any]:
    return {
        "network_id": "network.demo.governance",
        "topology": "star",
        "center_bundle_ref": "bundle.center",
        "leaf_bundle_refs": ["bundle.leaf_a", "bundle.leaf_b"],
        "edges": [
            {
                "from": "bundle.leaf_a",
                "to": "bundle.center",
                "kpi_keys": ["kpi.alpha"],
            },
            {
                "from": "bundle.leaf_b",
                "to": "bundle.center",
                "kpi_keys": ["kpi.beta"],
            },
        ],
        "transport_default": "http",
    }


def _candidate(body_raw: Dict[str, Any]) -> Artifact:
    body = AgentNetworkBody.from_mapping(body_raw)
    return Artifact(envelope=_envelope(body.network_id), body=body, body_raw=body_raw)


# ─── candidate → accepted lifecycle ────────────────────────────────


class TestAgentNetworkAcceptedByGovernance:
    def test_clean_candidate_verifies(self) -> None:
        candidate = _candidate(_clean_body_raw())
        accepted, report = verify(candidate)
        assert accepted is not None
        assert report.errors == []
        assert accepted.envelope.status == ArtifactStatus.ACCEPTED

    def test_body_hash_stamped_on_envelope(self) -> None:
        candidate = _candidate(_clean_body_raw())
        accepted, _report = verify(candidate)
        assert accepted is not None
        assert len(accepted.envelope.body_hash) == 64
        assert accepted.envelope.meta["hashes"]["body_hash"] == accepted.envelope.body_hash

    def test_idempotent_under_reverify_returns_same_hash(self) -> None:
        body_raw = _clean_body_raw()
        cand1 = _candidate(body_raw)
        acc1, _ = verify(cand1)
        assert acc1 is not None
        h1 = acc1.envelope.body_hash

        cand2 = _candidate(body_raw)
        acc2, _ = verify(cand2)
        assert acc2 is not None
        assert acc2.envelope.body_hash == h1


# ─── Rejection paths ────────────────────────────────────────────────


class TestGovernanceRejectsInvalid:
    def test_rejects_non_candidate_status(self) -> None:
        body_raw = _clean_body_raw()
        body = AgentNetworkBody.from_mapping(body_raw)
        env = _envelope(body.network_id, status="accepted")
        artifact = Artifact(envelope=env, body=body, body_raw=body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        assert any("status" in err for err in report.errors)

    def test_rejects_unsupported_version(self) -> None:
        body_raw = _clean_body_raw()
        body = AgentNetworkBody.from_mapping(body_raw)
        env = _envelope(body.network_id, version="v1")
        artifact = Artifact(envelope=env, body=body, body_raw=body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        assert any("version" in err for err in report.errors)

    def test_rejects_tree_topology(self) -> None:
        # `tree` is reserved in the enum but explicitly NOT implemented in v0.3.
        # The dataclass accepts it (enum is open), but the verifier must reject it.
        body_raw = _clean_body_raw()
        body_raw["topology"] = "tree"
        body = AgentNetworkBody.from_mapping(body_raw)
        artifact = Artifact(envelope=_envelope(body.network_id), body=body, body_raw=body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        assert any("AN_TOPOLOGY_UNSUPPORTED" in err for err in report.errors)

    def test_rejects_leaf_to_leaf_edge(self) -> None:
        body_raw = _clean_body_raw()
        body_raw["edges"].append({"from": "bundle.leaf_a", "to": "bundle.leaf_b", "kpi_keys": ["kpi.gossip"]})
        artifact = _candidate(body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        assert any("AN_EDGE_LEAF_TO_LEAF" in err for err in report.errors)

    def test_rejects_leaf_patches_center(self) -> None:
        body_raw = _clean_body_raw()
        body_raw["edges"][0]["allowed_patches"] = ["policy_override"]
        artifact = _candidate(body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        assert any("AN_EDGE_LEAF_PATCHES_CENTER" in err for err in report.errors)

    def test_rejects_center_in_leaves(self) -> None:
        body_raw = _clean_body_raw()
        body_raw["leaf_bundle_refs"].append("bundle.center")
        body_raw["edges"].append({"from": "bundle.center", "to": "bundle.center", "kpi_keys": ["kpi.self"]})
        # AgentNetworkBody.from_mapping is permissive (it just dataclass-parses);
        # the verifier rejects via the lint pass.
        artifact = _candidate(body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        codes_found = [code for err in report.errors for code in ("AN_CENTER_IN_LEAVES",) if code in err]
        assert "AN_CENTER_IN_LEAVES" in codes_found

    def test_rejects_invalid_kpi_name(self) -> None:
        # The schema layer (first) catches this via the pattern constraint;
        # the lint layer (second) would also catch it as AN_EDGE_KPI_NAME.
        # Either path is acceptable governance — verification must reject.
        body_raw = _clean_body_raw()
        body_raw["edges"][0]["kpi_keys"] = ["KPI.upper_case"]
        artifact = _candidate(body_raw)
        accepted, report = verify(artifact)
        assert accepted is None
        assert any("kpi_keys" in err or "AN_EDGE_KPI_NAME" in err for err in report.errors), report.errors

    def test_warning_only_does_not_block(self) -> None:
        # A leaf with no edge to the center is a *warning*, not an error —
        # verification must succeed.
        body_raw = _clean_body_raw()
        body_raw["edges"] = [
            {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
        ]
        # bundle.leaf_b has no edge — warning only.
        artifact = _candidate(body_raw)
        accepted, report = verify(artifact)
        assert accepted is not None, f"warning-only network must be accepted; errors={report.errors}"


# ─── New AgentNetwork via ProposedChangePlanBody ────────────────────


class TestAgentNetworkChangeAsProposal:
    def test_new_network_proposal_validates(self) -> None:
        """Adding a new AgentNetwork to the registry is a governable change."""
        new_network_body = _clean_body_raw()

        proposal_raw = {
            "plan_id": "patch.add_agent_network.v1",
            "intent_id": "intent.governance.new_network",
            "summary": "Introduce cross-domain governance star",
            "decisions": [
                {
                    "effect_ref": "registry/networks/network.demo.governance.yaml",
                    "target_state": {
                        "operation": "create_agent_network",
                        "body": new_network_body,
                    },
                    "idempotency_key": "add.network.demo.governance.v1",
                    "reasoning_trace": "Operator proposed via tm network propose",
                }
            ],
            "llm_metadata": {
                "model": "none",
                "prompt_hash": "n/a",
                "determinism_hint": "deterministic",
            },
            "policy_requirements": ["review.required"],
        }

        body = ProposedChangePlanBody.from_mapping(proposal_raw)
        envelope = ArtifactEnvelope(
            artifact_id="patch.add_agent_network.v1",
            status=ArtifactStatus.CANDIDATE,
            artifact_type=ArtifactType.PROPOSED_CHANGE_PLAN,
            version="v0.3",
            created_by="tester",
            created_at="2026-05-19T00:00:00Z",
            body_hash="",
            envelope_hash="",
            meta={},
        )
        artifact = Artifact(envelope=envelope, body=body, body_raw=proposal_raw)
        accepted, report = verify(artifact)
        assert accepted is not None, f"proposal rejected: {report.errors}"
        assert report.errors == []


# ─── Parametric coverage of each blocking issue code ────────────────


@pytest.mark.parametrize(
    "mutate, expected_code",
    [
        (lambda b: b.__setitem__("topology", "tree"), "AN_TOPOLOGY_UNSUPPORTED"),
        (
            lambda b: b["edges"].append({"from": "bundle.leaf_a", "to": "bundle.leaf_b", "kpi_keys": ["kpi.x"]}),
            "AN_EDGE_LEAF_TO_LEAF",
        ),
        (
            lambda b: b["edges"][0].__setitem__("allowed_patches", ["policy_override"]),
            "AN_EDGE_LEAF_PATCHES_CENTER",
        ),
    ],
)
def test_each_blocking_code_blocks_verification(mutate, expected_code: str) -> None:
    body_raw = _clean_body_raw()
    mutate(body_raw)
    artifact = _candidate(body_raw)
    accepted, report = verify(artifact)
    assert accepted is None
    assert any(
        expected_code in err for err in report.errors
    ), f"expected {expected_code} in errors; got {report.errors}"
