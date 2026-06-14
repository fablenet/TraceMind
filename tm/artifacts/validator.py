from __future__ import annotations

from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .schema import SCHEMAS


class ArtifactValidationError(RuntimeError):
    def __init__(self, schema_name: str, errors: Sequence[str]) -> None:
        super().__init__(f"{schema_name} failed validation")
        self.schema_name = schema_name
        self.errors = tuple(errors)

    def __str__(self) -> str:
        errors = "\n  - ".join(self.errors)
        return f"{self.schema_name} validation failed:\n  - {errors}"


def _format_error(error: ValidationError) -> str:
    path = ".".join(str(part) for part in error.path) or "<root>"
    return f"{path}: {error.message}"


def _validate(schema_name: str, schema: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: tuple(error.path))
    if errors:
        raise ArtifactValidationError(schema_name, tuple(_format_error(error) for error in errors))


def _validate_named(schema_name: str, payload: Mapping[str, Any]) -> None:
    schema = SCHEMAS[schema_name]
    _validate(schema_name, schema, payload)


def validate_intent_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("IntentSpec", payload)


def validate_capability_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("CapabilitySpec", payload)


def validate_policy_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("PolicySpec", payload)


def validate_workflow_policy(payload: Mapping[str, Any]) -> None:
    _validate_named("WorkflowPolicy", payload)


def validate_execution_trace(payload: Mapping[str, Any]) -> None:
    _validate_named("ExecutionTrace", payload)


def validate_integrated_state_report(payload: Mapping[str, Any]) -> None:
    _validate_named("IntegratedStateReport", payload)


def validate_patch_proposal(payload: Mapping[str, Any]) -> None:
    _validate_named("PatchProposal", payload)


def validate_property_pattern_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("PropertyPatternSpec", payload)


def validate_proof_report_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("ProofReportSpec", payload)


def validate_escalation_report_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("EscalationReportSpec", payload)


def validate_agent_network_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("AgentNetworkSpec", payload)


def validate_intent_session_spec(payload: Mapping[str, Any]) -> None:
    _validate_named("IntentSessionSpec", payload)


__all__ = [
    "ArtifactValidationError",
    "validate_intent_spec",
    "validate_capability_spec",
    "validate_policy_spec",
    "validate_workflow_policy",
    "validate_execution_trace",
    "validate_integrated_state_report",
    "validate_patch_proposal",
    "validate_property_pattern_spec",
    "validate_proof_report_spec",
    "validate_escalation_report_spec",
    "validate_agent_network_spec",
    "validate_intent_session_spec",
]
