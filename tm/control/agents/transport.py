"""Compatibility shim — the real implementation lives in ``tm.transport.base``.

The Transport seam was originally introduced in Phase 5 Stage 5-2 task 2.4
inside ``tm.control.agents.transport``. In Phase 6 Stage 6-2.1 the
implementation was lifted to the top-level ``tm.transport`` package so the
new ``HttpTransport`` / ``FileQueueTransport`` implementations can live
alongside it without nesting them under ``tm.control``. This module is kept
as a thin re-export so existing imports (``from tm.control.agents import
Transport, InProcessTransport``; ``from tm.control.agents.transport import
...``) continue to work without modification.

Prefer the new path in new code:

    from tm.transport import Transport, InProcessTransport
"""

from __future__ import annotations

from tm.transport.base import (
    InProcessTransport,
    RequestHandler,
    Transport,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)

__all__ = [
    "InProcessTransport",
    "RequestHandler",
    "Transport",
    "TransportError",
    "TransportNetworkError",
    "TransportTimeout",
]
