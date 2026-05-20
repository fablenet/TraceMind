"""Tests for the lifted Transport Protocol + InProcessTransport + error hierarchy.

Covers Stage 6-2.1a/b: ``tm.transport.base`` is the canonical home; the old
``tm.control.agents.transport`` path stays as a re-export. The Phase 5
in-memory mailbox semantics are preserved unchanged; the new RPC method
(``request``) is tested here alongside.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from tm.transport import (
    InProcessTransport,
    Transport,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)


class TestImportCompat:
    def test_re_export_yields_same_classes(self) -> None:
        from tm.control.agents.transport import (
            InProcessTransport as _Legacy_InProcess,
            Transport as _Legacy_Transport,
            TransportError as _Legacy_Error,
            TransportNetworkError as _Legacy_NetErr,
            TransportTimeout as _Legacy_Timeout,
        )

        assert _Legacy_InProcess is InProcessTransport
        assert _Legacy_Transport is Transport
        assert _Legacy_Error is TransportError
        assert _Legacy_NetErr is TransportNetworkError
        assert _Legacy_Timeout is TransportTimeout

    def test_top_level_agents_re_export(self) -> None:
        from tm.control.agents import InProcessTransport as _A, Transport as _T

        assert _A is InProcessTransport
        assert _T is Transport


class TestErrorHierarchy:
    def test_timeout_is_transport_error(self) -> None:
        assert issubclass(TransportTimeout, TransportError)

    def test_network_error_is_transport_error(self) -> None:
        assert issubclass(TransportNetworkError, TransportError)

    def test_network_error_carries_peer_id(self) -> None:
        err = TransportNetworkError("unreachable", peer_id="leaf.a")
        assert err.peer_id == "leaf.a"
        assert str(err) == "unreachable"

    def test_transport_error_is_runtime_error(self) -> None:
        assert issubclass(TransportError, RuntimeError)


class TestInProcessFifo:
    def test_send_and_recv_roundtrip(self) -> None:
        t = InProcessTransport()
        t.send("peer.a", {"msg": 1})
        assert t.recv("peer.a") == {"msg": 1}

    def test_recv_empty_returns_none(self) -> None:
        assert InProcessTransport().recv("missing") is None

    def test_fifo_ordering(self) -> None:
        t = InProcessTransport()
        for i in range(5):
            t.send("peer.a", {"i": i})
        received = [t.recv("peer.a")["i"] for _ in range(5)]
        assert received == [0, 1, 2, 3, 4]

    def test_send_top_level_is_isolated(self) -> None:
        # Phase 5 contract: ``send`` makes a shallow copy of the envelope.
        # Reassigning a top-level key on the original must not affect the
        # queued message. Deeper structural sharing is documented and the
        # JSON-serializing transports (HTTP / FileQueue) provide true deep
        # isolation by virtue of their wire format.
        t = InProcessTransport()
        payload = {"k": 1}
        t.send("peer.a", payload)
        payload["k"] = 999
        assert t.recv("peer.a") == {"k": 1}

    def test_known_peers_pre_populates_mailboxes(self) -> None:
        t = InProcessTransport(known_peers=["a", "b", "c"])
        assert set(t.peers()) == {"a", "b", "c"}
        assert t.pending_count("a") == 0

    def test_broadcast_reaches_all_known_peers(self) -> None:
        t = InProcessTransport(known_peers=["a", "b"])
        t.broadcast({"hello": "world"})
        assert t.recv("a") == {"hello": "world"}
        assert t.recv("b") == {"hello": "world"}

    def test_pending_count(self) -> None:
        t = InProcessTransport()
        t.send("peer.a", {"x": 1})
        t.send("peer.a", {"x": 2})
        assert t.pending_count("peer.a") == 2
        assert t.pending_count("missing") == 0

    def test_runtime_isinstance_transport(self) -> None:
        assert isinstance(InProcessTransport(), Transport)


class TestInProcessRequestHandlers:
    def test_request_without_handler_raises_network_error(self) -> None:
        t = InProcessTransport()
        with pytest.raises(TransportNetworkError) as excinfo:
            t.request("ghost", {"op": "ping"})
        assert excinfo.value.peer_id == "ghost"

    def test_request_with_handler_returns_response(self) -> None:
        def handler(msg: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"echo": msg["op"]}

        t = InProcessTransport(request_handlers={"echo.peer": handler})
        assert t.request("echo.peer", {"op": "ping"}) == {"echo": "ping"}

    def test_register_and_unregister_request_handler(self) -> None:
        t = InProcessTransport()
        t.register_request_handler("echo.peer", lambda msg: {"got": msg})
        assert t.request("echo.peer", {"x": 1}) == {"got": {"x": 1}}

        t.unregister_request_handler("echo.peer")
        with pytest.raises(TransportNetworkError):
            t.request("echo.peer", {"x": 1})

    def test_request_passes_shallow_copy_to_handler(self) -> None:
        # Parity with ``send`` — shallow copy semantics. Top-level key
        # reassignment on the original is invisible to the handler.
        received: list[dict[str, Any]] = []

        def handler(msg: Mapping[str, Any]) -> Mapping[str, Any]:
            received.append(dict(msg))
            return {"ok": True}

        t = InProcessTransport(request_handlers={"p": handler})
        payload = {"k": 1}
        t.request("p", payload)
        payload["k"] = 999
        assert received == [{"k": 1}]

    def test_request_idempotency_key_passthrough_silently_ignored(self) -> None:
        # InProcess transport is already-ordered + synchronous; the
        # idempotency_key is accepted for API parity but has no effect.
        t = InProcessTransport(request_handlers={"p": lambda msg: {"ok": True}})
        r1 = t.request("p", {"op": "x"}, idempotency_key="key-1")
        r2 = t.request("p", {"op": "x"}, idempotency_key="key-1")
        assert r1 == r2 == {"ok": True}

    def test_request_timeout_arg_accepted_no_effect(self) -> None:
        t = InProcessTransport(request_handlers={"p": lambda _msg: {"ok": True}})
        assert t.request("p", {}, timeout_s=0.001) == {"ok": True}


class TestProtocolConformance:
    """``runtime_checkable`` Protocol only checks attribute presence; verify
    InProcessTransport (and any custom Transport) conforms structurally."""

    def test_inprocess_implements_all_methods(self) -> None:
        t = InProcessTransport()
        for method in ("send", "recv", "broadcast", "peers", "request"):
            assert callable(getattr(t, method))

    def test_custom_implementation_passes_isinstance(self) -> None:
        class CustomTransport:
            def send(self, peer_id, message):  # noqa: D401, ARG002
                return None

            def recv(self, peer_id, *, timeout_s=None):  # noqa: ARG002
                return None

            def broadcast(self, message):  # noqa: ARG002
                return None

            def peers(self):
                return ()

            def request(self, peer_id, message, *, timeout_s=None, idempotency_key=None):  # noqa: ARG002
                return {}

        assert isinstance(CustomTransport(), Transport)
