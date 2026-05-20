"""Remote ``AgentRuntime`` wiring — Phase 6 Stage 6-2.3.

Builds a concrete :class:`tm.transport.Transport` from a declarative
:class:`AgentRuntime` spec and wraps it as a locally-invokable
:class:`RuntimeAgent` proxy. Transport failures surface as
:class:`RemoteAgentEscalation` (never silently swallowed) so callers
can materialize an :class:`EscalationReportBody` without catching bare
:class:`Exception`.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from tm.agents.models import AgentRuntime, AgentSpec, RetryPolicySpec
from tm.agents.runtime import RuntimeAgent, RuntimeInputs, RuntimeOutputs
from tm.transport import (
    FileQueueTransport,
    HttpTransport,
    InProcessTransport,
    RetryPolicy,
    Transport,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)

AuthTokenResolver = Callable[[str], str | None]


@dataclass
class TransportBuildContext:
    """Caller-supplied wiring context for ``build_transport_for_runtime``.

    ``NetworkController`` (Stage 6-3) populates this from an
    :class:`AgentNetwork` artifact; unit tests supply minimal values.
    """

    my_peer_id: str
    file_queue_root: str | Path | None = None
    auth_tokens: Mapping[str, str] = field(default_factory=dict)
    peer_endpoints: Mapping[str, str] = field(default_factory=dict)


class RemoteAgentEscalation(RuntimeError):
    """Remote agent invocation failed; carries an escalation payload."""

    def __init__(
        self,
        message: str,
        *,
        escalation: Mapping[str, Any],
        peer_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.escalation: Dict[str, Any] = dict(escalation)
        self.peer_id = peer_id
        self.__cause__ = cause


def _retry_policy_from_spec(spec: RetryPolicySpec | None) -> RetryPolicy:
    if spec is None:
        return RetryPolicy()
    return RetryPolicy(
        max_attempts=spec.max_attempts,
        base_backoff_s=spec.base_backoff_s,
        max_backoff_s=spec.max_backoff_s,
    )


def _resolve_auth_header(
    runtime: AgentRuntime,
    *,
    auth_resolver: AuthTokenResolver | None = None,
    auth_tokens: Mapping[str, str] | None = None,
) -> str | None:
    if not runtime.auth_ref:
        return None
    if auth_tokens and runtime.auth_ref in auth_tokens:
        token = auth_tokens[runtime.auth_ref]
        return token if token.startswith("Bearer ") else f"Bearer {token}"
    if auth_resolver is not None:
        resolved = auth_resolver(runtime.auth_ref)
        if resolved is None:
            return None
        return resolved if resolved.startswith("Bearer ") else f"Bearer {resolved}"
    return None


def build_transport_for_runtime(
    runtime: AgentRuntime,
    ctx: TransportBuildContext,
    *,
    remote_peer_id: str,
    auth_resolver: AuthTokenResolver | None = None,
) -> Transport:
    """Instantiate the transport declared by ``runtime.transport_kind``."""
    if not runtime.is_remote():
        raise ValueError("build_transport_for_runtime requires runtime.kind == 'remote'")
    if not runtime.transport_kind:
        raise ValueError("runtime.transport_kind is required for remote agents")

    timeout_s = (runtime.timeout_ms / 1000.0) if runtime.timeout_ms else 10.0
    retry = _retry_policy_from_spec(runtime.retry_policy)
    auth_header = _resolve_auth_header(
        runtime,
        auth_resolver=auth_resolver,
        auth_tokens=ctx.auth_tokens,
    )

    if runtime.transport_kind == "http":
        endpoint = ctx.peer_endpoints.get(remote_peer_id) or runtime.endpoint
        if not endpoint:
            raise ValueError(f"no HTTP endpoint for remote peer '{remote_peer_id}'")
        return HttpTransport(
            my_peer_id=ctx.my_peer_id,
            peer_endpoints={remote_peer_id: endpoint},
            timeout_s=timeout_s,
            retry_policy=retry,
            auth_header=auth_header,
        )

    if runtime.transport_kind == "file_queue":
        if ctx.file_queue_root is None:
            raise ValueError("file_queue_root is required when transport_kind == 'file_queue'")
        return FileQueueTransport(
            my_peer_id=ctx.my_peer_id,
            root_dir=ctx.file_queue_root,
            known_peers=[remote_peer_id, ctx.my_peer_id],
        )

    if runtime.transport_kind == "inprocess":
        # Useful for resolver unit tests: remote kind + inprocess transport
        # exercises the proxy/escalation path without real I/O.
        return InProcessTransport(known_peers=[remote_peer_id, ctx.my_peer_id])

    raise ValueError(f"unsupported transport_kind '{runtime.transport_kind}'")


def make_escalation_payload(
    spec: AgentSpec,
    exc: BaseException,
    *,
    intent_ref: str | None = None,
) -> Dict[str, Any]:
    """Build a minimal escalation-report-shaped dict from a transport failure."""
    peer_id = getattr(exc, "peer_id", None) or spec.runtime.endpoint or spec.agent_id
    severity = "critical" if isinstance(exc, TransportNetworkError) else "warning"
    return {
        "report_id": f"escalation.remote.{uuid.uuid4().hex[:12]}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "severity": severity,
        "intent_ref": intent_ref or spec.agent_id,
        "peer_node_id": peer_id,
        "recent_errors": [str(exc)],
        "suggested_actions": ["human_review"],
        "gap_summary": f"remote agent '{spec.agent_id}' transport failure: {exc}",
    }


def _idempotency_key(spec: AgentSpec, inputs: Mapping[str, Any]) -> str:
    payload = {
        "agent_id": spec.agent_id,
        "inputs": dict(inputs),
        "config": dict(spec.runtime.config),
    }
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class RemoteRuntimeAgent(RuntimeAgent):
    """Locally-invokable proxy for an agent whose ``runtime.kind == 'remote'``."""

    def __init__(
        self,
        spec: AgentSpec,
        config: Mapping[str, Any],
        *,
        transport: Transport,
        remote_peer_id: str,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(spec, config)
        self._transport = transport
        self._remote_peer_id = remote_peer_id
        self._timeout_s = timeout_s

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def remote_peer_id(self) -> str:
        return self._remote_peer_id

    def run(self, inputs: RuntimeInputs) -> RuntimeOutputs:
        envelope = {
            "op": "agent.run",
            "agent_id": self.spec.agent_id,
            "inputs": dict(inputs),
            "config": dict(self.config),
        }
        idempotency_key = _idempotency_key(self.spec, inputs)
        try:
            reply = self._transport.request(
                self._remote_peer_id,
                envelope,
                timeout_s=self._timeout_s,
                idempotency_key=idempotency_key,
            )
        except (TransportNetworkError, TransportTimeout) as exc:
            peer_id = getattr(exc, "peer_id", self._remote_peer_id)
            escalation = make_escalation_payload(self.spec, exc)
            raise RemoteAgentEscalation(
                f"remote agent '{self.spec.agent_id}' failed: {exc}",
                escalation=escalation,
                peer_id=peer_id,
                cause=exc,
            ) from exc
        except TransportError as exc:
            escalation = make_escalation_payload(self.spec, exc)
            raise RemoteAgentEscalation(
                f"remote agent '{self.spec.agent_id}' failed: {exc}",
                escalation=escalation,
                peer_id=self._remote_peer_id,
                cause=exc,
            ) from exc

        if not isinstance(reply, Mapping):
            raise RemoteAgentEscalation(
                f"remote agent '{self.spec.agent_id}' returned non-mapping reply",
                escalation=make_escalation_payload(
                    self.spec,
                    TransportError(f"expected mapping reply, got {type(reply).__name__}"),
                ),
                peer_id=self._remote_peer_id,
            )

        if reply.get("error"):
            err = TransportNetworkError(str(reply.get("error")), peer_id=self._remote_peer_id)
            raise RemoteAgentEscalation(
                f"remote agent '{self.spec.agent_id}' returned error: {reply.get('error')}",
                escalation=make_escalation_payload(self.spec, err),
                peer_id=self._remote_peer_id,
            )

        outputs = reply.get("outputs")
        if isinstance(outputs, Mapping):
            return dict(outputs)
        return dict(reply)


def resolve_remote_agent(
    spec: AgentSpec,
    config: Mapping[str, Any],
    *,
    transport_context: TransportBuildContext | None = None,
    remote_peer_id: str | None = None,
    auth_resolver: AuthTokenResolver | None = None,
) -> RemoteRuntimeAgent:
    """Wire a remote ``AgentSpec`` into a local proxy ``RuntimeAgent``."""
    if not spec.runtime.is_remote():
        raise ValueError(f"resolve_remote_agent requires runtime.kind == 'remote', got '{spec.runtime.kind}'")

    ctx = transport_context or TransportBuildContext(my_peer_id="local")
    peer_id = remote_peer_id or spec.agent_id
    transport = build_transport_for_runtime(
        spec.runtime,
        ctx,
        remote_peer_id=peer_id,
        auth_resolver=auth_resolver,
    )
    timeout_s = (spec.runtime.timeout_ms / 1000.0) if spec.runtime.timeout_ms else None
    return RemoteRuntimeAgent(
        spec,
        config,
        transport=transport,
        remote_peer_id=peer_id,
        timeout_s=timeout_s,
    )


__all__ = [
    "AuthTokenResolver",
    "RemoteAgentEscalation",
    "RemoteRuntimeAgent",
    "TransportBuildContext",
    "build_transport_for_runtime",
    "make_escalation_payload",
    "resolve_remote_agent",
]
