"""v0.3 → v0.4 backward-compatibility regression (Stage 7-2.1).

Adding the ``IntentSession`` artifact kind must be *purely additive*: every
pre-existing artifact kind keeps verifying byte-identically, the body factory
keeps a body for every ArtifactType, and the new in-code schema slots in
alongside the existing ones without disturbing them.
"""

from __future__ import annotations

from typing import Any, Dict

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    AgentNetworkBody,
    verify,
)
from tm.artifacts.models import _BODY_FACTORY
from tm.artifacts.schema import SCHEMAS
from tm.server.versioning import _discover_schemas


def _agent_network_raw() -> Dict[str, Any]:
    return {
        "network_id": "network.demo.compat",
        "topology": "star",
        "center_bundle_ref": "bundle.center",
        "leaf_bundle_refs": ["bundle.leaf_a", "bundle.leaf_b"],
        "edges": [
            {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
            {"from": "bundle.leaf_b", "to": "bundle.center", "kpi_keys": ["kpi.beta"]},
        ],
        "transport_default": "inprocess",
    }


def _agent_network_candidate(body_raw: Dict[str, Any], *, version: str) -> Artifact:
    body = AgentNetworkBody.from_mapping(body_raw)
    env = ArtifactEnvelope(
        artifact_id=f"network.{body.network_id}",
        status=ArtifactStatus.CANDIDATE,
        artifact_type=ArtifactType.AGENT_NETWORK,
        version=version,
        created_by="tester",
        created_at="2026-06-13T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )
    return Artifact(envelope=env, body=body, body_raw=body_raw)


def test_v03_agent_network_still_verifies_under_v04() -> None:
    candidate = _agent_network_candidate(_agent_network_raw(), version="v0.3")
    accepted, report = verify(candidate)
    assert accepted is not None, report.errors
    assert report.errors == []


def test_v03_and_v04_body_hash_identical() -> None:
    """The new enum value must not perturb the canonical body hash of v0.3 kinds."""
    body_raw = _agent_network_raw()
    acc_v03, _ = verify(_agent_network_candidate(body_raw, version="v0.3"))
    acc_v04, _ = verify(_agent_network_candidate(body_raw, version="v0.4"))
    assert acc_v03 is not None and acc_v04 is not None
    assert acc_v03.envelope.body_hash == acc_v04.envelope.body_hash


def test_every_artifact_type_has_a_body_factory() -> None:
    for artifact_type in ArtifactType:
        assert artifact_type in _BODY_FACTORY, f"no body registered for {artifact_type}"


def test_intent_session_added_without_dropping_existing_schemas() -> None:
    # Pre-v0.4 in-code schemas remain present...
    for name in ("AgentNetworkSpec", "IntentSpec", "PropertyPatternSpec", "ProofReportSpec"):
        assert name in SCHEMAS
    # ...and the new one is registered alongside them.
    assert "IntentSessionSpec" in SCHEMAS


def test_meta_advertises_both_old_and_new_schemas() -> None:
    discovered = set(_discover_schemas())
    assert "agent_network@v0" in discovered
    assert "intent_session@v0" in discovered
