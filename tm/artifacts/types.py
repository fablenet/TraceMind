from __future__ import annotations

from enum import Enum


class ArtifactType(str, Enum):
    INTENT = "intent"
    CAPABILITIES = "capabilities"
    PLAN = "plan"
    GAP_MAP = "gap_map"
    BACKLOG = "backlog"
    AGENT_BUNDLE = "agent_bundle"
    ENVIRONMENT_SNAPSHOT = "environment_snapshot"
    PROPOSED_CHANGE_PLAN = "proposed_change_plan"
    EXECUTION_REPORT = "execution_report"
    PROPERTY_PATTERN = "property_pattern"
    PROOF_REPORT = "proof_report"
    ESCALATION_REPORT = "escalation_report"
    AGENT_NETWORK = "agent_network"
