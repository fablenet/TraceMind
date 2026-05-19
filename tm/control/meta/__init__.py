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
    EscalationReport,
    Escalator,
    Severity,
    SuggestedAction,
)
from tm.control.meta.kpi_tracker import CycleRecord, KpiTracker
from tm.control.meta.proof import (
    EvidenceEntry,
    KripkeVerdict,
    ProofReport,
    ProofReportGenerator,
    SnapshotDiff,
    SnapshotDiffEntry,
    Verdict,
    build_hash_chain,
    diff_snapshots,
    verify_hash_chain,
)

__all__ = [
    "ConvergenceDetector",
    "ConvergenceVerdict",
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
    "ProofReport",
    "ProofReportGenerator",
    "Severity",
    "SnapshotDiff",
    "SnapshotDiffEntry",
    "SuggestedAction",
    "Trend",
    "UNSPECIFIED_INTENT_REF",
    "Verdict",
    "build_hash_chain",
    "diff_snapshots",
    "make_l1_runner",
    "verify_hash_chain",
]
