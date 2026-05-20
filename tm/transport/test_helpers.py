"""Failure-injection harness for testing transport-dependent code.

:class:`FailureInjectingTransport` wraps any concrete :class:`Transport`
implementation and applies a configurable matrix of fault kinds to the
``send`` / ``recv`` / ``request`` / ``broadcast`` operations. This is the
public interface used by Stage 6-2.4 and consumed by Stage 6-3 cross-node
tests to verify the remote ``AgentRuntime`` resolver escalates correctly
under each failure mode.

Fault kinds (Phase 6 §6-2.4)
----------------------------
- ``drop``       — silently swallow the outgoing call (the receiver never
                   sees the message). Implementations choose the failure
                   mode: ``send`` returns ``None`` as if successful;
                   ``request`` raises :class:`TransportNetworkError` (the
                   caller is blocking on a reply that will never come).
- ``delay``      — sleep ``seconds`` (float) before forwarding to the
                   wrapped transport. Useful for timeout testing.
- ``corrupt``    — mutate the outgoing payload before forwarding. Default
                   mutation prepends ``"__corrupt__"`` to every top-level
                   string value; callers may pass ``transform=callable`` to
                   override.
- ``partition``  — refuse to talk to peers in ``blocked_peers`` set; raises
                   :class:`TransportNetworkError`.
- ``duplicate``  — forward the call twice. ``send`` enqueues twice;
                   ``request`` invokes the wrapped transport twice and
                   returns the first reply (the second is discarded but
                   observed by the receiver, which exercises idempotency).

Usage::

    from tm.transport import HttpTransport, FailureInjectingTransport
    inner = HttpTransport(my_peer_id="leaf", peer_endpoints={...})
    flaky = FailureInjectingTransport(inner)
    flaky.inject_failure("delay", {"seconds": 0.5})
    flaky.inject_failure("partition", {"blocked_peers": {"center"}})
    # ... wire flaky into the agent under test ...
    flaky.clear_failures()

The harness preserves all incidental behavior of the wrapped transport
(peer registry, in-process handlers, retry policy, etc.) — it intercepts
only the four wire methods.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Set

from tm.transport.base import (
    RequestHandler,
    Transport,
    TransportError,
    TransportNetworkError,
)

PayloadTransform = Callable[[Mapping[str, Any]], Mapping[str, Any]]


_FAULT_KINDS = frozenset({"drop", "delay", "corrupt", "partition", "duplicate"})


@dataclass
class FailureSpec:
    """One declarative fault. ``params`` is kind-specific (see module docstring)."""

    kind: str
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _FAULT_KINDS:
            raise ValueError(f"unknown fault kind '{self.kind}'; expected one of {sorted(_FAULT_KINDS)}")


def _default_corrupt_transform(message: Mapping[str, Any]) -> Mapping[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in message.items():
        if isinstance(value, str):
            out[key] = "__corrupt__" + value
        else:
            out[key] = value
    return out


class FailureInjectingTransport:
    """Wraps any :class:`Transport`, injecting the configured fault matrix.

    Faults are applied in registration order so callers can stack them:
    e.g. a ``delay`` followed by a ``corrupt`` will sleep first, then
    mutate. The wrapper is :class:`Transport`-protocol-conformant — it can
    be substituted anywhere a real transport is expected.
    """

    def __init__(
        self,
        inner: Transport,
        *,
        faults: Sequence[FailureSpec] | None = None,
    ) -> None:
        self._inner = inner
        self._faults: List[FailureSpec] = list(faults or [])

    # ─── Fault registration ─────────────────────────────────────────

    def inject_failure(self, kind: str, params: Mapping[str, Any] | None = None) -> None:
        self._faults.append(FailureSpec(kind=kind, params=dict(params or {})))

    def clear_failures(self) -> None:
        self._faults.clear()

    @property
    def faults(self) -> Sequence[FailureSpec]:
        return tuple(self._faults)

    @property
    def inner(self) -> Transport:
        return self._inner

    # ─── Transport Protocol ─────────────────────────────────────────

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        payload = dict(message)
        for fault in self._faults:
            payload = self._apply_outgoing(fault, peer_id, payload, op="send")
            if payload is _DROPPED:
                return
            if fault.kind == "duplicate":
                self._inner.send(peer_id, dict(payload))
        return self._inner.send(peer_id, payload)

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        for fault in self._faults:
            self._apply_passive(fault, peer_id, op="recv")
            if fault.kind == "drop":
                return None
        return self._inner.recv(peer_id, timeout_s=timeout_s)

    def broadcast(self, message: Mapping[str, Any]) -> None:
        payload = dict(message)
        for fault in self._faults:
            payload = self._apply_outgoing(fault, peer_id="<broadcast>", payload=payload, op="broadcast")
            if payload is _DROPPED:
                return
            if fault.kind == "duplicate":
                self._inner.broadcast(dict(payload))
        return self._inner.broadcast(payload)

    def request(
        self,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        payload = dict(message)
        duplicated_call = False
        for fault in self._faults:
            payload = self._apply_outgoing(fault, peer_id, payload, op="request")
            if payload is _DROPPED:
                raise TransportNetworkError(
                    f"request to '{peer_id}' dropped by failure injection",
                    peer_id=peer_id,
                )
            if fault.kind == "duplicate":
                duplicated_call = True

        if duplicated_call:
            # Fire the duplicate first; its reply is discarded but the
            # receiver sees both calls (exercising idempotency_key dedup).
            try:
                self._inner.request(
                    peer_id,
                    dict(payload),
                    timeout_s=timeout_s,
                    idempotency_key=idempotency_key,
                )
            except TransportError:
                # Swallow — the duplicate is best-effort; the real reply
                # comes from the second call below.
                pass

        return self._inner.request(
            peer_id,
            payload,
            timeout_s=timeout_s,
            idempotency_key=idempotency_key,
        )

    def peers(self) -> Iterable[str]:
        return self._inner.peers()

    # ─── Optional pass-through hooks (delegated when supported) ─────

    def register_request_handler(self, peer_id: str, handler: RequestHandler) -> None:
        register = getattr(self._inner, "register_request_handler", None)
        if register is None:
            raise TransportError(
                f"wrapped transport {type(self._inner).__name__} does not support " "register_request_handler"
            )
        register(peer_id, handler)

    def unregister_request_handler(self, peer_id: str) -> None:
        unregister = getattr(self._inner, "unregister_request_handler", None)
        if unregister is None:
            return
        unregister(peer_id)

    # ─── Internals ──────────────────────────────────────────────────

    def _apply_outgoing(
        self,
        fault: FailureSpec,
        peer_id: str,
        payload: Mapping[str, Any],
        *,
        op: str,
    ) -> Mapping[str, Any] | Any:
        if fault.kind == "drop":
            return _DROPPED
        if fault.kind == "delay":
            seconds = float(fault.params.get("seconds", 0.0))
            if seconds > 0:
                time.sleep(seconds)
            return payload
        if fault.kind == "corrupt":
            transform = fault.params.get("transform") or _default_corrupt_transform
            return dict(transform(payload))
        if fault.kind == "partition":
            blocked: Set[str] = set(fault.params.get("blocked_peers", ()) or ())
            if peer_id in blocked or "<all>" in blocked:
                raise TransportNetworkError(
                    f"partition fault: peer '{peer_id}' is in blocked set",
                    peer_id=peer_id,
                )
            return payload
        if fault.kind == "duplicate":
            return payload
        return payload  # never reached

    def _apply_passive(self, fault: FailureSpec, peer_id: str, *, op: str) -> None:
        if fault.kind == "partition":
            blocked: Set[str] = set(fault.params.get("blocked_peers", ()) or ())
            if peer_id in blocked or "<all>" in blocked:
                raise TransportNetworkError(
                    f"partition fault: peer '{peer_id}' is in blocked set",
                    peer_id=peer_id,
                )


# Sentinel returned by ``_apply_outgoing`` to signal "drop the call".
class _Dropped:
    __slots__ = ()


_DROPPED = _Dropped()


__all__ = [
    "FailureInjectingTransport",
    "FailureSpec",
    "PayloadTransform",
]
