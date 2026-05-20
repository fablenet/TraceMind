"""Tests for :class:`tm.transport.http.HttpTransport` — Phase 6 Stage 6-2.1c.

All tests use ``httpx.MockTransport`` for the client and an in-process
sibling ``HttpTransport`` for the server side, so no real network sockets
are opened. The mock router translates inbox / RPC requests into calls on
the server-side ``HttpTransport.push_inbox`` / ``handle_rpc`` hooks,
which is what a FastAPI route would do in Stage 6-3.

Covers:
- send / recv FIFO over HTTP
- broadcast across multiple peers
- request/response with idempotency dedup
- retry on transient HTTP 5xx + network error + timeout
- no-retry on HTTP 4xx (deterministic client error)
- malformed envelope handling
- unknown peer / endpoint resolution failure
- auth header propagation
- context-manager lifecycle
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

import httpx
import pytest

from tm.transport import (
    Transport,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)
from tm.transport.http import (
    HttpTransport,
    RetryPolicy,
    make_inbox_handler,
    make_rpc_handler,
)

# ─── Test rig ──────────────────────────────────────────────────────


def _route(server: HttpTransport, fail_count: Dict[str, int] | None = None):
    """Build an ``httpx.MockTransport`` handler that routes /inbox + /rpc
    calls into ``server``'s server-side hooks.

    ``fail_count`` (optional) is a mutable counter keyed by URL path that,
    when positive, returns HTTP 503 instead of dispatching and decrements
    the counter — used to exercise retry semantics deterministically.
    """
    inbox_handler = make_inbox_handler(server)
    rpc_handler = make_rpc_handler(server)
    counter = fail_count if fail_count is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        remaining = counter.get(path, 0)
        if remaining > 0:
            counter[path] = remaining - 1
            return httpx.Response(503, json={"error": "transient"})
        try:
            envelope = request.read()
            payload = json.loads(envelope) if envelope else {}
        except Exception:
            return httpx.Response(400, json={"error": "bad json"})
        try:
            if path.endswith("/_transport/inbox"):
                inbox_handler(payload)
                return httpx.Response(202, json={"ok": True})
            if path.endswith("/_transport/rpc"):
                reply = rpc_handler(payload)
                return httpx.Response(200, json=dict(reply))
        except TransportNetworkError as exc:
            return httpx.Response(503, json={"error": str(exc)})
        except TransportError as exc:
            return httpx.Response(400, json={"error": str(exc)})
        return httpx.Response(404, json={"error": "no route"})

    return handler


def _make_pair(*, with_failures: Dict[str, int] | None = None) -> tuple[HttpTransport, HttpTransport]:
    """Build (client, server) pair wired via httpx.MockTransport.

    Both sides know each other under stable peer_ids and base URLs so the
    client can target the server by ``peer_id``.
    """
    server = HttpTransport(my_peer_id="server", peer_endpoints={})
    mock = httpx.MockTransport(_route(server, with_failures))
    client_httpx = httpx.Client(transport=mock, timeout=1.0)
    client = HttpTransport(
        my_peer_id="client",
        peer_endpoints={"server": "http://server.test"},
        client=client_httpx,
        retry_policy=RetryPolicy(max_attempts=3, base_backoff_s=0.0, max_backoff_s=0.0),
    )
    return client, server


# ─── Construction & protocol conformance ──────────────────────────


class TestConstruction:
    def test_empty_peer_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            HttpTransport(my_peer_id="", peer_endpoints={})

    def test_satisfies_transport_protocol(self) -> None:
        t = HttpTransport(my_peer_id="x", peer_endpoints={})
        assert isinstance(t, Transport)

    def test_context_manager_closes_owned_client(self) -> None:
        with HttpTransport(my_peer_id="x", peer_endpoints={}) as t:
            assert t._client is not None  # owned client
        # No error means close() succeeded.

    def test_external_client_not_closed(self) -> None:
        ext = httpx.Client()
        try:
            t = HttpTransport(my_peer_id="x", peer_endpoints={}, client=ext)
            t.close()
            # External client must still be usable.
            assert not ext.is_closed
        finally:
            ext.close()


# ─── Send / recv ──────────────────────────────────────────────────


class TestSendRecv:
    def test_send_then_recv_fifo(self) -> None:
        client, server = _make_pair()
        try:
            client.send("server", {"msg": 1})
            client.send("server", {"msg": 2})
            client.send("server", {"msg": 3})

            r1 = server.recv("client")
            r2 = server.recv("client")
            r3 = server.recv("client")
            assert r1["body"] == {"msg": 1}
            assert r2["body"] == {"msg": 2}
            assert r3["body"] == {"msg": 3}
        finally:
            client.close()
            server.close()

    def test_recv_empty_returns_none(self) -> None:
        _, server = _make_pair()
        try:
            assert server.recv("nobody") is None
        finally:
            server.close()

    def test_send_to_unknown_peer_raises(self) -> None:
        client = HttpTransport(my_peer_id="me", peer_endpoints={})
        try:
            with pytest.raises(TransportNetworkError) as excinfo:
                client.send("ghost", {})
            assert excinfo.value.peer_id == "ghost"
        finally:
            client.close()

    def test_envelope_carries_from_to_kind(self) -> None:
        client, server = _make_pair()
        try:
            client.send("server", {"hello": "world"})
            received = server.recv("client")
            assert received["from"] == "client"
            assert received["to"] == "server"
            assert received["kind"] == "send"
            assert received["body"] == {"hello": "world"}
        finally:
            client.close()
            server.close()


# ─── Broadcast ────────────────────────────────────────────────────


class TestBroadcast:
    def test_broadcast_skips_self(self) -> None:
        # When a peer addresses itself, it should not loop back.
        server = HttpTransport(my_peer_id="server", peer_endpoints={})
        mock = httpx.MockTransport(_route(server))
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="client",
            peer_endpoints={"client": "http://self.test", "server": "http://server.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=1, base_backoff_s=0.0, max_backoff_s=0.0),
        )
        try:
            client.broadcast({"announce": True})
            # Only server received; self was skipped (mock would have crashed
            # otherwise because there is no route for self.test).
            received = server.recv("client")
            assert received["body"] == {"announce": True}
        finally:
            client.close()
            server.close()

    def test_broadcast_bubbles_last_error(self) -> None:
        # Two peers; the second one's endpoint is unreachable. Broadcast
        # should attempt both and surface the second's failure.
        good_server = HttpTransport(my_peer_id="good", peer_endpoints={})

        def handler(request: httpx.Request) -> httpx.Response:
            if "good.test" in str(request.url):
                inbox = make_inbox_handler(good_server)
                inbox(json.loads(request.read()))
                return httpx.Response(202)
            return httpx.Response(500, json={"error": "boom"})

        mock = httpx.MockTransport(handler)
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="me",
            peer_endpoints={"good": "http://good.test", "bad": "http://bad.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=1, base_backoff_s=0.0, max_backoff_s=0.0),
        )
        try:
            with pytest.raises(TransportNetworkError):
                client.broadcast({"ping": True})
            # Good peer still got the message.
            assert good_server.recv("me") is not None
        finally:
            client.close()


# ─── Request / response (RPC) ─────────────────────────────────────


class TestRequest:
    def test_request_returns_handler_reply(self) -> None:
        client, server = _make_pair()
        server.register_request_handler("server", lambda msg: {"echo": msg["op"]})
        try:
            reply = client.request("server", {"op": "ping"})
            assert reply == {"echo": "ping"}
        finally:
            client.close()
            server.close()

    def test_request_without_server_handler_surfaces_503(self) -> None:
        client, server = _make_pair()
        try:
            with pytest.raises(TransportNetworkError):
                client.request("server", {"op": "ping"})
        finally:
            client.close()
            server.close()

    def test_request_idempotency_replays_cached_reply(self) -> None:
        client, server = _make_pair()
        invocations: list[Mapping[str, Any]] = []

        def handler(msg: Mapping[str, Any]) -> Mapping[str, Any]:
            invocations.append(dict(msg))
            return {"counter": len(invocations)}

        server.register_request_handler("server", handler)
        try:
            first = client.request("server", {"op": "x"}, idempotency_key="key-1")
            second = client.request("server", {"op": "x"}, idempotency_key="key-1")
            assert first == second == {"counter": 1}
            assert len(invocations) == 1

            # A different key invokes the handler again.
            third = client.request("server", {"op": "x"}, idempotency_key="key-2")
            assert third == {"counter": 2}
            assert len(invocations) == 2
        finally:
            client.close()
            server.close()

    def test_request_unknown_peer_raises_before_io(self) -> None:
        client = HttpTransport(my_peer_id="me", peer_endpoints={})
        try:
            with pytest.raises(TransportNetworkError) as excinfo:
                client.request("ghost", {})
            assert excinfo.value.peer_id == "ghost"
        finally:
            client.close()


# ─── Send-side idempotency dedup on receive ───────────────────────


class TestServerSideDedup:
    def test_duplicate_send_dropped_silently(self) -> None:
        client, server = _make_pair()
        try:
            envelope_msg = {"op": "x"}
            client.send("server", envelope_msg)
            # Simulate duplicate by re-pushing the same envelope into the server.
            server.push_inbox(
                {
                    "from": "client",
                    "to": "server",
                    "kind": "send",
                    "body": envelope_msg,
                    "idempotency_key": "dup-1",
                }
            )
            server.push_inbox(
                {
                    "from": "client",
                    "to": "server",
                    "kind": "send",
                    "body": envelope_msg,
                    "idempotency_key": "dup-1",
                }
            )
            # First send had no idempotency_key → delivered. Then the keyed
            # one delivered once. The duplicate keyed one was dropped.
            received = []
            for _ in range(3):
                r = server.recv("client")
                if r is None:
                    break
                received.append(r)
            assert len(received) == 2

        finally:
            client.close()
            server.close()

    def test_push_inbox_missing_from_raises(self) -> None:
        _, server = _make_pair()
        try:
            with pytest.raises(TransportError):
                server.push_inbox({"to": "server", "body": {}})
        finally:
            server.close()


# ─── Retry semantics ──────────────────────────────────────────────


class TestRetry:
    def test_transient_5xx_retried_until_success(self) -> None:
        # Two transient failures, then success.
        fails = {"/_transport/inbox": 2}
        client, server = _make_pair(with_failures=fails)
        try:
            client.send("server", {"msg": 1})
            received = server.recv("client")
            assert received["body"] == {"msg": 1}
            # Counter reached zero after 2 failures.
            assert fails["/_transport/inbox"] == 0
        finally:
            client.close()
            server.close()

    def test_persistent_5xx_eventually_fails(self) -> None:
        fails = {"/_transport/inbox": 99}  # never recovers within max_attempts=3
        client, server = _make_pair(with_failures=fails)
        try:
            with pytest.raises(TransportNetworkError):
                client.send("server", {"msg": 1})
            # All 3 attempts consumed.
            assert fails["/_transport/inbox"] == 99 - 3
        finally:
            client.close()
            server.close()

    def test_4xx_not_retried(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(400, json={"error": "malformed"})

        mock = httpx.MockTransport(handler)
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="me",
            peer_endpoints={"p": "http://p.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=5, base_backoff_s=0.0, max_backoff_s=0.0),
        )
        try:
            with pytest.raises(TransportError):
                client.send("p", {})
            assert call_count["n"] == 1  # exactly one attempt, no retry
        finally:
            client.close()

    def test_network_error_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("simulated connect failure")
            return httpx.Response(202)

        mock = httpx.MockTransport(handler)
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="me",
            peer_endpoints={"p": "http://p.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=3, base_backoff_s=0.0, max_backoff_s=0.0),
        )
        try:
            client.send("p", {})
            assert attempts["n"] == 3
        finally:
            client.close()

    def test_timeout_eventually_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated timeout")

        mock = httpx.MockTransport(handler)
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="me",
            peer_endpoints={"p": "http://p.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=2, base_backoff_s=0.0, max_backoff_s=0.0),
        )
        try:
            with pytest.raises(TransportTimeout):
                client.send("p", {})
        finally:
            client.close()


# ─── Retry policy math ────────────────────────────────────────────


class TestRetryPolicy:
    def test_backoff_doubles_then_caps(self) -> None:
        policy = RetryPolicy(max_attempts=10, base_backoff_s=0.1, max_backoff_s=0.5)
        assert policy.backoff_for(1) == pytest.approx(0.1)
        assert policy.backoff_for(2) == pytest.approx(0.2)
        assert policy.backoff_for(3) == pytest.approx(0.4)
        assert policy.backoff_for(4) == pytest.approx(0.5)  # capped
        assert policy.backoff_for(20) == pytest.approx(0.5)  # capped

    def test_backoff_zero_for_nonpositive_attempt(self) -> None:
        assert RetryPolicy().backoff_for(0) == 0.0
        assert RetryPolicy().backoff_for(-1) == 0.0


# ─── Auth header propagation ──────────────────────────────────────


class TestAuthHeader:
    def test_auth_header_attached_when_set(self) -> None:
        seen_auth: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization"))
            return httpx.Response(202)

        mock = httpx.MockTransport(handler)
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="me",
            peer_endpoints={"p": "http://p.test"},
            client=client_httpx,
            auth_header="Bearer secret-token",
            retry_policy=RetryPolicy(max_attempts=1),
        )
        try:
            client.send("p", {})
        finally:
            client.close()
        assert seen_auth == ["Bearer secret-token"]

    def test_no_auth_header_when_unset(self) -> None:
        seen_auth: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization"))
            return httpx.Response(202)

        mock = httpx.MockTransport(handler)
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="me",
            peer_endpoints={"p": "http://p.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        try:
            client.send("p", {})
        finally:
            client.close()
        assert seen_auth == [None]


# ─── Server-side handler registry ─────────────────────────────────


class TestServerHandlerRegistry:
    def test_register_and_unregister_handler(self) -> None:
        client, server = _make_pair()
        server.register_request_handler("server", lambda msg: {"v": 1})
        try:
            assert client.request("server", {}) == {"v": 1}
            server.unregister_request_handler("server")
            with pytest.raises(TransportNetworkError):
                client.request("server", {})
        finally:
            client.close()
            server.close()

    def test_handler_routed_by_to_field(self) -> None:
        # Two logical agents multiplexed on the same HTTP transport: routing
        # is by the ``to`` field of the envelope, not by the transport's
        # my_peer_id.
        server = HttpTransport(my_peer_id="node", peer_endpoints={})
        server.register_request_handler("agent.a", lambda _msg: {"who": "a"})
        server.register_request_handler("agent.b", lambda _msg: {"who": "b"})
        mock = httpx.MockTransport(_route(server))
        client_httpx = httpx.Client(transport=mock, timeout=1.0)
        client = HttpTransport(
            my_peer_id="caller",
            peer_endpoints={"agent.a": "http://node.test", "agent.b": "http://node.test"},
            client=client_httpx,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        try:
            assert client.request("agent.a", {}) == {"who": "a"}
            assert client.request("agent.b", {}) == {"who": "b"}
        finally:
            client.close()
            server.close()
