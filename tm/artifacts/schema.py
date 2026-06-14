from __future__ import annotations

from typing import Any, Mapping

Schema = Mapping[str, Any]

_IDENTIFIER_PATTERN = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
_DATE_TIME_SCHEMA: Schema = {"type": "string", "format": "date-time"}

_PROPERTY_DESCRIPTOR: Schema = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "required": {"type": "boolean"},
        "description": {"type": "string"},
        "default": {},
        "schema": {"type": "object"},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_GOAL_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["achieve", "avoid", "maintain"]},
        "target": {"type": "string"},
        "description": {"type": "string"},
        "parameters": {"type": "object"},
    },
    "required": ["type", "target"],
    "additionalProperties": False,
}

_CONSTRAINT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "rule": {"type": "string"},
        "value": {},
        "description": {"type": "string"},
        "context": {"type": "object"},
    },
    "required": ["type", "rule"],
    "additionalProperties": False,
}

_PREFERENCE_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "weight": {"anyOf": [{"type": "string"}, {"type": "number"}]},
        "description": {"type": "string"},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_INTENT_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "intent_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "version": {"type": "string"},
        "goal": _GOAL_SCHEMA,
        "constraints": {"type": "array", "items": _CONSTRAINT_SCHEMA},
        "preferences": {"type": "array", "items": _PREFERENCE_SCHEMA},
        "context_refs": {"type": "array", "items": {"type": "string"}},
        "property_pattern_refs": {"type": "array", "items": {"type": "string"}},
        "slot_fills": {
            "type": "object",
            "additionalProperties": {"type": "object"},
        },
        "metadata": {"type": "object"},
    },
    "required": ["intent_id", "version", "goal"],
    "additionalProperties": False,
}

_EVENT_TYPE_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "payload_schema": {"type": "object"},
        "description": {"type": "string"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_STATE_EXTRACTOR_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "from_event": {"type": "string"},
        "produces": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "value": {},
                    "stability": {"type": "string", "enum": ["stable", "unstable", "derived"]},
                },
                "required": ["type"],
                "additionalProperties": False,
            },
        },
        "description": {"type": "string"},
    },
    "required": ["from_event", "produces"],
    "additionalProperties": False,
}

_SAFETY_CONTRACT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "determinism": {"type": "boolean"},
        "side_effects": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "rollback": {
            "type": "object",
            "properties": {
                "supported": {"type": "boolean"},
                "strategy": {"type": "string"},
            },
            "required": ["supported"],
            "additionalProperties": False,
        },
        "isolation_level": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["determinism", "side_effects", "rollback"],
    "additionalProperties": False,
}

_CAPABILITY_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "capability_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "version": {"type": "string"},
        "description": {"type": "string"},
        "inputs": {"type": "object", "additionalProperties": _PROPERTY_DESCRIPTOR},
        "outputs": {"type": "object", "additionalProperties": _PROPERTY_DESCRIPTOR},
        "config_schema": {"type": "object", "additionalProperties": _PROPERTY_DESCRIPTOR},
        "event_types": {"type": "array", "items": _EVENT_TYPE_SCHEMA},
        "state_extractors": {"type": "array", "items": _STATE_EXTRACTOR_SCHEMA},
        "safety_contract": _SAFETY_CONTRACT_SCHEMA,
        "execution_binding": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "ref": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    },
    "required": [
        "capability_id",
        "version",
        "inputs",
        "event_types",
        "state_extractors",
        "safety_contract",
    ],
    "additionalProperties": False,
}

_STATE_SCHEMA_ENTRY: Schema = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "source": {"type": "string"},
        "stability": {"type": "string", "enum": ["stable", "unstable", "derived"]},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_INVARIANT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "type": {"type": "string"},
        "condition": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["id", "type", "condition"],
    "additionalProperties": False,
}

_LIVENESS_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "type": {"type": "string"},
        "condition": {"type": "string"},
        "within": {"type": "string"},
    },
    "required": ["id", "type", "condition"],
    "additionalProperties": False,
}

_GUARD_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "scope": {"type": "string"},
        "required_for": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "minItems": 1},
            ]
        },
        "config": {"type": "object"},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_POLICY_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "policy_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "version": {"type": "string"},
        "description": {"type": "string"},
        "state_schema": {
            "type": "object",
            "patternProperties": {"[a-zA-Z0-9_.-]+": _STATE_SCHEMA_ENTRY},
            "additionalProperties": False,
        },
        "invariants": {"type": "array", "items": _INVARIANT_SCHEMA},
        "liveness": {"type": "array", "items": _LIVENESS_SCHEMA},
        "guards": {"type": "array", "items": _GUARD_SCHEMA},
        "metadata": {"type": "object"},
    },
    "required": ["policy_id", "version", "state_schema"],
    "additionalProperties": False,
}

_STEP_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "step_id": {"type": "string"},
        "capability_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "description": {"type": "string"},
        "inputs": {"type": "object"},
        "outputs": {"type": "object"},
        "guard": _GUARD_SCHEMA,
        "metadata": {"type": "object"},
    },
    "required": ["step_id", "capability_id"],
    "additionalProperties": False,
}

_TRANSITION_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "condition": {"type": "string"},
        "type": {"type": "string"},
    },
    "required": ["from", "to"],
    "additionalProperties": False,
}

_EXPLANATION_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "intent_coverage": {"type": "string"},
        "capability_reasoning": {"type": "string"},
        "constraint_coverage": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "string"},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent_coverage", "capability_reasoning", "constraint_coverage", "risks"],
    "additionalProperties": False,
}

_WORKFLOW_POLICY_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "workflow_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "intent_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "policy_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "string"},
        "steps": {"type": "array", "items": _STEP_SCHEMA, "minItems": 1},
        "transitions": {"type": "array", "items": _TRANSITION_SCHEMA},
        "guards": {"type": "array", "items": _GUARD_SCHEMA},
        "explanation": _EXPLANATION_SCHEMA,
        "created_at": _DATE_TIME_SCHEMA,
        "metadata": {"type": "object"},
    },
    "required": ["workflow_id", "intent_id", "policy_id", "steps", "explanation"],
    "additionalProperties": False,
}

_TRACE_ENTRY_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "time": _DATE_TIME_SCHEMA,
        "unit": {"type": "string"},
        "status": {"type": "string"},
        "event": {"type": "string"},
        "details": {"type": "object"},
    },
    "required": ["time", "unit"],
    "additionalProperties": False,
}

_EXECUTION_TRACE_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "trace_id": {"type": "string"},
        "workflow_id": {"type": "string"},
        "workflow_revision": {"type": "string"},
        "run_id": {"type": "string"},
        "intent_id": {"type": "string"},
        "timestamp": _DATE_TIME_SCHEMA,
        "entries": {"type": "array", "items": _TRACE_ENTRY_SCHEMA},
        "state_snapshot": {"type": "object"},
        "violations": {"type": "array", "items": {"type": "string"}},
        "metadata": {"type": "object"},
    },
    "required": ["trace_id", "workflow_id", "run_id", "entries", "timestamp"],
    "additionalProperties": False,
}

_BLAME_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "capability": {"type": "string"},
        "policy": {"type": "string"},
        "guard": {"type": "string"},
        "step": {"type": "string"},
    },
    "additionalProperties": False,
}

_INTEGRATED_STATE_REPORT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "report_id": {"type": "string"},
        "workflow_id": {"type": "string"},
        "intent_id": {"type": "string"},
        "status": {"type": "string", "enum": ["satisfied", "violated", "unknown"]},
        "violated_rules": {"type": "array", "items": {"type": "string"}},
        "state_snapshot": {"type": "object"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "blame": _BLAME_SCHEMA,
        "timestamp": _DATE_TIME_SCHEMA,
        "metadata": {"type": "object"},
    },
    "required": ["report_id", "workflow_id", "status", "timestamp", "state_snapshot"],
    "additionalProperties": False,
}

_PATCH_CHANGE_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "value": {},
        "op": {"type": "string", "enum": ["set", "remove"]},
        "note": {"type": "string"},
    },
    "required": ["path"],
    "additionalProperties": False,
}

_PATCH_PROPOSAL_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "proposal_id": {"type": "string"},
        "source": {"type": "string", "enum": ["violation", "analysis", "human", "ai"]},
        "target": {"type": "string", "enum": ["policy", "intent", "workflow", "config"]},
        "description": {"type": "string"},
        "rationale": {"type": "string"},
        "expected_effect": {"type": "string"},
        "changes": {"type": "array", "items": _PATCH_CHANGE_SCHEMA},
        "created_at": _DATE_TIME_SCHEMA,
        "metadata": {"type": "object"},
    },
    "required": ["proposal_id", "source", "target", "description", "rationale", "expected_effect"],
    "additionalProperties": False,
}

_PROPERTY_PATTERN_SLOT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
        "type": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "required": {"type": "boolean"},
    },
    "required": ["name", "type"],
    "additionalProperties": False,
}

_PROPERTY_PATTERN_COUNTEREXAMPLE_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "minLength": 1},
        "scenario": {"type": "string"},
    },
    "required": ["description"],
    "additionalProperties": False,
}

_PROPERTY_PATTERN_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "pattern_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "category": {"type": "string", "enum": ["safety", "liveness", "fairness"]},
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "formula_template": {"type": "string", "minLength": 1},
        "slots": {"type": "array", "items": _PROPERTY_PATTERN_SLOT_SCHEMA, "minItems": 1},
        "applicable_conditions": {"type": "array", "items": {"type": "string"}},
        "counterexamples": {"type": "array", "items": _PROPERTY_PATTERN_COUNTEREXAMPLE_SCHEMA},
        "metadata": {"type": "object"},
    },
    "required": ["pattern_id", "category", "title", "formula_template", "slots"],
    "additionalProperties": False,
}

_KRIPKE_VERDICT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "properties_checked": {"type": "integer", "minimum": 0},
        "properties_passed": {"type": "integer", "minimum": 0},
        "failed_properties": {"type": "array", "items": {"type": "string"}},
        "counterexamples": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["verified", "properties_checked", "properties_passed"],
    "additionalProperties": False,
}

_EVIDENCE_ENTRY_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "minLength": 1},
        "event_type": {"type": "string", "minLength": 1},
        "data": {"type": "object"},
        "timestamp": _DATE_TIME_SCHEMA,
    },
    "required": ["source", "event_type"],
    "additionalProperties": False,
}

_PROOF_REPORT_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "report_id": {"type": "string", "minLength": 1},
        "intent_id": {"type": "string", "minLength": 1},
        "cycle_id": {"type": "string", "minLength": 1},
        "pre_snapshot": {"type": "object"},
        "post_snapshot": {"type": "object"},
        "execution_summary": {"type": "object"},
        "kripke_verdict": _KRIPKE_VERDICT_SCHEMA,
        "evidence_chain": {"type": "array", "items": _EVIDENCE_ENTRY_SCHEMA},
        "policy_decisions": {"type": "array", "items": {"type": "object"}},
        "overall_verdict": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
        "verdict_reason": {"type": "string"},
        "created_at": _DATE_TIME_SCHEMA,
        "report_hash": {"type": "string"},
        "peer_node_id": {"type": "string"},
        "peer_chain_ref": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": ["report_id", "intent_id", "cycle_id", "overall_verdict"],
    "additionalProperties": False,
}

_ESCALATION_VERDICT_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "kpi": {"type": "string", "minLength": 1},
        "trend": {
            "type": "string",
            "enum": ["improving", "stalled", "worsening", "insufficient_data"],
        },
        "converged": {"type": "boolean"},
        "delta": {"type": "number"},
        "values": {"type": "array", "items": {"type": "number"}},
        "reason": {"type": "string"},
    },
    "required": ["kpi", "trend"],
    "additionalProperties": False,
}

_ESCALATION_REPORT_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "report_id": {"type": "string", "minLength": 1},
        "timestamp": _DATE_TIME_SCHEMA,
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "intent_ref": {"type": "string", "minLength": 1},
        "verdicts": {"type": "array", "items": _ESCALATION_VERDICT_SCHEMA},
        "kpi_history_count": {"type": "integer", "minimum": 0},
        "recent_rules_fired": {"type": "array", "items": {"type": "string"}},
        "recent_errors": {"type": "array", "items": {"type": "string"}},
        "gap_summary": {"type": "string"},
        "suggested_actions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "tighten_thresholds",
                    "add_new_rule",
                    "update_knowledge",
                    "retrain_model",
                    "human_review",
                    "recompile_bundle",
                    "adjust_kripke_properties",
                ],
            },
        },
        "counterexample": {"type": ["object", "null"]},
        "peer_node_id": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": ["report_id", "timestamp", "severity", "intent_ref"],
    "additionalProperties": False,
}

_AGENT_NETWORK_TRANSPORTS = ["inprocess", "http", "file_queue"]
_AGENT_NETWORK_KPI_KEY_PATTERN = r"^[a-z][a-z0-9_.]*$"

_AGENT_NETWORK_EDGE_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "from": {"type": "string", "minLength": 1},
        "to": {"type": "string", "minLength": 1},
        "kpi_keys": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "pattern": _AGENT_NETWORK_KPI_KEY_PATTERN},
        },
        "allowed_patches": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "transport": {"type": "string", "enum": _AGENT_NETWORK_TRANSPORTS},
        "description": {"type": "string"},
    },
    "required": ["from", "to", "kpi_keys"],
    "additionalProperties": False,
}

_AGENT_NETWORK_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "network_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "topology": {"type": "string", "enum": ["star", "tree"]},
        "center_bundle_ref": {"type": "string", "minLength": 1},
        "leaf_bundle_refs": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "edges": {"type": "array", "minItems": 1, "items": _AGENT_NETWORK_EDGE_SCHEMA},
        "transport_default": {"type": "string", "enum": _AGENT_NETWORK_TRANSPORTS},
        "description": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": [
        "network_id",
        "topology",
        "center_bundle_ref",
        "leaf_bundle_refs",
        "edges",
        "transport_default",
    ],
    "additionalProperties": False,
}

# IntentSession (K-Ontology v0.4 / Phase 7 Stage 7-2). The step / action /
# status enums mirror the frozen contract in tm/intent/design_loop.py.
_INTENT_SESSION_TURN_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "seq": {"type": "integer", "minimum": 0},
        "role": {"type": "string", "enum": ["human", "agent"]},
        "action": {
            "type": "string",
            "enum": ["propose", "refine", "check_5w1h", "verify", "accept", "clarify", "note"],
        },
        "input_ref": {"type": "string", "minLength": 1},
        "output_ref": {"type": "string", "minLength": 1},
        "provider": {"type": "string", "minLength": 1},
        "turn_hash": {"type": "string", "minLength": 1},
    },
    "required": ["seq", "role", "action"],
    "additionalProperties": False,
}

_INTENT_SESSION_SIGN_OFF_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "signer": {"type": "string", "minLength": 1},
        "scope": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "completeness_snapshot": {"type": "object"},
        "dispositions": {"type": "object"},
        "gate_report_hash": {"type": "string", "minLength": 1},
        "signed_at": _DATE_TIME_SCHEMA,
        "sign_hash": {"type": "string", "minLength": 1},
    },
    "required": ["signer"],
    "additionalProperties": False,
}

_INTENT_SESSION_SPEC_SCHEMA: Schema = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
        "root_intent_ref": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["working", "sealed"]},
        "current_step": {
            "type": "string",
            "enum": ["draft", "check_5w1h", "propose", "refine", "verify", "accept", "sealed"],
        },
        "turns": {"type": "array", "items": _INTENT_SESSION_TURN_SCHEMA},
        "completeness": {"type": "object"},
        "produced_refs": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "sign_off": _INTENT_SESSION_SIGN_OFF_SCHEMA,
        "metadata": {"type": "object"},
    },
    "required": ["session_id", "root_intent_ref", "status", "current_step"],
    "additionalProperties": False,
}

SCHEMAS: Mapping[str, Schema] = {
    "IntentSpec": _INTENT_SPEC_SCHEMA,
    "CapabilitySpec": _CAPABILITY_SPEC_SCHEMA,
    "PolicySpec": _POLICY_SPEC_SCHEMA,
    "WorkflowPolicy": _WORKFLOW_POLICY_SCHEMA,
    "ExecutionTrace": _EXECUTION_TRACE_SCHEMA,
    "IntegratedStateReport": _INTEGRATED_STATE_REPORT_SCHEMA,
    "PatchProposal": _PATCH_PROPOSAL_SCHEMA,
    "PropertyPatternSpec": _PROPERTY_PATTERN_SPEC_SCHEMA,
    "ProofReportSpec": _PROOF_REPORT_SPEC_SCHEMA,
    "EscalationReportSpec": _ESCALATION_REPORT_SPEC_SCHEMA,
    "AgentNetworkSpec": _AGENT_NETWORK_SPEC_SCHEMA,
    "IntentSessionSpec": _INTENT_SESSION_SPEC_SCHEMA,
}
