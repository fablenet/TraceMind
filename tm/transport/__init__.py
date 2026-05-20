"""Transport package — agent-to-agent communication implementations.

Phase 6 Stage 6-2 introduced the package to host the production-ready
transports (HTTP / FileQueue) alongside the original in-memory default.
The Phase 5 import path ``tm.control.agents.transport`` is kept as a thin
re-export so existing users (MAPE-K agent base classes, policy adapter
tests, etc.) continue to work without modification.

Implementations
---------------
- :class:`InProcessTransport` — synchronous in-memory mailboxes; default
  for single-process bundles. Supports optional in-process RPC via
  registered request handlers.
- :class:`HttpTransport` — HTTP/1.1 over ``httpx`` with configurable
  retry policy, idempotency-keyed dedup, and an auth header hook.
  Designed to layer on top of tm-server v1 routes (Stage 6-3 adds the
  ``/v1/network/...`` routes that mount it).
- :class:`FileQueueTransport` — local-filesystem queue with at-least-once
  delivery, atomic envelope writes, and a rolling idempotency-keyed
  dedup window. Useful for audit / debug / disconnected-leaf scenarios.

Error hierarchy
---------------
- :class:`TransportError` — base class
- :class:`TransportTimeout` — operation exceeded its deadline
- :class:`TransportNetworkError` — peer unreachable / dispatch failure;
  carries the ``peer_id`` for escalation routing

Testing
-------
- :class:`tm.transport.test_helpers.FailureInjectingTransport` wraps any
  ``Transport`` and applies a configurable matrix of fault kinds
  (drop / delay / corrupt / partition / duplicate). See Stage 6-2.4.
"""

from tm.transport.base import (
    InProcessTransport,
    RequestHandler,
    Transport,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)
from tm.transport.file_queue import FileQueueConfig, FileQueueTransport
from tm.transport.http import (
    HttpTransport,
    InboxHandler,
    RetryPolicy,
    RpcHandlerEndpoint,
    make_inbox_handler,
    make_rpc_handler,
)
from tm.transport.test_helpers import (
    FailureInjectingTransport,
    FailureSpec,
    PayloadTransform,
)

__all__ = [
    "FailureInjectingTransport",
    "FailureSpec",
    "FileQueueConfig",
    "FileQueueTransport",
    "HttpTransport",
    "InboxHandler",
    "InProcessTransport",
    "PayloadTransform",
    "RequestHandler",
    "RetryPolicy",
    "RpcHandlerEndpoint",
    "Transport",
    "TransportError",
    "TransportNetworkError",
    "TransportTimeout",
    "make_inbox_handler",
    "make_rpc_handler",
]
