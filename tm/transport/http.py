"""``HttpTransport`` — HTTP/1.1 transport over ``httpx``.

Companion implementation to :class:`tm.transport.base.InProcessTransport`.
The wire protocol is intentionally minimal and JSON-only so any
language-runtime that speaks HTTP/1.1 can interoperate (FableNet's
polyglot invariant from Phase 5 §6.7).

Wire format (envelope sent on every HTTP request body):

::

    {
      "from": "<sender peer_id>",
      "to": "<recipient peer_id>",
      "kind": "send" | "request",
      "body": { ... opaque message payload ... },
      "idempotency_key": "<optional string for receiver-side dedup>",
      "correlation_id": "<optional client-supplied request id>"
    }

Endpoint mapping:

- ``POST {peer_endpoint}/_transport/inbox`` — fire-and-forget delivery
  (server enqueues into the recipient's local mailbox; reply is HTTP 202)
- ``POST {peer_endpoint}/_transport/rpc`` — synchronous RPC (server
  invokes a request handler and returns the reply JSON; reply is HTTP 200)

Server-side dispatch is provided by :func:`make_inbox_handler` and
:func:`make_rpc_handler`: they convert raw HTTP bodies into the local
:class:`HttpTransport`'s in-memory inbox / handler registry. The actual
FastAPI route registration is deferred to Stage 6-3 so that this module
has zero hard dependency on the tm-server package (and stays useful for
tests / scripts that want a transport without a running server).

Concurrency: the local inbox is protected by an internal lock. The
``httpx.Client`` is safe to share across threads for synchronous use
(see httpx docs).

Retry semantics: a configurable :class:`RetryPolicy` governs ``send`` and
``request``. By default, requests retry on :class:`httpx.NetworkError`
(connect / read failures) with capped exponential backoff. HTTP 4xx
responses are **never** retried — they indicate a deterministic client-side
problem that retrying cannot fix. HTTP 5xx responses retry up to the
configured limit.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, MutableMapping

import httpx

from tm.transport.base import (
    RequestHandler,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Capped exponential backoff for transient failures.

    A retry is attempted only on :class:`httpx.NetworkError` and HTTP 5xx
    responses. The backoff at attempt ``n`` (1-indexed) is
    ``min(base_backoff_s * 2**(n-1), max_backoff_s)``. ``max_attempts``
    includes the first try, so ``max_attempts=3`` means: try once, then
    up to 2 retries.
    """

    max_attempts: int = 3
    base_backoff_s: float = 0.1
    max_backoff_s: float = 2.0

    def backoff_for(self, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        return min(self.base_backoff_s * (2 ** (attempt - 1)), self.max_backoff_s)


@dataclass
class _Mailbox:
    """Per-peer FIFO mailbox protected by a lock."""

    queue: deque[Dict[str, Any]] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)


class HttpTransport:
    """HTTP/1.1 ``Transport`` implementation backed by ``httpx``.

    ``peer_endpoints`` maps a peer_id to its base URL (e.g.
    ``"http://leaf-1.cluster.local:8080"``). The transport appends
    ``/_transport/inbox`` and ``/_transport/rpc`` to the base URL when
    talking to peers.

    Parameters
    ----------
    my_peer_id
        This node's peer_id; written into the ``from`` field of every
        outgoing envelope so the receiver can audit provenance.
    peer_endpoints
        Mapping from peer_id to base URL.
    client
        Optional pre-built ``httpx.Client`` (useful for tests that need
        ``httpx.MockTransport``). If not supplied, a default client with
        ``timeout=timeout_s`` is created.
    timeout_s
        Default per-request timeout, in seconds. Overridable per-call via
        ``request(timeout_s=...)``.
    retry_policy
        :class:`RetryPolicy` for ``send`` and ``request``. Defaults to
        ``RetryPolicy()``.
    auth_header
        Optional ``Authorization`` header value (e.g.
        ``"Bearer abc..."``). Applied to every outgoing request.
    """

    INBOX_PATH = "/_transport/inbox"
    RPC_PATH = "/_transport/rpc"

    def __init__(
        self,
        *,
        my_peer_id: str,
        peer_endpoints: Mapping[str, str],
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
        retry_policy: RetryPolicy | None = None,
        auth_header: str | None = None,
    ) -> None:
        if not my_peer_id:
            raise ValueError("my_peer_id must be a non-empty string")
        self._my_id = my_peer_id
        self._endpoints: MutableMapping[str, str] = dict(peer_endpoints)
        self._timeout_s = float(timeout_s)
        self._retry = retry_policy or RetryPolicy()
        self._auth = auth_header
        self._client = client if client is not None else httpx.Client(timeout=self._timeout_s)
        self._owns_client = client is None
        self._inboxes: Dict[str, _Mailbox] = {}
        self._handlers: Dict[str, RequestHandler] = {}
        self._dedup_seen: Dict[str, Dict[str, Any]] = {}
        self._dedup_lock = threading.Lock()

    # ─── Outgoing (client side) ─────────────────────────────────────

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        url = self._endpoint_url(peer_id, self.INBOX_PATH)
        envelope = self._make_envelope("send", peer_id, message)
        self._post_with_retry(url, envelope, peer_id, timeout_s=self._timeout_s)

    def broadcast(self, message: Mapping[str, Any]) -> None:
        last_err: TransportError | None = None
        for peer_id in list(self._endpoints.keys()):
            if peer_id == self._my_id:
                continue
            try:
                self.send(peer_id, message)
            except TransportError as exc:
                last_err = exc
        if last_err is not None:
            # Best-effort: bubble the most recent failure for visibility.
            raise last_err

    def request(
        self,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        url = self._endpoint_url(peer_id, self.RPC_PATH)
        envelope = self._make_envelope("request", peer_id, message, idempotency_key=idempotency_key)
        response = self._post_with_retry(url, envelope, peer_id, timeout_s=timeout_s or self._timeout_s)
        try:
            return dict(response.json())
        except ValueError as exc:
            raise TransportError(f"RPC reply from '{peer_id}' is not valid JSON: {exc}") from exc

    def peers(self) -> Iterable[str]:
        return tuple(self._endpoints.keys())

    # ─── Incoming (server side hooks) ───────────────────────────────

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        del timeout_s  # non-blocking: server-side handlers populate the inbox synchronously
        mailbox = self._inboxes.get(peer_id)
        if mailbox is None:
            return None
        with mailbox.lock:
            if not mailbox.queue:
                return None
            return mailbox.queue.popleft()

    def push_inbox(self, envelope: Mapping[str, Any]) -> None:
        """Server-side hook: deposit an inbound envelope into the local mailbox.

        Mounted FastAPI routes call this from within their request handler.
        Dedup is applied per ``(peer_id, idempotency_key)`` tuple: if the
        same key from the same sender is observed twice, the second arrival
        is silently dropped (at-least-once → exactly-once semantics).
        """
        peer_id = str(envelope.get("from") or "")
        if not peer_id:
            raise TransportError("inbound envelope is missing 'from' field")
        if self._is_duplicate(peer_id, envelope):
            return
        mailbox = self._inboxes.setdefault(peer_id, _Mailbox())
        with mailbox.lock:
            mailbox.queue.append(dict(envelope))

    def handle_rpc(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Server-side hook: dispatch an RPC envelope to a registered handler.

        Returns the handler's reply (which the HTTP route serializes back to
        the caller). Raises :class:`TransportNetworkError` when no handler
        is registered — the route should translate that into an HTTP 503.
        Dedup is applied identically to :meth:`push_inbox`; a duplicate RPC
        replays the previously-computed reply (idempotent at-least-once).
        """
        peer_id = str(envelope.get("from") or "")
        if not peer_id:
            raise TransportError("inbound RPC envelope is missing 'from' field")
        body = envelope.get("body") or {}
        if not isinstance(body, Mapping):
            raise TransportError("inbound RPC envelope body must be a mapping")
        cached = self._cached_reply(peer_id, envelope)
        if cached is not None:
            return cached

        # Route by the local handler that corresponds to *our* role
        # in the conversation — the recipient is always ``self._my_id``,
        # but the handler key is configurable so multiple logical agents
        # can multiplex over the same HTTP transport.
        handler_key = str(envelope.get("to") or self._my_id)
        handler = self._handlers.get(handler_key)
        if handler is None:
            raise TransportNetworkError(
                f"no RPC handler registered for '{handler_key}'",
                peer_id=handler_key,
            )
        reply = dict(handler(dict(body)))
        self._record_reply(peer_id, envelope, reply)
        return reply

    def register_request_handler(self, peer_id: str, handler: RequestHandler) -> None:
        """Register a synchronous RPC handler keyed by the recipient's peer_id."""
        self._handlers[peer_id] = handler

    def unregister_request_handler(self, peer_id: str) -> None:
        self._handlers.pop(peer_id, None)

    # ─── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ─── Internals ───────────────────────────────────────────────────

    def _endpoint_url(self, peer_id: str, path: str) -> str:
        try:
            base = self._endpoints[peer_id]
        except KeyError as exc:
            raise TransportNetworkError(
                f"no endpoint registered for peer '{peer_id}'",
                peer_id=peer_id,
            ) from exc
        return base.rstrip("/") + path

    def _make_envelope(
        self,
        kind: str,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "from": self._my_id,
            "to": peer_id,
            "kind": kind,
            "body": dict(message),
        }
        if idempotency_key is not None:
            envelope["idempotency_key"] = idempotency_key
        return envelope

    def _headers(self) -> Dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._auth:
            headers["authorization"] = self._auth
        return headers

    def _post_with_retry(
        self,
        url: str,
        envelope: Mapping[str, Any],
        peer_id: str,
        *,
        timeout_s: float,
    ) -> httpx.Response:
        last_exc: BaseException | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                response = self._client.post(
                    url,
                    json=dict(envelope),
                    headers=self._headers(),
                    timeout=timeout_s,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt >= self._retry.max_attempts:
                    raise TransportTimeout(f"timeout after {attempt} attempt(s) to '{peer_id}': {exc}") from exc
                time.sleep(self._retry.backoff_for(attempt))
                continue
            except httpx.NetworkError as exc:
                last_exc = exc
                if attempt >= self._retry.max_attempts:
                    raise TransportNetworkError(
                        f"network error after {attempt} attempt(s) to '{peer_id}': {exc}",
                        peer_id=peer_id,
                    ) from exc
                time.sleep(self._retry.backoff_for(attempt))
                continue

            if 500 <= response.status_code < 600:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                if attempt >= self._retry.max_attempts:
                    raise TransportNetworkError(
                        f"peer '{peer_id}' returned HTTP {response.status_code} after {attempt} attempt(s)",
                        peer_id=peer_id,
                    )
                time.sleep(self._retry.backoff_for(attempt))
                continue

            if 400 <= response.status_code < 500:
                # Deterministic failure — do not retry.
                raise TransportError(f"peer '{peer_id}' returned HTTP {response.status_code}: {response.text[:200]}")
            return response

        # Defensive — loop above always either returns or raises.
        raise TransportError(f"unreachable: retry loop exited without resolution (last={last_exc!r})")

    def _dedup_box_key(self, sender_peer_id: str, idempotency_key: str) -> str:
        return f"{sender_peer_id}|{idempotency_key}"

    def _is_duplicate(self, sender_peer_id: str, envelope: Mapping[str, Any]) -> bool:
        idempotency_key = envelope.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return False
        with self._dedup_lock:
            box_key = self._dedup_box_key(sender_peer_id, idempotency_key)
            if box_key in self._dedup_seen:
                return True
            self._dedup_seen[box_key] = {}
            return False

    def _cached_reply(self, sender_peer_id: str, envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
        idempotency_key = envelope.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return None
        with self._dedup_lock:
            box_key = self._dedup_box_key(sender_peer_id, idempotency_key)
            cached = self._dedup_seen.get(box_key)
            if cached and "reply" in cached:
                return dict(cached["reply"])
        return None

    def _record_reply(
        self,
        sender_peer_id: str,
        envelope: Mapping[str, Any],
        reply: Mapping[str, Any],
    ) -> None:
        idempotency_key = envelope.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return
        with self._dedup_lock:
            box_key = self._dedup_box_key(sender_peer_id, idempotency_key)
            self._dedup_seen[box_key] = {"reply": dict(reply)}


# ─── Server-side helpers (framework-agnostic) ───────────────────────


InboxHandler = Callable[[Mapping[str, Any]], None]
RpcHandlerEndpoint = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def make_inbox_handler(transport: HttpTransport) -> InboxHandler:
    """Return a function suitable for mounting under a FastAPI route.

    The HTTP route is expected to parse the JSON request body itself and
    pass the resulting dict to the returned handler. The handler does not
    raise on duplicates (they are silently absorbed by the transport's
    idempotency dedup); it raises :class:`TransportError` on malformed
    envelopes, which the route should translate into HTTP 400.
    """

    def handler(envelope: Mapping[str, Any]) -> None:
        transport.push_inbox(envelope)

    return handler


def make_rpc_handler(transport: HttpTransport) -> RpcHandlerEndpoint:
    """Return a function suitable for mounting under a FastAPI RPC route.

    The HTTP route passes the parsed JSON envelope to the returned function
    and serializes the returned mapping back to the caller as JSON. Missing
    handlers surface as :class:`TransportNetworkError`, which the route
    should translate into HTTP 503.
    """

    def handler(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        return transport.handle_rpc(envelope)

    return handler


__all__ = [
    "HttpTransport",
    "InboxHandler",
    "RetryPolicy",
    "RpcHandlerEndpoint",
    "make_inbox_handler",
    "make_rpc_handler",
]
