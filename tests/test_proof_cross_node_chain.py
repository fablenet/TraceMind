"""Cross-node proof chain tests — Phase 6 Stage 6-3.2."""

from __future__ import annotations

from tm.control.meta.proof import (
    EvidenceEntry,
    KripkeVerdict,
    ProofReport,
    ProofReportGenerator,
    Verdict,
    attach_peer_proofs,
    verify_peer_chain,
)


def _sample_proof(cycle_id: str, *, intent_id: str = "intent.test") -> ProofReport:
    gen = ProofReportGenerator(intent_id=intent_id)
    result = type(
        "CycleResult",
        (),
        {
            "bundle_artifact_id": cycle_id,
            "env_snapshot": type("S", (), {"body": {"environment": {"metrics": {"x": 1.0}}}})(),
            "execution_report": type(
                "E",
                (),
                {
                    "body": type(
                        "B",
                        (),
                        {"report_id": f"rep-{cycle_id}", "status": "ok", "artifact_refs": {}, "errors": []},
                    )()
                },
            )(),
            "policy_decisions": [],
            "start_time": "t0",
            "end_time": "t1",
        },
    )()
    return gen.generate(cycle_result=result, pre_snapshot={"environment": {"metrics": {"x": 1.0}}})


class TestAttachPeerProofs:
    def test_appends_peer_entries(self) -> None:
        center = _sample_proof("center-1")
        leaf_a = _sample_proof("leaf-a-1")
        leaf_b = _sample_proof("leaf-b-1")
        attach_peer_proofs(center, [("leaf.a", leaf_a), ("leaf.b", leaf_b)])
        peer_entries = [e for e in center.evidence_chain if e.event_type == "peer_proof_report"]
        assert len(peer_entries) == 2
        assert peer_entries[0].peer_node_id == "leaf.a"
        assert peer_entries[0].peer_chain_ref == leaf_a.report_hash

    def test_recomputes_center_hash(self) -> None:
        center = _sample_proof("center-1")
        before = center.report_hash
        leaf = _sample_proof("leaf-1")
        attach_peer_proofs(center, [("leaf.a", leaf)])
        assert center.report_hash != before

    def test_hash_includes_peer_refs(self) -> None:
        center = _sample_proof("center-1")
        leaf = _sample_proof("leaf-1")
        attach_peer_proofs(center, [("leaf.a", leaf)])
        hash_with_peer = center.report_hash
        center.evidence_chain = [e for e in center.evidence_chain if e.event_type != "peer_proof_report"]
        center.recompute_hash()
        assert center.report_hash != hash_with_peer

    def test_peer_fields_mirrored_in_data(self) -> None:
        center = _sample_proof("center-1")
        leaf = _sample_proof("leaf-1")
        attach_peer_proofs(center, [("leaf.a", leaf)])
        entry = center.evidence_chain[-1]
        assert entry.data["peer_node_id"] == "leaf.a"
        assert entry.data["peer_chain_ref"] == leaf.report_hash


class TestVerifyPeerChain:
    def _attached(self) -> tuple[ProofReport, dict[str, ProofReport]]:
        center = _sample_proof("center-1")
        leaves = {"leaf.a": _sample_proof("leaf-a-1"), "leaf.b": _sample_proof("leaf-b-1")}
        attach_peer_proofs(center, list(leaves.items()))
        return center, leaves

    def test_valid_chain_passes(self) -> None:
        center, leaves = self._attached()
        ok, errors = verify_peer_chain(center, leaves)
        assert ok
        assert errors == []

    def test_tampered_leaf_hash_fails(self) -> None:
        center, leaves = self._attached()
        leaves["leaf.a"].report_hash = "tampered"
        ok, errors = verify_peer_chain(center, leaves)
        assert not ok
        assert any("mismatch" in e for e in errors)

    def test_missing_peer_entry_fails(self) -> None:
        center, leaves = self._attached()
        center.evidence_chain = [e for e in center.evidence_chain if e.peer_node_id != "leaf.a"]
        ok, errors = verify_peer_chain(center, leaves)
        assert not ok

    def test_extra_leaf_not_in_center_still_checked(self) -> None:
        center, leaves = self._attached()
        leaves["leaf.c"] = _sample_proof("leaf-c-1")
        ok, errors = verify_peer_chain(center, leaves)
        assert not ok
        assert any("expected 3" in e for e in errors)

    def test_corrupt_center_hash_fails(self) -> None:
        center, leaves = self._attached()
        center.report_hash = "not-the-real-hash"
        ok, errors = verify_peer_chain(center, leaves)
        assert not ok
        assert any("report_hash" in e for e in errors)

    def test_from_dict_roundtrip_preserves_peer_fields(self) -> None:
        center, leaves = self._attached()
        restored = ProofReport.from_dict(center.to_dict())
        ok, _ = verify_peer_chain(restored, leaves)
        assert ok

    def test_manual_evidence_entry_without_top_level_peer_fields(self) -> None:
        center = _sample_proof("center-1")
        leaf = _sample_proof("leaf-1")
        center.evidence_chain.append(
            EvidenceEntry(
                source="peer:leaf.a",
                event_type="peer_proof_report",
                data={"peer_node_id": "leaf.a", "peer_chain_ref": leaf.report_hash},
            )
        )
        center.recompute_hash()
        ok, errors = verify_peer_chain(center, {"leaf.a": leaf})
        assert ok
        assert errors == []

    def test_empty_leaf_map_with_no_peer_entries_passes(self) -> None:
        center = _sample_proof("center-1")
        ok, errors = verify_peer_chain(center, {})
        assert ok
        assert errors == []

    def test_kripke_verdict_does_not_affect_peer_chain(self) -> None:
        center = _sample_proof("center-1")
        leaf = _sample_proof("leaf-1")
        center.kripke_verdict = KripkeVerdict(
            verified=False,
            properties_checked=1,
            properties_passed=0,
            failed_properties=["p1"],
        )
        center.overall_verdict = Verdict.FAIL
        attach_peer_proofs(center, [("leaf.a", leaf)])
        ok, _ = verify_peer_chain(center, {"leaf.a": leaf})
        assert ok
