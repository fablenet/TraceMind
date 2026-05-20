"""Tests for :class:`tm.transport.test_helpers.FailureInjectingTransport`.

Covers Phase 6 Stage 6-2.4: the five declared fault kinds
(drop / delay / corrupt / partition / duplicate) each apply correctly
across the four Transport operations (send / recv / request / broadcast)
and the wrapper itself remains :class:`Transport`-protocol-conformant.

Tests use :class:`InProcessTransport` as the wrapped inner transport so
faults are observable purely in-memory (no filesystem / HTTP needed) and
behavior is fully deterministic. The HTTP / FileQueue transports inherit
the same harness behavior because the wrapper is implementation-agnostic.
"""

from __future__ import annotations

import time
from typing import Any, List, Mapping

import pytest

from tm.transport import (
    FailureInjectingTransport,
    FailureSpec,
    InProcessTransport,
    Transport,
    TransportError,
    TransportNetworkError,
)

# ─── Construction & protocol conformance ──────────────────────────


class TestConstruction:
    def test_unknown_fault_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            FailureSpec(kind="explode")

    def test_wrapper_implements_transport_protocol(self) -> None:
        wrapped = FailureInjectingTransport(InProcessTransport())
        assert isinstance(wrapped, Transport)

    def test_faults_starts_empty(self) -> None:
        wrapped = FailureInjectingTransport(InProcessTransport())
        assert wrapped.faults == ()

    def test_inject_and_clear_failures(self) -> None:
        wrapped = FailureInjectingTransport(InProcessTransport())
        wrapped.inject_failure("delay", {"seconds": 0.1})
        wrapped.inject_failure("partition", {"blocked_peers": {"x"}})
        assert len(wrapped.faults) == 2
        wrapped.clear_failures()
        assert wrapped.faults == ()

    def test_inner_exposed(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        assert wrapped.inner is inner


# ─── Pass-through (no faults registered) ──────────────────────────


class TestPassthrough:
    def test_send_recv_passthrough(self) -> None:
        wrapped = FailureInjectingTransport(InProcessTransport(known_peers=["peer.a"]))
        wrapped.send("peer.a", {"msg": 1})
        assert wrapped.recv("peer.a") == {"msg": 1}

    def test_request_passthrough(self) -> None:
        inner = InProcessTransport(request_handlers={"p": lambda msg: {"echo": msg}})
        wrapped = FailureInjectingTransport(inner)
        assert wrapped.request("p", {"op": "x"}) == {"echo": {"op": "x"}}

    def test_peers_delegated(self) -> None:
        inner = InProcessTransport(known_peers=["a", "b"])
        wrapped = FailureInjectingTransport(inner)
        assert set(wrapped.peers()) == {"a", "b"}


# ─── DROP fault ───────────────────────────────────────────────────


class TestDrop:
    def test_send_dropped_silently(self) -> None:
        inner = InProcessTransport(known_peers=["peer.a"])
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("drop")
        wrapped.send("peer.a", {"msg": "vanished"})
        assert inner.recv("peer.a") is None

    def test_request_drop_raises_network_error(self) -> None:
        inner = InProcessTransport(request_handlers={"p": lambda _msg: {"ok": True}})
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("drop")
        with pytest.raises(TransportNetworkError) as excinfo:
            wrapped.request("p", {})
        assert excinfo.value.peer_id == "p"

    def test_recv_drop_returns_none(self) -> None:
        inner = InProcessTransport()
        inner.send("p", {"msg": 1})
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("drop")
        assert wrapped.recv("p") is None
        # The underlying message is still queued — drop affects the
        # wrapper's view, not the inner transport's state.
        assert inner.recv("p") == {"msg": 1}

    def test_broadcast_dropped(self) -> None:
        inner = InProcessTransport(known_peers=["a", "b"])
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("drop")
        wrapped.broadcast({"hello": "world"})
        assert inner.recv("a") is None
        assert inner.recv("b") is None


# ─── DELAY fault ──────────────────────────────────────────────────


class TestDelay:
    def test_send_delayed_by_configured_seconds(self) -> None:
        inner = InProcessTransport(known_peers=["p"])
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("delay", {"seconds": 0.05})
        t0 = time.monotonic()
        wrapped.send("p", {"msg": 1})
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04
        assert inner.recv("p") == {"msg": 1}

    def test_zero_delay_is_noop(self) -> None:
        inner = InProcessTransport(known_peers=["p"])
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("delay", {"seconds": 0})
        t0 = time.monotonic()
        wrapped.send("p", {})
        assert time.monotonic() - t0 < 0.01


# ─── CORRUPT fault ────────────────────────────────────────────────


class TestCorrupt:
    def test_default_corrupt_prepends_marker_to_strings(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("corrupt")
        wrapped.send("p", {"name": "alice", "count": 5})
        received = inner.recv("p")
        assert received["name"].startswith("__corrupt__")
        assert received["count"] == 5  # non-string untouched

    def test_custom_transform(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure(
            "corrupt",
            {"transform": lambda msg: {**msg, "_extra": "injected"}},
        )
        wrapped.send("p", {"k": 1})
        received = inner.recv("p")
        assert received == {"k": 1, "_extra": "injected"}

    def test_corrupt_applies_to_request_payload_too(self) -> None:
        seen: List[Mapping[str, Any]] = []

        def handler(msg: Mapping[str, Any]) -> Mapping[str, Any]:
            seen.append(dict(msg))
            return {"ack": True}

        inner = InProcessTransport(request_handlers={"p": handler})
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("corrupt")
        wrapped.request("p", {"name": "alice"})
        assert seen == [{"name": "__corrupt__alice"}]


# ─── PARTITION fault ──────────────────────────────────────────────


class TestPartition:
    def test_send_to_blocked_peer_raises(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("partition", {"blocked_peers": {"blocked-1"}})
        with pytest.raises(TransportNetworkError) as excinfo:
            wrapped.send("blocked-1", {"msg": 1})
        assert excinfo.value.peer_id == "blocked-1"

    def test_send_to_allowed_peer_succeeds(self) -> None:
        inner = InProcessTransport(known_peers=["good"])
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("partition", {"blocked_peers": {"blocked-1"}})
        wrapped.send("good", {"msg": 1})
        assert inner.recv("good") == {"msg": 1}

    def test_all_peers_blocked_via_wildcard(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("partition", {"blocked_peers": {"<all>"}})
        with pytest.raises(TransportNetworkError):
            wrapped.send("any-peer", {"msg": 1})

    def test_request_partition_raises(self) -> None:
        inner = InProcessTransport(request_handlers={"p": lambda _msg: {"ok": True}})
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("partition", {"blocked_peers": {"p"}})
        with pytest.raises(TransportNetworkError):
            wrapped.request("p", {})

    def test_recv_partition_raises(self) -> None:
        inner = InProcessTransport()
        inner.send("p", {"msg": 1})
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("partition", {"blocked_peers": {"p"}})
        with pytest.raises(TransportNetworkError):
            wrapped.recv("p")


# ─── DUPLICATE fault ──────────────────────────────────────────────


class TestDuplicate:
    def test_send_delivered_twice(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("duplicate")
        wrapped.send("p", {"msg": "once"})
        received = []
        while True:
            m = inner.recv("p")
            if m is None:
                break
            received.append(m)
        assert received == [{"msg": "once"}, {"msg": "once"}]

    def test_request_invokes_inner_twice_returns_second(self) -> None:
        invocations = {"n": 0}

        def handler(_msg: Mapping[str, Any]) -> Mapping[str, Any]:
            invocations["n"] += 1
            return {"counter": invocations["n"]}

        inner = InProcessTransport(request_handlers={"p": handler})
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("duplicate")
        reply = wrapped.request("p", {})
        # Inner was hit twice; the reply returned is the second one.
        assert invocations["n"] == 2
        assert reply == {"counter": 2}


# ─── Fault stacking ───────────────────────────────────────────────


class TestFaultStacking:
    def test_delay_then_corrupt(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("delay", {"seconds": 0.03})
        wrapped.inject_failure("corrupt")
        t0 = time.monotonic()
        wrapped.send("p", {"x": "hello"})
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.02
        received = inner.recv("p")
        assert received["x"].startswith("__corrupt__")

    def test_partition_short_circuits_remaining_faults(self) -> None:
        # When partition raises mid-pipeline, later faults don't get applied
        # — that's correct: the call already failed.
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("partition", {"blocked_peers": {"p"}})
        wrapped.inject_failure("corrupt")  # never reached
        with pytest.raises(TransportNetworkError):
            wrapped.send("p", {"x": "hello"})

    def test_drop_short_circuits_send(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.inject_failure("drop")
        wrapped.inject_failure("corrupt")  # never reached, drop wins
        wrapped.send("p", {"x": "hello"})
        assert inner.recv("p") is None


# ─── Pass-through hooks ───────────────────────────────────────────


class TestHandlerPassthrough:
    def test_register_request_handler_delegates(self) -> None:
        inner = InProcessTransport()
        wrapped = FailureInjectingTransport(inner)
        wrapped.register_request_handler("p", lambda _msg: {"ok": True})
        assert wrapped.request("p", {}) == {"ok": True}

    def test_register_on_unsupported_inner_raises(self) -> None:
        class MinimalTransport:
            def send(self, peer_id, message):  # noqa: ARG002
                return None

            def recv(self, peer_id, *, timeout_s=None):  # noqa: ARG002
                return None

            def broadcast(self, message):  # noqa: ARG002
                return None

            def peers(self):
                return ()

            def request(self, peer_id, message, *, timeout_s=None, idempotency_key=None):  # noqa: ARG002
                return {}

        wrapped = FailureInjectingTransport(MinimalTransport())
        with pytest.raises(TransportError):
            wrapped.register_request_handler("p", lambda _msg: {})
