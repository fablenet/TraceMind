"""Cross-node escalation aggregation tests — Phase 6 Stage 6-3.3."""

from __future__ import annotations

from tm.control.meta.convergence import ConvergenceVerdict, Trend
from tm.control.meta.escalation import (
    CrossNodeEscalationReport,
    EscalationReport,
    Escalator,
    NetworkEscalator,
    Severity,
    SuggestedAction,
)
from tm.control.meta.kpi_tracker import CycleRecord, KpiTracker


def _escalation(
    *,
    report_id: str = "esc-1",
    severity: Severity = Severity.WARNING,
    gap: str = "stalled",
    peer_node_id: str | None = None,
) -> EscalationReport:
    verdict = ConvergenceVerdict(
        converged=False,
        trend=Trend.STALLED,
        kpi_name="kpi.x",
        values=(0.5, 0.5),
        delta=0.0,
        reason=gap,
    )
    return EscalationReport(
        report_id=report_id,
        timestamp="2026-01-01T00:00:00Z",
        severity=severity,
        intent_ref="intent.test",
        verdicts=(verdict,),
        kpi_history=(),
        recent_rules_fired=("rule-1",),
        recent_errors=(),
        gap_summary=gap,
        suggested_actions=(SuggestedAction.TIGHTEN_THRESHOLDS,),
        counterexample=None,
        peer_node_id=peer_node_id,
    )


class TestEscalationReportPeerNode:
    def test_to_dict_includes_peer_node_id(self) -> None:
        report = _escalation(peer_node_id="bundle.leaf_a")
        payload = report.to_dict()
        assert payload["peer_node_id"] == "bundle.leaf_a"

    def test_to_dict_omits_peer_when_unset(self) -> None:
        report = _escalation()
        assert "peer_node_id" not in report.to_dict()

    def test_from_dict_roundtrip(self) -> None:
        original = _escalation(peer_node_id="leaf.b", severity=Severity.CRITICAL)
        restored = EscalationReport.from_dict(original.to_dict())
        assert restored.peer_node_id == "leaf.b"
        assert restored.severity == Severity.CRITICAL
        assert restored.gap_summary == original.gap_summary


class TestNetworkEscalator:
    def test_returns_none_when_nothing_to_report(self) -> None:
        escalator = NetworkEscalator("network.demo")
        assert escalator.aggregate(None, {}) is None

    def test_aggregates_peer_escalations(self) -> None:
        escalator = NetworkEscalator("network.demo")
        peers = {
            "leaf.a": _escalation(report_id="esc-a", peer_node_id="leaf.a"),
            "leaf.b": _escalation(report_id="esc-b", peer_node_id="leaf.b", gap="worsening"),
        }
        report = escalator.aggregate(None, peers)
        assert report is not None
        assert isinstance(report, CrossNodeEscalationReport)
        assert set(report.peer_escalations.keys()) == {"leaf.a", "leaf.b"}
        assert report.severity == Severity.WARNING

    def test_includes_center_escalation(self) -> None:
        escalator = NetworkEscalator("network.demo")
        center = _escalation(report_id="esc-center", gap="center stalled")
        report = escalator.aggregate(center, {})
        assert report is not None
        assert report.center_escalation is center
        assert "center stalled" in report.summary

    def test_peer_chain_failure_is_critical(self) -> None:
        escalator = NetworkEscalator("network.demo")
        report = escalator.aggregate(
            None,
            {},
            peer_chain_valid=False,
            peer_chain_errors=["peer chain ref mismatch"],
        )
        assert report is not None
        assert report.severity == Severity.CRITICAL
        assert not report.peer_chain_valid
        assert "verification failed" in report.summary

    def test_worst_severity_wins(self) -> None:
        escalator = NetworkEscalator("network.demo")
        center = _escalation(severity=Severity.INFO)
        peers = {"leaf.a": _escalation(severity=Severity.CRITICAL, peer_node_id="leaf.a")}
        report = escalator.aggregate(center, peers)
        assert report is not None
        assert report.severity == Severity.CRITICAL

    def test_to_dict_includes_all_peers(self) -> None:
        escalator = NetworkEscalator("network.demo")
        peers = {"leaf.a": _escalation(peer_node_id="leaf.a")}
        report = escalator.aggregate(None, peers)
        assert report is not None
        payload = report.to_dict()
        assert payload["network_id"] == "network.demo"
        assert "leaf.a" in payload["peer_escalations"]

    def test_ignores_none_peer_entries(self) -> None:
        escalator = NetworkEscalator("network.demo")
        report = escalator.aggregate(None, {"leaf.a": None, "leaf.b": _escalation(peer_node_id="leaf.b")})
        assert report is not None
        assert set(report.peer_escalations.keys()) == {"leaf.b"}

    def test_escalator_still_sets_peer_via_evaluate(self) -> None:
        tracker = KpiTracker(window_size=3)
        for i, v in enumerate([0.1, 0.3, 0.6]):
            tracker.record(CycleRecord(f"c{i}", float(i), {"x": v}, "rule-1", "ok", ()))
        verdict = ConvergenceVerdict(
            converged=False,
            trend=Trend.WORSENING,
            kpi_name="x",
            values=(0.1, 0.3, 0.6),
            delta=0.5,
            reason="worsening",
        )
        report = Escalator(intent_ref="intent.test").evaluate(tracker, [verdict])
        assert report is not None
        assert report.peer_node_id is None

    def test_cross_node_view_preserves_peer_verdicts(self) -> None:
        escalator = NetworkEscalator("network.demo")
        peer = _escalation(peer_node_id="leaf.a")
        report = escalator.aggregate(None, {"leaf.a": peer})
        assert report is not None
        assert report.peer_escalations["leaf.a"].verdicts[0].trend == Trend.STALLED

    def test_peer_chain_errors_listed_in_summary(self) -> None:
        escalator = NetworkEscalator("network.demo")
        report = escalator.aggregate(
            None,
            {"leaf.a": _escalation(peer_node_id="leaf.a")},
            peer_chain_valid=False,
            peer_chain_errors=["missing peer evidence for 'leaf.b'"],
        )
        assert report is not None
        assert "missing peer evidence" in report.summary
