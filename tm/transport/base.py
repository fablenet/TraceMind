"""Transport protocol seam for agent-to-agent communication.

Lifted from ``tm.control.agents.transport`` in Phase 6 Stage 6-2.1; the old
module path remains as a thin re-export so Phase 5 users (the L1 MAPE-K
agent base classes, ``tm.policy.transports``, the existing test suite) keep
working without modification.

The transport abstraction splits "what an agent says to its peers" (this
protocol) from "how the words travel" (the implementation). Stage 6-2 adds
two production-ready implementations alongside the in-process default:

- :class:`tm.transport.http.HttpTransport` — HTTP/1.1 over ``httpx``,
  designed to layer cleanly on top of the existing tm-server v1 API
- :class:`tm.transport.file_queue.FileQueueTransport` — local filesystem
  queue with at-least-once delivery and idempotency-keyed dedup, suitable
  for audit / debug / disconnected-leaf scenarios

The Transport API:

- ``send(peer_id, message)``: point-to-point fire-and-forget delivery
- ``recv(peer_id, timeout_s=None)``: pull-mode receive from a local mailbox
- ``broadcast(message)``: best-effort to all known peers
- ``peers()``: list known peer ids (diagnostics + broadcast routing)
- ``request(peer_id, message, *, timeout_s, idempotency_key)``: synchronous
  request/response. Optional — implementations that don't support it raise
  :class:`TransportError`. New in Stage 6-2 (was reserved as a hook in the
  Phase 5 docstring).

Error hierarchy:

- :class:`TransportError` — base; wraps any I/O-side or protocol-side failure
- :class:`TransportTimeout` — operation exceeded the deadline
- :class:`TransportNetworkError` — connection / dispatch failure (unreachable
  peer, HTTP 5xx, filesystem permission denied, etc.)

These errors are domain-typed so that the remote ``AgentRuntime`` resolver
(Stage 6-2.3) can map them onto :class:`EscalationReportBody` without
catching bare :class:`Exception`.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Callable, Dict, Iterable, Mapping, Protocol, runtime_checkable


class TransportError(RuntimeError):
    """Base class for any transport-side failure (network, protocol, timeout)."""


class TransportTimeout(TransportError):
    """Raised when an operation exceeds its deadline."""


class TransportNetworkError(TransportError):
    """Raised when a transport cannot reach the requested peer.

    The ``peer_id`` attribute holds the addressee whose delivery failed.
    The remote ``AgentRuntime`` resolver translates this into an
    ``EscalationReportBody`` rather than swallowing the failure silently.
    """

    def __init__(self, message: str, *, peer_id: str | None = None) -> None:
        super().__init__(message)
        self.peer_id = peer_id


@runtime_checkable
class Transport(Protocol):
    """Agent-to-agent communication protocol.

    Implementations MUST be thread-safe across the methods they support;
    Phase 5 was single-threaded so this was informational. Phase 6 adds
    HTTP and FileQueue transports that may be invoked from multiple cycles
    concurrently — each implementation documents its concurrency model.
    """

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        """Fire-and-forget delivery to ``peer_id``."""
        ...

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        """Receive the next pending message for ``peer_id`` from the local mailbox.

        Returns ``None`` if no message is available within ``timeout_s``
        (``None`` means non-blocking). Implementations decide ordering;
        :class:`InProcessTransport` and :class:`FileQueueTransport` are FIFO.
        """
        ...

    def broadcast(self, message: Mapping[str, Any]) -> None:
        """Best-effort delivery to every currently-known peer."""
        ...

    def peers(self) -> Iterable[str]:
        """List currently-known peer ids (diagnostics + broadcast routing)."""
        ...

    def request(
        self,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        """Synchronous request/response to ``peer_id`` returning the peer's reply.

        Implementations that don't support synchronous RPC raise
        :class:`TransportError`. ``idempotency_key``, when set, lets the
        receiver de-duplicate retried requests (important for at-least-once
        transports such as :class:`FileQueueTransport`).
        """
        ...


RequestHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class InProcessTransport:
    """Synchronous, in-memory transport — the default for single-process bundles.

    Each peer has its own FIFO mailbox. ``send`` appends, ``recv`` pops the
    head non-blockingly (``timeout_s`` is ignored — there's no thread to
    block on). ``broadcast`` enqueues onto every known peer's mailbox,
    including peers we've never sent to before (they're added lazily).

    Stage 6-2 adds optional in-process RPC support via
    :meth:`register_request_handler` — invoke ``register_request_handler(peer_id, fn)``
    to make ``request(peer_id, msg)`` route directly to ``fn(msg)``.
    Without a registered handler, ``request`` raises :class:`TransportNetworkError`.

    Concurrency: not thread-safe. Single-process bundles invoke this
    serially from one cycle. If a future caller needs concurrent access,
    wrap with an external lock.
    """

    def __init__(
        self,
        *,
        known_peers: Iterable[str] | None = None,
        request_handlers: Mapping[str, RequestHandler] | None = None,
    ) -> None:
        self._mailboxes: Dict[str, deque[Dict[str, Any]]] = defaultdict(deque)
        self._handlers: Dict[str, RequestHandler] = dict(request_handlers or {})
        if known_peers is not None:
            for peer_id in known_peers:
                self._mailboxes[peer_id]

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        self._mailboxes[peer_id].append(dict(message))

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        del timeout_s  # non-blocking by design
        mailbox = self._mailboxes.get(peer_id)
        if not mailbox:
            return None
        return mailbox.popleft()

    def broadcast(self, message: Mapping[str, Any]) -> None:
        for peer_id in list(self._mailboxes.keys()):
            self._mailboxes[peer_id].append(dict(message))

    def peers(self) -> Iterable[str]:
        return tuple(self._mailboxes.keys())

    def request(
        self,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        del timeout_s, idempotency_key  # InProcess is synchronous + already-ordered
        handler = self._handlers.get(peer_id)
        if handler is None:
            raise TransportNetworkError(
                f"InProcessTransport has no request handler for peer '{peer_id}'",
                peer_id=peer_id,
            )
        return dict(handler(dict(message)))

    def register_request_handler(self, peer_id: str, handler: RequestHandler) -> None:
        """Register a synchronous request handler for ``peer_id``.

        After registration, callers may invoke ``request(peer_id, msg)`` and
        receive the handler's return value. Useful for in-process testing of
        remote-runtime agents without needing a real HTTP / filesystem peer.
        """
        self._handlers[peer_id] = handler

    def unregister_request_handler(self, peer_id: str) -> None:
        self._handlers.pop(peer_id, None)

    def pending_count(self, peer_id: str) -> int:
        """Diagnostics: how many messages queued for ``peer_id``."""
        return len(self._mailboxes.get(peer_id, ()))


__all__ = [
    "InProcessTransport",
    "RequestHandler",
    "Transport",
    "TransportError",
    "TransportNetworkError",
    "TransportTimeout",
]
