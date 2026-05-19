"""Generic MAPE-K agent base classes + Transport seam.

The four abstract base classes (Observe / Analyze / Decide / Act) in
:mod:`tm.control.agents.base` lift the MAPE-K skeleton out of any specific
domain so that K8s, FableNet, or any other downstream control scenario can
subclass them and fill in only the domain-specific hooks.

The :class:`Transport` protocol in :mod:`tm.control.agents.transport`
is the seam designed for Phase 6 AgentNetwork topologies — Phase 5 ships
the default :class:`InProcessTransport` so existing single-process bundles
keep working unchanged.
"""

from .base import (
    ActBaseAgent,
    ActOutcome,
    AnalyzeBaseAgent,
    DecideBaseAgent,
    ObserveBaseAgent,
)
from .transport import InProcessTransport, Transport

__all__ = [
    "ObserveBaseAgent",
    "AnalyzeBaseAgent",
    "DecideBaseAgent",
    "ActBaseAgent",
    "ActOutcome",
    "Transport",
    "InProcessTransport",
]
