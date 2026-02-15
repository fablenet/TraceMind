import pytest

from tm.artifacts import (
    ArtifactValidationError,
    validate_capability_spec,
    validate_execution_trace,
    validate_integrated_state_report,
    validate_intent_spec,
    validate_patch_proposal,
    validate_policy_spec,
    validate_workflow_policy,
)

ARTIFACT_ACCEPT_CASES = [
    (
        "IntentSpec",
        validate_intent_spec,
        {
            "intent_id": "intent.create.result",
            "version": "1.0.0",
            "goal": {"type": "achieve", "target": "result.validated"},
        },
    ),
    (
        "PolicySpec",
        validate_policy_spec,
        {
            "policy_id": "policy.minimal",
            "version": "1.0.0",
            "state_schema": {"result.validated": {"type": "string"}},
        },
    ),
    (
        "CapabilitySpec",
        validate_capability_spec,
        {
            "capability_id": "compute.process",
            "version": "0.1.0",
            "inputs": {},
            "event_types": [{"name": "compute.process.done"}],
            "state_extractors": [],
            "safety_contract": {
                "determinism": True,
                "side_effects": ["write"],
                "rollback": {"supported": False},
            },
        },
    ),
    (
        "WorkflowPolicy",
        validate_workflow_policy,
        {
            "workflow_id": "policy.minimal.reference",
            "intent_id": "intent.create.result",
            "policy_id": "policy.minimal",
            "steps": [
                {"step_id": "step_compute", "capability_id": "compute.process"},
            ],
            "explanation": {
                "intent_coverage": "carries intent target",
                "capability_reasoning": "single compute step",
                "constraint_coverage": "no extra invariants",
                "risks": ["none"],
            },
        },
    ),
    (
        "ExecutionTrace",
        validate_execution_trace,
        {
            "trace_id": "trace-123",
            "workflow_id": "policy.minimal.reference",
            "run_id": "run-123",
            "entries": [
                {"time": "2024-01-01T00:00:00Z", "unit": "step"},
            ],
            "timestamp": "2024-01-01T00:01:00Z",
        },
    ),
    (
        "IntegratedStateReport",
        validate_integrated_state_report,
        {
            "report_id": "report-123",
            "workflow_id": "policy.minimal.reference",
            "status": "satisfied",
            "intent_id": "intent.create.result",
            "state_snapshot": {},
            "metadata": {},
            "timestamp": "2024-01-01T00:01:00Z",
        },
    ),
    (
        "PatchProposal",
        validate_patch_proposal,
        {
            "proposal_id": "patch-123",
            "source": "analysis",
            "target": "policy",
            "description": "Add a guard",
            "rationale": "Policy review flagged a hole",
            "expected_effect": "Prevent unvalidated writes",
            "changes": [{"path": "guards[0].type", "value": "approval"}],
        },
    ),
]

ARTIFACT_REJECT_CASES = [
    (
        "IntentSpec",
        validate_intent_spec,
        {"intent_id": "intent.create.result", "goal": {"type": "achieve", "target": "result.validated"}},
        "version",
    ),
    (
        "PolicySpec",
        validate_policy_spec,
        {"policy_id": "policy.minimal", "version": "1.0.0"},
        "state_schema",
    ),
    (
        "CapabilitySpec",
        validate_capability_spec,
        {
            "capability_id": "compute.process",
            "version": "0.1.0",
            "inputs": {},
            "event_types": [{"name": "compute.process.done"}],
        },
        "safety_contract",
    ),
    (
        "WorkflowPolicy",
        validate_workflow_policy,
        {
            "workflow_id": "policy.minimal.reference",
            "intent_id": "intent.create.result",
            "policy_id": "policy.minimal",
            "explanation": {
                "intent_coverage": "carries intent target",
                "capability_reasoning": "single compute step",
                "constraint_coverage": "no extra invariants",
                "risks": ["none"],
            },
        },
        "steps",
    ),
    (
        "ExecutionTrace",
        validate_execution_trace,
        {
            "trace_id": "trace-123",
            "workflow_id": "policy.minimal.reference",
            "run_id": "run-123",
            "timestamp": "2024-01-01T00:01:00Z",
        },
        "entries",
    ),
    (
        "IntegratedStateReport",
        validate_integrated_state_report,
        {
            "report_id": "report-123",
            "workflow_id": "policy.minimal.reference",
            "timestamp": "2024-01-01T00:01:00Z",
        },
        "status",
    ),
    (
        "IntegratedStateReport",
        validate_integrated_state_report,
        {
            "report_id": "report-123",
            "workflow_id": "policy.minimal.reference",
            "status": "satisfied",
            "timestamp": "2024-01-01T00:01:00Z",
        },
        "state_snapshot",
    ),
    (
        "PatchProposal",
        validate_patch_proposal,
        {
            "proposal_id": "patch-123",
            "source": "analysis",
            "target": "policy",
            "description": "Add a guard",
            "expected_effect": "Prevent unvalidated writes",
        },
        "rationale",
    ),
]


@pytest.mark.parametrize("name, validator, payload", ARTIFACT_ACCEPT_CASES)
def test_artifact_validators_accept_minimal_payloads(name, validator, payload) -> None:
    validator(payload)


@pytest.mark.parametrize("name, validator, payload, missing", ARTIFACT_REJECT_CASES)
def test_artifact_validators_reject_missing_required_fields(name, validator, payload, missing) -> None:
    with pytest.raises(ArtifactValidationError) as excinfo:
        validator(payload)
    assert missing in str(excinfo.value)
