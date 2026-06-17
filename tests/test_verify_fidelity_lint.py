"""Tests for ``lint_verify_meta_fidelity`` — ISSUE-FORMLANG P0.

Reconciles the verification model (``meta.verify``) against the executable
plan (``bundle.plan``). See ``.plan/formal-language-completeness-review.md`` §3
and ``.plan/issues/ISSUE-formal-language-expressiveness.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    verify,
)
from tm.artifacts.models import AgentBundleBody
from tm.lint import lint_verify_meta_fidelity
from tm.lint.plan_lint import LintIssue
from tm.verify.network import load_agent_bundle_body

FIXTURES = Path("tests/fixtures/network_violation")


# ─── helpers ──────────────────────────────────────────────────────


def _agent(agent_id: str = "agent.x") -> Dict[str, Any]:
    """Minimal valid agent with an empty IO contract."""
    return {
        "agent_id": agent_id,
        "name": "x",
        "version": "0.1",
        "runtime": {"kind": "tm-noop", "config": {}},
        "contract": {"inputs": [], "outputs": [], "effects": []},
        "config_schema": {"type": "object"},
        "evidence_outputs": [],
    }


def _bundle(
    *,
    plan: List[Dict[str, Any]],
    verify_meta: Dict[str, Any] | None,
    bundle_id: str = "bundle.test",
) -> tuple[AgentBundleBody, Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if verify_meta is not None:
        meta["verify"] = verify_meta
    raw: Dict[str, Any] = {
        "bundle_id": bundle_id,
        "agents": [_agent()],
        "plan": plan,
        "meta": meta,
    }
    return AgentBundleBody.from_mapping(raw), raw


def _codes(issues: List[LintIssue]) -> set[str]:
    return {i.code for i in issues}


def _errors(issues: List[LintIssue]) -> List[LintIssue]:
    return [i for i in issues if i.severity == "error"]


# ─── clean reconciliation ─────────────────────────────────────────


class TestCleanReconciliation:
    def test_shipped_center_fixture_is_clean(self) -> None:
        body = load_agent_bundle_body(FIXTURES / "bundle.center.yaml")
        issues = lint_verify_meta_fidelity(body, {})
        assert _errors(issues) == []

    def test_shipped_leaf_fixture_is_clean(self) -> None:
        body = load_agent_bundle_body(FIXTURES / "bundle.leaf_a.yaml")
        issues = lint_verify_meta_fidelity(body, {})
        assert _errors(issues) == []

    def test_inline_matching_bundle_is_clean(self) -> None:
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta={
                "initial_store": {},
                "changed_paths": ["start"],
                "steps": {"detect": {"reads": [], "writes": ["detected"]}},
                "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["detect"]}],
            },
        )
        assert lint_verify_meta_fidelity(body, raw) == []

    def test_no_meta_verify_is_noop(self) -> None:
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta=None,
        )
        assert lint_verify_meta_fidelity(body, raw) == []


# ─── drift detection ──────────────────────────────────────────────


class TestDriftDetection:
    def test_writes_drift_is_error(self) -> None:
        # plan produces 'detected'; verify models writing 'quarantined' instead.
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {"detect": {"reads": [], "writes": ["quarantined"]}},
                "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["detect"]}],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_WRITES_DRIFT" in _codes(issues)
        assert any(i.severity == "error" and i.code == "VERIFY_WRITES_DRIFT" for i in issues)

    def test_unmodeled_plan_step_is_error(self) -> None:
        # plan has 'act' step but verify never models it.
        body, raw = _bundle(
            plan=[
                {"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]},
                {"step": "act", "agent_id": "agent.x", "inputs": ["detected"], "outputs": ["acted"]},
            ],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {"detect": {"reads": [], "writes": ["detected"]}},
                "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["detect"]}],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_STEP_UNMODELED" in _codes(issues)

    def test_orphan_verify_step_is_error(self) -> None:
        # verify models 'ghost' that has no plan step.
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {
                    "detect": {"reads": [], "writes": ["detected"]},
                    "ghost": {"reads": [], "writes": ["spooked"]},
                },
                "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["detect"]}],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_STEP_ORPHAN" in _codes(issues)

    def test_reads_drift_is_warning_not_error(self) -> None:
        # verify reads a fact the plan step does not consume — advisory only.
        body, raw = _bundle(
            plan=[{"step": "act", "agent_id": "agent.x", "inputs": ["detected"], "outputs": ["acted"]}],
            verify_meta={
                "changed_paths": ["detected"],
                "steps": {"act": {"reads": [], "writes": ["acted"]}},
                "rules": [{"name": "go", "triggers": ["detected"], "steps": ["act"]}],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_READS_DRIFT" in _codes(issues)
        assert _errors(issues) == []

    def test_unreachable_trigger_is_warning(self) -> None:
        # rule 'never' triggers on a fact no step writes and no seed provides.
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {"detect": {"reads": [], "writes": ["detected"]}},
                "rules": [
                    {"name": "on_start", "triggers": ["start"], "steps": ["detect"]},
                    {"name": "never", "triggers": ["phantom"], "steps": ["detect"]},
                ],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_TRIGGER_UNREACHABLE" in _codes(issues)
        assert all(i.severity == "warning" for i in issues if i.code == "VERIFY_TRIGGER_UNREACHABLE")


# ─── malformed / waiver / empty-plan edge cases ───────────────────


class TestEdgeCases:
    def test_malformed_steps_is_error(self) -> None:
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta={"changed_paths": ["start"], "steps": ["not", "a", "mapping"]},
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_META_MALFORMED" in _codes(issues)

    def test_manual_waiver_skips_reconciliation(self) -> None:
        # Drift exists, but the author explicitly opted out.
        body, raw = _bundle(
            plan=[{"step": "detect", "agent_id": "agent.x", "inputs": [], "outputs": ["detected"]}],
            verify_meta={
                "fidelity": "manual",
                "changed_paths": ["start"],
                "steps": {"detect": {"reads": [], "writes": ["totally_different"]}},
                "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["detect"]}],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert _codes(issues) == {"VERIFY_FIDELITY_WAIVED"}
        assert _errors(issues) == []

    def test_meta_verify_without_plan_warns(self) -> None:
        body, raw = _bundle(
            plan=[],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {"detect": {"reads": [], "writes": ["detected"]}},
                "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["detect"]}],
            },
        )
        issues = lint_verify_meta_fidelity(body, raw)
        assert "VERIFY_NO_PLAN" in _codes(issues)
        assert _errors(issues) == []


# ─── governance integration ───────────────────────────────────────


def _envelope(bundle_id: str, *, status: str = "candidate", version: str = "v0") -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=f"bundle.{bundle_id}",
        status=ArtifactStatus(status),
        artifact_type=ArtifactType.AGENT_BUNDLE,
        version=version,
        created_by="tester",
        created_at="2026-06-17T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )


def _candidate(raw: Dict[str, Any]) -> Artifact:
    body = AgentBundleBody.from_mapping(raw)
    return Artifact(envelope=_envelope("test"), body=body, body_raw=raw)


class TestGovernanceIntegration:
    def test_clean_bundle_with_verify_meta_accepted(self) -> None:
        # Empty plan IO keeps the io-contract lint happy; verify model matches.
        _body, raw = _bundle(
            plan=[{"step": "work", "agent_id": "agent.x", "inputs": [], "outputs": []}],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {"work": {"reads": [], "writes": []}},
                "rules": [{"name": "go", "triggers": ["start"], "steps": ["work"]}],
            },
        )
        accepted, report = verify(_candidate(raw))
        assert accepted is not None, report.errors
        assert accepted.envelope.status == ArtifactStatus.ACCEPTED

    def test_writes_drift_rejected_by_governance(self) -> None:
        # plan 'work' produces nothing; verify models it writing 'done' -> drift.
        _body, raw = _bundle(
            plan=[{"step": "work", "agent_id": "agent.x", "inputs": [], "outputs": []}],
            verify_meta={
                "changed_paths": ["start"],
                "steps": {"work": {"reads": [], "writes": ["done"]}},
                "rules": [{"name": "go", "triggers": ["start"], "steps": ["work"]}],
            },
        )
        accepted, report = verify(_candidate(raw))
        assert accepted is None
        assert any("VERIFY_WRITES_DRIFT" in err for err in report.errors)
