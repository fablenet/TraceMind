"""Smoke tests for ``tm.control.meta`` extracted core API.

These tests pin the **public surface** that downstream control scenarios
(``fablenet-control``, future ``k8s`` scenarios, etc.) depend on. The
exhaustive behavioral suite remains in ``fablenet-control/tests/`` since
that is where the lived-in usage patterns are.

Covers the three priorities of Stage 5-2 phase A:

- ``CycleRecord.from_cycle_outputs`` is **parameterized** (``kpi_keys=None``
  default extracts all numeric metrics; explicit allowlist restricts).
- Defaults are domain-neutral (no ``FNET-INT-001``).
- The barrel ``from tm.control.meta import ...`` works.
"""

from __future__ import annotations

import pytest

from tm.control.meta import (
    UNSPECIFIED_INTENT_REF,
    ConvergenceDetector,
    ConvergenceVerdict,
    CycleRecord,
    EscalationReport,
    Escalator,
    EvidenceEntry,
    KpiTracker,
    KripkeVerdict,
    MetaController,
    MetaControllerResult,
    ProofReport,
    ProofReportGenerator,
    Severity,
    SuggestedAction,
    Trend,
    Verdict,
    build_hash_chain,
    diff_snapshots,
    verify_hash_chain,
)


class TestCycleRecordExtraction:
    def test_default_extracts_all_numeric_metrics(self) -> None:
        snap = {
            "environment": {
                "metrics": {
                    "qps": 100.0,
                    "errors": 3,
                    "name": "ignored_string",
                    "active": True,
                    "ratio": "0.42",
                }
            }
        }
        rec = CycleRecord.from_cycle_outputs("c1", snap, {"status": "ok"})
        assert set(rec.kpis.keys()) == {"qps", "errors", "ratio"}
        assert rec.kpis["qps"] == 100.0
        assert rec.kpis["errors"] == 3.0
        assert rec.kpis["ratio"] == pytest.approx(0.42)

    def test_allowlist_restricts_extraction(self) -> None:
        snap = {"environment": {"metrics": {"a": 1.0, "b": 2.0, "c": 3.0}}}
        rec = CycleRecord.from_cycle_outputs("c1", snap, {"status": "ok"}, kpi_keys=["a", "c"])
        assert set(rec.kpis.keys()) == {"a", "c"}
        assert rec.kpis["a"] == 1.0
        assert rec.kpis["c"] == 3.0

    def test_allowlist_ignores_missing_keys(self) -> None:
        snap = {"environment": {"metrics": {"a": 1.0}}}
        rec = CycleRecord.from_cycle_outputs("c1", snap, {"status": "ok"}, kpi_keys=["a", "missing"])
        assert rec.kpis == {"a": 1.0}

    def test_empty_metrics_returns_empty_kpis(self) -> None:
        rec = CycleRecord.from_cycle_outputs("c1", {}, {"status": "ok"})
        assert rec.kpis == {}

    def test_extracts_policy_rule_fired(self) -> None:
        policy_result = {
            "actions": [
                {"rule_id": "r-skipped", "applied": False},
                {"rule_id": "r-fired", "applied": True},
                {"rule_id": "r-after", "applied": True},
            ]
        }
        rec = CycleRecord.from_cycle_outputs("c1", {}, {"status": "ok"}, policy_result=policy_result)
        assert rec.policy_rule_fired == "r-fired"

    def test_captures_errors_and_status(self) -> None:
        rec = CycleRecord.from_cycle_outputs("c1", {}, {"status": "failed", "errors": ["e1", "e2"]})
        assert rec.act_status == "failed"
        assert rec.errors == ("e1", "e2")


class TestKpiTracker:
    def test_sliding_window(self) -> None:
        tracker = KpiTracker(window_size=2)
        for i in range(5):
            tracker.record(
                CycleRecord(
                    cycle_id=f"c{i}",
                    timestamp=float(i),
                    kpis={"x": float(i)},
                    policy_rule_fired="",
                    act_status="ok",
                    errors=(),
                )
            )
        assert tracker.count == 2
        assert [r.cycle_id for r in tracker.records] == ["c3", "c4"]

    def test_kpi_series_skips_missing(self) -> None:
        tracker = KpiTracker(window_size=5)
        tracker.record(CycleRecord("c1", 1.0, {"x": 0.1}, "", "ok", ()))
        tracker.record(CycleRecord("c2", 2.0, {"y": 0.5}, "", "ok", ()))
        tracker.record(CycleRecord("c3", 3.0, {"x": 0.3}, "", "ok", ()))
        assert tracker.kpi_series("x") == [0.1, 0.3]
        assert tracker.kpi_series("y") == [0.5]


class TestConvergenceDetector:
    def _make_tracker(self, values: list[float], kpi_name: str = "x") -> KpiTracker:
        tracker = KpiTracker(window_size=len(values) + 1)
        for i, v in enumerate(values):
            tracker.record(CycleRecord(f"c{i}", float(i), {kpi_name: v}, "", "ok", ()))
        return tracker

    def test_insufficient_data(self) -> None:
        detector = ConvergenceDetector(target_kpi="x")
        tracker = self._make_tracker([0.5])
        verdict = detector.evaluate(tracker)
        assert verdict.trend == Trend.INSUFFICIENT_DATA
        assert not verdict.converged

    def test_converged_within_tolerance(self) -> None:
        detector = ConvergenceDetector(target_kpi="x", target_value=0.0, tolerance=0.05)
        tracker = self._make_tracker([0.4, 0.2, 0.01])
        verdict = detector.evaluate(tracker)
        assert verdict.converged
        assert verdict.trend == Trend.IMPROVING

    def test_stalled(self) -> None:
        detector = ConvergenceDetector(target_kpi="x", target_value=0.0, tolerance=0.01, stall_threshold=0.05)
        tracker = self._make_tracker([0.51, 0.50, 0.50])
        verdict = detector.evaluate(tracker)
        assert verdict.trend == Trend.STALLED
        assert verdict.needs_escalation

    def test_worsening_when_down_target(self) -> None:
        detector = ConvergenceDetector(target_kpi="x", target_value=0.0, direction="down")
        tracker = self._make_tracker([0.1, 0.3, 0.6])
        verdict = detector.evaluate(tracker)
        assert verdict.trend == Trend.WORSENING

    def test_improving_when_up_target(self) -> None:
        detector = ConvergenceDetector(target_kpi="x", target_value=1.0, tolerance=0.01, direction="up")
        tracker = self._make_tracker([0.1, 0.4, 0.7])
        verdict = detector.evaluate(tracker)
        assert verdict.trend == Trend.IMPROVING


class TestEscalator:
    def test_default_intent_ref_is_neutral(self) -> None:
        escalator = Escalator()
        assert escalator.intent_ref == UNSPECIFIED_INTENT_REF
        assert escalator.intent_ref == "intent.unspecified"

    def test_explicit_intent_ref_preserved(self) -> None:
        escalator = Escalator(intent_ref="FNET-INT-001")
        assert escalator.intent_ref == "FNET-INT-001"

    def test_returns_none_when_no_escalation_needed(self) -> None:
        tracker = KpiTracker()
        verdict = ConvergenceVerdict(
            converged=True,
            trend=Trend.IMPROVING,
            kpi_name="x",
            values=(0.1, 0.05, 0.01),
            delta=-0.09,
            reason="converged",
        )
        escalator = Escalator()
        assert escalator.evaluate(tracker, [verdict]) is None

    def test_produces_report_when_worsening(self) -> None:
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
        escalator = Escalator(intent_ref="test.intent")
        report = escalator.evaluate(tracker, [verdict])
        assert report is not None
        assert isinstance(report, EscalationReport)
        assert report.intent_ref == "test.intent"
        assert report.severity in (Severity.WARNING, Severity.CRITICAL)
        assert SuggestedAction.HUMAN_REVIEW in report.suggested_actions
        assert report.counterexample is not None


class TestMetaController:
    def test_default_intent_ref_is_neutral(self) -> None:
        ctrl = MetaController(
            l1_runner=lambda _: None,  # not invoked in this test
            detector=ConvergenceDetector(target_kpi="x"),
        )
        assert ctrl._escalator.intent_ref == UNSPECIFIED_INTENT_REF

    def test_run_with_converging_runner(self) -> None:
        from tm.control.meta import L1CycleResult

        def runner(cycle_num: int) -> L1CycleResult:
            return L1CycleResult(
                cycle_id=f"c{cycle_num}",
                snapshot={"environment": {"metrics": {"x": 1.0 / cycle_num}}},
                report={"status": "ok"},
            )

        ctrl = MetaController(
            l1_runner=runner,
            detector=ConvergenceDetector(target_kpi="x", target_value=0.0, tolerance=0.5),
            max_cycles=3,
        )
        result = ctrl.run()
        assert isinstance(result, MetaControllerResult)
        assert result.converged
        assert result.escalation is None
        assert result.total_cycles >= 1

    def test_run_passes_kpi_keys_through(self) -> None:
        from tm.control.meta import L1CycleResult

        captured: list[dict[str, float]] = []

        def runner(cycle_num: int) -> L1CycleResult:
            return L1CycleResult(
                cycle_id=f"c{cycle_num}",
                snapshot={"environment": {"metrics": {"x": 0.5, "y": 0.5, "z": 0.5}}},
                report={"status": "ok"},
            )

        ctrl = MetaController(
            l1_runner=runner,
            detector=ConvergenceDetector(target_kpi="x", target_value=0.0, tolerance=0.01),
            max_cycles=3,
            kpi_keys=["x"],
        )
        result = ctrl.run()
        for rec in result.cycle_records:
            assert set(rec.kpis.keys()) == {"x"}
            captured.append(dict(rec.kpis))
        assert len(captured) >= 1


class TestProofReport:
    def _make_kripke(self, verified: bool = True) -> KripkeVerdict:
        return KripkeVerdict(
            verified=verified,
            properties_checked=3 if verified else 2,
            properties_passed=3 if verified else 1,
            failed_properties=[] if verified else ["safety.A"],
        )

    def test_kripke_verdict_inconclusive_when_no_properties(self) -> None:
        kv = KripkeVerdict(verified=False, properties_checked=0, properties_passed=0)
        assert kv.verdict == Verdict.INCONCLUSIVE

    def test_kripke_verdict_pass_when_verified(self) -> None:
        kv = self._make_kripke(verified=True)
        assert kv.verdict == Verdict.PASS

    def test_kripke_verdict_fail_when_not_verified(self) -> None:
        kv = self._make_kripke(verified=False)
        assert kv.verdict == Verdict.FAIL

    def test_generator_with_neutral_intent_id(self) -> None:
        class FakeResult:
            bundle_artifact_id = "bundle-test"
            env_snapshot = None
            execution_report = None
            policy_decisions: list = []

        gen = ProofReportGenerator(intent_id="my.test.intent")
        report = gen.generate(
            cycle_result=FakeResult(),
            kripke_verdict=self._make_kripke(verified=True),
        )
        assert isinstance(report, ProofReport)
        assert report.intent_id == "my.test.intent"
        assert report.overall_verdict in (Verdict.PASS, Verdict.INCONCLUSIVE)

    def test_diff_snapshots(self) -> None:
        pre = {"environment": {"metrics": {"x": 1.0, "y": 2.0}}}
        post = {"environment": {"metrics": {"x": 1.5, "z": 3.0}}}
        diff = diff_snapshots(pre, post)
        assert any(e.key == "x" and e.delta == pytest.approx(0.5) for e in diff.changed)
        assert "z" in diff.added
        assert "y" in diff.removed

    def test_build_and_verify_hash_chain_roundtrip(self) -> None:
        class FakeResult:
            bundle_artifact_id = "bundle-test"
            env_snapshot = None
            execution_report = None
            policy_decisions: list = []

        gen = ProofReportGenerator(intent_id="t")
        report = gen.generate(cycle_result=FakeResult())
        chain = build_hash_chain(report)
        assert verify_hash_chain(chain, report)
        assert len(chain) == 6

    def test_proof_report_evidence_entry_timestamps(self) -> None:
        entry = EvidenceEntry(source="s", event_type="e")
        assert entry.timestamp != ""

    def test_round_trip_dict(self) -> None:
        class FakeResult:
            bundle_artifact_id = "bundle-test"
            env_snapshot = None
            execution_report = None
            policy_decisions: list = []

        gen = ProofReportGenerator(intent_id="t")
        report = gen.generate(cycle_result=FakeResult())
        as_dict = report.to_dict()
        restored = ProofReport.from_dict(as_dict)
        assert restored.intent_id == report.intent_id
        assert restored.overall_verdict == report.overall_verdict
