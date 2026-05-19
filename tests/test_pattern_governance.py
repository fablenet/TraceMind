"""Tests for Pattern Lifecycle ↔ Governance integration (Stage 5-3 task 3.4).

Patterns themselves are first-class artifacts and must go through the
same governance lifecycle as any other K-Ontology artifact:

  candidate  →  ``tm.artifacts.verify``  →  accepted

Plus: adding a new pattern to the library is itself a governable change,
expressed as a :class:`ProposedChangePlanBody` (a PatchProposal).

DoD:
- Each of the 3 seed patterns can be wrapped as a candidate Artifact and
  verified to ``accepted`` without errors
- Schema violations (malformed body) and template/slot mismatches cause
  verification failure with informative error messages
- A "new pattern" PatchProposal validates as a proper candidate
  ProposedChangePlan
"""

from __future__ import annotations

from typing import Any

import pytest

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    PropertyPatternBody,
    ProposedChangePlanBody,
    verify,
)
from tm.patterns import load_seed_patterns

# ─── Helpers ──────────────────────────────────────────────────────


def _envelope_for_pattern(pattern_id: str, status: str = "candidate") -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=f"pattern.{pattern_id}",
        status=ArtifactStatus(status),
        artifact_type=ArtifactType.PROPERTY_PATTERN,
        version="v0",
        created_by="tester",
        created_at="2026-05-12T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )


def _seed_body_raw(pattern_id: str) -> dict[str, Any]:
    """Return the body_raw dict for a seed pattern (round-trip from disk)."""
    import yaml  # type: ignore[import-untyped]

    lib = load_seed_patterns()
    entry = lib.get(pattern_id)
    with open(entry.path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _candidate_from_seed(pattern_id: str) -> Artifact:
    body_raw = _seed_body_raw(pattern_id)
    body = PropertyPatternBody.from_mapping(body_raw)
    envelope = _envelope_for_pattern(pattern_id)
    return Artifact(envelope=envelope, body=body, body_raw=body_raw)


# ─── Seed pattern lifecycle: candidate → accepted ─────────────────


class TestSeedPatternsAcceptedByGovernance:
    """Each shipped seed pattern must verify cleanly as an artifact."""

    @pytest.mark.parametrize(
        "pattern_id",
        [
            "safety.no_x_amplifies_y",
            "liveness.eventually_x_holds",
            "fairness.bounded_x_across_actors",
        ],
    )
    def test_seed_pattern_verifies_to_accepted(self, pattern_id: str) -> None:
        candidate = _candidate_from_seed(pattern_id)
        assert candidate.envelope.status == ArtifactStatus.CANDIDATE

        accepted, report = verify(candidate)

        assert report.errors == [], f"unexpected errors for {pattern_id}: {report.errors}"
        assert accepted is not None
        assert accepted.envelope.status == ArtifactStatus.ACCEPTED
        assert accepted.envelope.body_hash, "body_hash must be stamped on accept"

    @pytest.mark.parametrize(
        "pattern_id",
        [
            "safety.no_x_amplifies_y",
            "liveness.eventually_x_holds",
            "fairness.bounded_x_across_actors",
        ],
    )
    def test_accepted_pattern_has_determinism_meta(self, pattern_id: str) -> None:
        candidate = _candidate_from_seed(pattern_id)
        accepted, _ = verify(candidate)
        assert accepted is not None
        assert accepted.envelope.meta.get("determinism") is True
        assert "hashes" in accepted.envelope.meta
        assert accepted.envelope.meta["produced_by"].startswith("tracemind.verifier.")


# ─── Lifecycle error paths ────────────────────────────────────────


class TestPatternGovernanceRejectsInvalid:
    def test_phantom_slot_in_template_rejected(self) -> None:
        """Template references a slot that isn't declared."""
        body_raw = _seed_body_raw("liveness.eventually_x_holds")
        body_raw["formula_template"] = "EF {undeclared_slot_name}"
        body = PropertyPatternBody.from_mapping(body_raw)
        envelope = _envelope_for_pattern("invalid.phantom")
        candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)

        accepted, report = verify(candidate)

        assert accepted is None
        assert any("undeclared slot" in e for e in report.errors), report.errors

    def test_required_slot_not_referenced_by_template_rejected(self) -> None:
        body_raw = _seed_body_raw("liveness.eventually_x_holds")
        # Template no longer uses goal_predicate, but it is still required
        body_raw["formula_template"] = "EF terminal"
        body = PropertyPatternBody.from_mapping(body_raw)
        envelope = _envelope_for_pattern("invalid.unused_required")
        candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)

        accepted, report = verify(candidate)

        assert accepted is None
        assert any("not used by template" in e for e in report.errors), report.errors

    def test_invalid_category_rejected(self) -> None:
        body_raw = _seed_body_raw("liveness.eventually_x_holds")
        body_raw["category"] = "invariance"  # not in enum
        # PropertyPatternBody.from_mapping doesn't check the enum, so the
        # body can still parse, but verification must catch it via schema
        try:
            body = PropertyPatternBody.from_mapping(body_raw)
        except Exception:
            # If from_mapping rejects, that's also valid governance
            return
        envelope = _envelope_for_pattern("invalid.category")
        candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)

        accepted, report = verify(candidate)

        assert accepted is None
        assert any("schema" in e.lower() for e in report.errors), report.errors

    def test_non_candidate_status_rejected(self) -> None:
        """Verifier refuses to re-verify an already-accepted artifact."""
        candidate = _candidate_from_seed("safety.no_x_amplifies_y")
        candidate.envelope.status = ArtifactStatus.ACCEPTED

        accepted, report = verify(candidate)

        assert accepted is None
        assert any("candidate" in e for e in report.errors)


# ─── Submitting a new pattern via PatchProposal ───────────────────


class TestNewPatternAsPatchProposal:
    """Adding a brand-new pattern to the library is itself governable.

    The pattern is staged as a ProposedChangePlanBody (a PatchProposal),
    which goes through its own candidate → accepted lifecycle. This test
    proves that path works end-to-end.
    """

    def _proposal_body_for_new_pattern(self) -> dict[str, Any]:
        """Build a ProposedChangePlanBody whose decision asks the library
        admin to materialize a new seed pattern file. The decision encodes
        the new pattern's body in ``target_state`` so a downstream handler
        can write it out atomically.
        """
        new_pattern_body = {
            "pattern_id": "safety.guarded_action",
            "category": "safety",
            "title": "Action X must be preceded by guard Y",
            "description": "Stricter than no_X_amplifies_Y: every action requires guard",
            "formula_template": "AG (NOT {action} OR {guard})",
            "slots": [
                {"name": "action", "type": "ctl_predicate", "required": True},
                {"name": "guard", "type": "ctl_predicate", "required": True},
            ],
            "applicable_conditions": ["Both atoms are reachable"],
            "counterexamples": [{"description": "Action without guard", "scenario": "..."}],
        }
        return {
            "plan_id": "pattern-library.add.safety.guarded_action",
            "intent_id": "intent.pattern_library.curation",
            "summary": "Add safety.guarded_action seed pattern to the library",
            "decisions": [
                {
                    "effect_ref": "tm/patterns/seed/safety/guarded_action.yaml",
                    "target_state": {
                        "operation": "create_seed_pattern",
                        "body": new_pattern_body,
                    },
                    "idempotency_key": "pattern.add.safety.guarded_action.v1",
                    "reasoning_trace": "User proposed via tm pattern propose",
                }
            ],
            "llm_metadata": {
                "model": "none",
                "prompt_hash": "n/a",
                "determinism_hint": "deterministic",
            },
            "policy_requirements": [
                "All 3 existing seed patterns must continue to verify",
                "New seed must be domain-neutral (no FableNet types)",
            ],
        }

    def _patch_envelope(self, status: str = "candidate") -> ArtifactEnvelope:
        return ArtifactEnvelope(
            artifact_id="proposal.add-pattern-guarded-action",
            status=ArtifactStatus(status),
            artifact_type=ArtifactType.PROPOSED_CHANGE_PLAN,
            version="v0",
            created_by="tester",
            created_at="2026-05-12T00:00:00Z",
            body_hash="",
            envelope_hash="",
            meta={},
        )

    def test_patch_proposal_verifies_to_accepted(self) -> None:
        body_raw = self._proposal_body_for_new_pattern()
        body = ProposedChangePlanBody.from_mapping(body_raw)
        envelope = self._patch_envelope()
        candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)

        accepted, report = verify(candidate)

        assert report.errors == [], f"unexpected errors: {report.errors}"
        assert accepted is not None
        assert accepted.envelope.status == ArtifactStatus.ACCEPTED


# ─── Full E2E: candidate body → verify → import to library ────────


class TestPatternRoundTripThroughGovernance:
    """End-to-end: build a candidate pattern body in-memory, verify it,
    then ensure the accepted body is usable for instantiation.
    """

    def test_in_memory_candidate_accepts_and_instantiates(self) -> None:
        from tm.patterns import instantiate_pattern

        body_raw = {
            "pattern_id": "test.x_implies_eventually_y",
            "category": "fairness",
            "title": "X implies eventually Y",
            "description": "Test pattern for governance integration.",
            "formula_template": "AG (NOT {trigger} OR EF {response})",
            "slots": [
                {
                    "name": "trigger",
                    "type": "ctl_predicate",
                    "description": "the triggering condition",
                    "required": True,
                },
                {
                    "name": "response",
                    "type": "ctl_predicate",
                    "description": "the eventual response",
                    "required": True,
                },
            ],
            "applicable_conditions": ["Both predicates are reachable atoms"],
            "counterexamples": [
                {"description": "No response", "scenario": "Path where trigger fires but response never reached"},
            ],
            "metadata": {"source": "Stage 5-3 test fixture"},
        }
        body = PropertyPatternBody.from_mapping(body_raw)
        envelope = _envelope_for_pattern("test.x_implies_eventually_y")
        candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)

        accepted, report = verify(candidate)

        assert report.errors == [], report.errors
        assert accepted is not None

        # Accepted pattern body is usable for instantiation downstream
        instance = instantiate_pattern(
            accepted.body,  # type: ignore[arg-type]
            {"trigger": "has(burst)", "response": "done(throttle)"},
        )
        assert instance.resolved_formula == ("AG (NOT has(burst) OR EF done(throttle))")
