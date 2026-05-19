"""Transport protocol seam for agent-to-agent communication.

This module defines the **Transport** abstraction used by the L1 MAPE-K
agents (Observe / Analyze / Decide / Act base classes in
``tm.control.agents.base``). In Phase 5 the only implementation is
:class:`InProcessTransport`, which is a synchronous in-memory mailbox; that
is sufficient for a single-process AgentBundle.

**Phase 6 forward-compatibility**: the design splits "what an agent says to
its peers" (this protocol) from "how the words travel" (the implementation).
Phase 6 will add ``HttpTransport``, ``GrpcTransport``, and
``FileQueueTransport`` as drop-in replacements so that an AgentNetwork can
be deployed in star / tree topologies across processes or hosts. The seam
exists now even though it has only one realization, so that adding remote
implementations later is purely additive (no agent-side changes).

The Transport API is deliberately minimal:

- ``send(peer_id, message)``: point-to-point delivery
- ``recv(peer_id, timeout_s=None)``: pull-mode receive
- ``broadcast(message)``: best-effort to all known peers

Phase 6 will also need request/response correlation; that is intentionally
out of scope here. Adding it requires a new method, not a breaking change.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, Iterable, Mapping, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Agent-to-agent communication protocol.

    All implementations must be thread-safe across the methods they support;
    in Phase 5 we only run single-threaded so this is informational. Phase 6
    will need to revisit this for HTTP/gRPC implementations.
    """

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        """Send a message to ``peer_id``. Implementations MAY drop unknown peers."""
        ...

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        """Receive the next pending message for ``peer_id``.

        Returns ``None`` if no message is available within ``timeout_s``
        (``None`` means non-blocking). Implementations decide FIFO vs other
        ordering; :class:`InProcessTransport` is FIFO.
        """
        ...

    def broadcast(self, message: Mapping[str, Any]) -> None:
        """Best-effort delivery to all known peers."""
        ...

    def peers(self) -> Iterable[str]:
        """List currently-known peer ids. Used for diagnostics + broadcast routing."""
        ...


class InProcessTransport:
    """Synchronous, in-memory transport — the default for single-process bundles.

    Each peer has its own FIFO mailbox. ``send`` appends, ``recv`` pops the
    head non-blockingly (``timeout_s`` is ignored — there's no thread to
    block on). ``broadcast`` enqueues onto every peer's mailbox including
    peers we've never sent to before (they're added lazily).

    Phase 6 will add ``HttpTransport`` etc. that satisfy the same protocol.
    """

    def __init__(self, *, known_peers: Iterable[str] | None = None) -> None:
        self._mailboxes: Dict[str, deque[Dict[str, Any]]] = defaultdict(deque)
        if known_peers is not None:
            for peer_id in known_peers:
                self._mailboxes[peer_id]

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        self._mailboxes[peer_id].append(dict(message))

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        # timeout_s is ignored; this is non-blocking by design.
        del timeout_s
        mailbox = self._mailboxes.get(peer_id)
        if not mailbox:
            return None
        return mailbox.popleft()

    def broadcast(self, message: Mapping[str, Any]) -> None:
        for peer_id in list(self._mailboxes.keys()):
            self._mailboxes[peer_id].append(dict(message))

    def peers(self) -> Iterable[str]:
        return tuple(self._mailboxes.keys())

    def pending_count(self, peer_id: str) -> int:
        """Diagnostics helper: how many messages queued for ``peer_id``."""
        return len(self._mailboxes.get(peer_id, ()))


__all__ = ["Transport", "InProcessTransport"]
