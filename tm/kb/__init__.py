"""Knowledge Base — Stage 5-4 deliverables.

This package hosts the **Case Corpus** (task 4.2), the **Retrieval layer**
(task 4.3) and the **Feedback loop** (task 4.4). All three are designed
as virtual views / additive indices over existing K-Ontology artifacts:
nothing here promotes new artifact types.

Public API:

- :class:`Case`, :class:`CaseEvidence`, :class:`CaseCorpus`,
  :func:`build_case_corpus` — the case-aggregation view (4.2)
"""

from .case_corpus import (
    Case,
    CaseCorpus,
    CaseEvidence,
    build_case_corpus,
)
from .feedback import (
    FeedbackSignal,
    collect_feedback_signals,
    run_feedback_loop,
    synthesize_kb_patch_proposal,
)
from .retrieval import (
    CaseStructuredRetriever,
    PatternKeywordRetriever,
    RetrievalBundle,
    RetrievalHit,
    Retriever,
    VectorRetriever,
    make_default_bundle,
    ripgrep_available,
    ripgrep_search,
)

__all__ = [
    "Case",
    "CaseCorpus",
    "CaseEvidence",
    "CaseStructuredRetriever",
    "FeedbackSignal",
    "PatternKeywordRetriever",
    "RetrievalBundle",
    "RetrievalHit",
    "Retriever",
    "VectorRetriever",
    "build_case_corpus",
    "collect_feedback_signals",
    "make_default_bundle",
    "ripgrep_available",
    "ripgrep_search",
    "run_feedback_loop",
    "synthesize_kb_patch_proposal",
]
