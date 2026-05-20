"""Tests for remote ``AgentRuntime`` resolver — Phase 6 Stage 6-2.3.

Covers :mod:`tm.agents.remote_runtime` and the registry integration that
routes ``runtime.kind == 'remote'`` through a transport-backed proxy.
Network failures MUST surface as :class:`RemoteAgentEscalation` (never
silently swallowed) with an escalation payload suitable for
:class:`EscalationReportBody` materialization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

import pytest

from tm.agents.models import AgentContract, AgentEvidenceOutput, AgentRuntime, AgentSpec
from tm.agents.registry import AgentRegistry, resolve_agent
from tm.agents.remote_runtime import (
    RemoteAgentEscalation,
    RemoteRuntimeAgent,
    TransportBuildContext,
    build_transport_for_runtime,
    make_escalation_payload,
    resolve_remote_agent,
)
from tm.transport import (
    FileQueueTransport,
    HttpTransport,
    InProcessTransport,
    TransportNetworkError,
)


def _remote_spec(**runtime_overrides: Any) -> AgentSpec:
    runtime_payload: Dict[str, Any] = {
        "kind": "remote",
        "config": {},
        "endpoint": "https://leaf-1.example.test:8080",
        "transport_kind": "inprocess",
    }
    runtime_payload.update(runtime_overrides)
    runtime = AgentRuntime.from_mapping(runtime_payload)
    return AgentSpec(
        agent_id="leaf.observer",
        name="leaf-observer",
        version="0.0.1",
        runtime=runtime,
        contract=AgentContract(inputs=[], outputs=[], effects=[]),
        config_schema={},
        evidence_outputs=[AgentEvidenceOutput(name="kpi")],
    )


def _echo_handler(message: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = message.get("inputs") or {}
    return {"outputs": {"echo": dict(inputs)}}


# ─── Happy path (inprocess transport stand-in) ────────────────────


class TestRemoteProxySuccess:
    def test_resolve_remote_agent_invokes_handler(self) -> None:
        spec = _remote_spec()
        ctx = TransportBuildContext(my_peer_id="center")
        agent = resolve_remote_agent(spec, {"mode": "test"}, transport_context=ctx)
        transport = agent.transport
        assert isinstance(transport, InProcessTransport)
        transport.register_request_handler("leaf.observer", _echo_handler)

        output = agent.run({"payload": "value"})
        assert output == {"echo": {"payload": "value"}}

    def test_registry_resolve_routes_remote_without_factory(self) -> None:
        spec = _remote_spec()
        ctx = TransportBuildContext(my_peer_id="center")
        agent = resolve_agent(
            spec.agent_id,
            spec,
            {},
            transport_context=ctx,
        )
        assert isinstance(agent, RemoteRuntimeAgent)
        agent.transport.register_request_handler("leaf.observer", _echo_handler)
        assert agent.run({"x": 1}) == {"echo": {"x": 1}}

    def test_registry_still_uses_factory_for_inprocess(self) -> None:
        spec = AgentSpec(
            agent_id="local.echo",
            name="echo",
            version="0.0.1",
            runtime=AgentRuntime.from_mapping({"kind": "inprocess", "config": {}}),
            contract=AgentContract(inputs=[], outputs=[], effects=[]),
            config_schema={},
            evidence_outputs=[],
        )
        registry = AgentRegistry()
        registry.register(
            spec.agent_id,
            lambda s, c: RemoteRuntimeAgent(
                s,
                c,
                transport=InProcessTransport(),
                remote_peer_id="unused",
            ),
        )
        agent = registry.resolve(spec.agent_id, spec, {})
        assert isinstance(agent, RemoteRuntimeAgent)


# ─── Escalation on transport failure ──────────────────────────────


class TestRemoteProxyEscalation:
    def test_missing_handler_raises_remote_agent_escalation(self) -> None:
        spec = _remote_spec()
        agent = resolve_remote_agent(
            spec,
            {},
            transport_context=TransportBuildContext(my_peer_id="center"),
        )
        with pytest.raises(RemoteAgentEscalation) as exc_info:
            agent.run({"payload": "value"})
        assert exc_info.value.peer_id == "leaf.observer"
        assert exc_info.value.escalation["peer_node_id"] == "leaf.observer"
        assert exc_info.value.escalation["severity"] in {"warning", "critical"}
        assert exc_info.value.escalation["recent_errors"]

    def test_error_reply_field_raises_escalation(self) -> None:
        spec = _remote_spec()
        agent = resolve_remote_agent(
            spec,
            {},
            transport_context=TransportBuildContext(my_peer_id="center"),
        )

        def failing_handler(_message: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"error": "handler exploded"}

        agent.transport.register_request_handler("leaf.observer", failing_handler)
        with pytest.raises(RemoteAgentEscalation, match="returned error"):
            agent.run({})

    def test_non_mapping_reply_raises_escalation(self) -> None:
        spec = _remote_spec()
        agent = resolve_remote_agent(
            spec,
            {},
            transport_context=TransportBuildContext(my_peer_id="center"),
        )

        class _NonMappingTransport(InProcessTransport):
            def request(self, peer_id, message, *, timeout_s=None, idempotency_key=None):
                return "not-a-mapping"  # type: ignore[return-value]

        agent._transport = _NonMappingTransport()
        with pytest.raises(RemoteAgentEscalation, match="non-mapping"):
            agent.run({})

    def test_make_escalation_payload_includes_peer_node_id(self) -> None:
        spec = _remote_spec()
        exc = TransportNetworkError("unreachable", peer_id="leaf.observer")
        payload = make_escalation_payload(spec, exc, intent_ref="intent.demo")
        assert payload["intent_ref"] == "intent.demo"
        assert payload["peer_node_id"] == "leaf.observer"
        assert "human_review" in payload["suggested_actions"]


# ─── Transport factory ─────────────────────────────────────────────


class TestBuildTransportForRuntime:
    def test_http_transport_wired_from_runtime(self) -> None:
        runtime = AgentRuntime.from_mapping(
            {
                "kind": "remote",
                "config": {},
                "endpoint": "http://leaf-1:8080",
                "transport_kind": "http",
                "timeout_ms": 5000,
                "retry_policy": {"max_attempts": 2},
            }
        )
        ctx = TransportBuildContext(my_peer_id="center")
        transport = build_transport_for_runtime(runtime, ctx, remote_peer_id="leaf.observer")
        assert isinstance(transport, HttpTransport)
        transport.close()

    def test_file_queue_transport_requires_root(self) -> None:
        runtime = AgentRuntime.from_mapping(
            {
                "kind": "remote",
                "config": {},
                "endpoint": "leaf.observer",
                "transport_kind": "file_queue",
            }
        )
        ctx = TransportBuildContext(my_peer_id="center")
        with pytest.raises(ValueError, match="file_queue_root"):
            build_transport_for_runtime(runtime, ctx, remote_peer_id="leaf.observer")

    def test_file_queue_transport_builds_with_root(self, tmp_path: Path) -> None:
        runtime = AgentRuntime.from_mapping(
            {
                "kind": "remote",
                "config": {},
                "endpoint": "leaf.observer",
                "transport_kind": "file_queue",
            }
        )
        ctx = TransportBuildContext(my_peer_id="center", file_queue_root=tmp_path)
        transport = build_transport_for_runtime(runtime, ctx, remote_peer_id="leaf.observer")
        assert isinstance(transport, FileQueueTransport)

    def test_auth_ref_resolved_from_context_tokens(self) -> None:
        runtime = AgentRuntime.from_mapping(
            {
                "kind": "remote",
                "config": {},
                "endpoint": "http://leaf-1:8080",
                "transport_kind": "http",
                "auth_ref": "vault:leaf/token",
            }
        )
        ctx = TransportBuildContext(
            my_peer_id="center",
            auth_tokens={"vault:leaf/token": "Bearer sekret"},
        )
        transport = build_transport_for_runtime(runtime, ctx, remote_peer_id="leaf.observer")
        assert isinstance(transport, HttpTransport)
        assert transport._auth == "Bearer sekret"
        transport.close()


# ─── End-to-end file_queue proxy ─────────────────────────────────


class TestFileQueueRemoteProxy:
    def test_center_calls_leaf_over_file_queue(self, tmp_path: Path) -> None:
        import threading
        import time

        spec = _remote_spec(transport_kind="file_queue", endpoint="leaf.observer")
        ctx = TransportBuildContext(my_peer_id="center", file_queue_root=tmp_path)

        leaf_transport = FileQueueTransport(
            my_peer_id="leaf.observer",
            root_dir=tmp_path,
            known_peers=["center"],
        )
        leaf_transport.register_request_handler("leaf.observer", _echo_handler)

        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                leaf_transport.process_request_inbox(max_messages=1)
                time.sleep(0.005)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            agent = resolve_remote_agent(spec, {}, transport_context=ctx)
            output = agent.run({"metric": 42})
        finally:
            stop.set()
            thread.join(timeout=1.0)

        assert output == {"echo": {"metric": 42}}


# ─── Guard rails ─────────────────────────────────────────────────


class TestGuardRails:
    def test_resolve_remote_agent_rejects_inprocess_kind(self) -> None:
        spec = AgentSpec(
            agent_id="local",
            name="local",
            version="0.0.1",
            runtime=AgentRuntime.from_mapping({"kind": "inprocess", "config": {}}),
            contract=AgentContract(inputs=[], outputs=[], effects=[]),
            config_schema={},
            evidence_outputs=[],
        )
        with pytest.raises(ValueError, match="remote"):
            resolve_remote_agent(spec, {})

    def test_build_transport_rejects_non_remote_runtime(self) -> None:
        runtime = AgentRuntime.from_mapping({"kind": "inprocess", "config": {}})
        with pytest.raises(ValueError, match="remote"):
            build_transport_for_runtime(
                runtime,
                TransportBuildContext(my_peer_id="center"),
                remote_peer_id="x",
            )

    def test_remote_escalation_is_runtime_error(self) -> None:
        assert issubclass(RemoteAgentEscalation, RuntimeError)
