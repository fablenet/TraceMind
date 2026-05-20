"""Meta-control layer (L2): KPI tracking, convergence detection, escalation, proof.

This package contains the **domain-neutral** L2 meta-control plumbing that
was extracted from ``fablenet-control`` in Phase 5 Stage 5-2. Each module
is independently usable; the barrel re-exports the most common symbols.

Construction discipline: every constructor that previously hard-coded a
domain-specific default now defaults to a neutral placeholder; callers
should supply domain identifiers explicitly.
"""

from tm.control.meta.controller import (
    L1CycleResult,
    L1Runner,
    MetaController,
    MetaControllerResult,
)
from tm.control.meta.convergence import (
    ConvergenceDetector,
    ConvergenceVerdict,
    Trend,
)
from tm.control.meta.cycle_bridge import make_l1_runner
from tm.control.meta.escalation import (
    UNSPECIFIED_INTENT_REF,
    CrossNodeEscalationReport,
    EscalationReport,
    Escalator,
    NetworkEscalator,
    Severity,
    SuggestedAction,
)
from tm.control.meta.kpi_tracker import CycleRecord, KpiTracker
from tm.control.meta.network import (
    AgentNetworkTopology,
    NetworkController,
    NetworkCycleResult,
    make_leaf_cycle_handler,
)
from tm.control.meta.proof import (
    EvidenceEntry,
    KripkeVerdict,
    ProofReport,
    ProofReportGenerator,
    SnapshotDiff,
    SnapshotDiffEntry,
    Verdict,
    attach_peer_proofs,
    build_hash_chain,
    diff_snapshots,
    verify_hash_chain,
    verify_peer_chain,
)

__all__ = [
    "AgentNetworkTopology",
    "ConvergenceDetector",
    "ConvergenceVerdict",
    "CrossNodeEscalationReport",
    "CycleRecord",
    "EscalationReport",
    "Escalator",
    "EvidenceEntry",
    "KpiTracker",
    "KripkeVerdict",
    "L1CycleResult",
    "L1Runner",
    "MetaController",
    "MetaControllerResult",
    "NetworkController",
    "NetworkCycleResult",
    "NetworkEscalator",
    "ProofReport",
    "ProofReportGenerator",
    "Severity",
    "SnapshotDiff",
    "SnapshotDiffEntry",
    "SuggestedAction",
    "Trend",
    "UNSPECIFIED_INTENT_REF",
    "Verdict",
    "attach_peer_proofs",
    "build_hash_chain",
    "diff_snapshots",
    "make_l1_runner",
    "make_leaf_cycle_handler",
    "verify_hash_chain",
    "verify_peer_chain",
]
