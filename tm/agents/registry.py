from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Sequence

from tm.agents.models import AgentSpec
from tm.agents.remote_runtime import (
    AuthTokenResolver,
    TransportBuildContext,
    resolve_remote_agent,
)
from tm.agents.runtime import RuntimeAgent

RuntimeAgentFactory = Callable[[AgentSpec, Mapping[str, Any]], RuntimeAgent]


class AgentRegistryError(RuntimeError):
    """Raised when an agent cannot be found or registered."""


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: Dict[str, RuntimeAgentFactory] = {}

    def register(self, agent_id: str, factory: RuntimeAgentFactory) -> None:
        if agent_id in self._agents:
            raise AgentRegistryError(f"agent '{agent_id}' already registered")
        self._agents[agent_id] = factory

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def resolve(
        self,
        agent_id: str,
        spec: AgentSpec,
        config: Mapping[str, Any],
        *,
        transport_context: TransportBuildContext | None = None,
        auth_resolver: AuthTokenResolver | None = None,
    ) -> RuntimeAgent:
        if spec.runtime.is_remote():
            return resolve_remote_agent(
                spec,
                config,
                transport_context=transport_context,
                auth_resolver=auth_resolver,
            )
        factory = self._agents.get(agent_id)
        if factory is None:
            raise AgentRegistryError(f"agent '{agent_id}' is not registered")
        return factory(spec, config)

    def list_agent_ids(self) -> Sequence[str]:
        return list(self._agents.keys())


_DEFAULT_REGISTRY = AgentRegistry()


def default_registry() -> AgentRegistry:
    return _DEFAULT_REGISTRY


def register_agent(agent_id: str, factory: RuntimeAgentFactory) -> None:
    default_registry().register(agent_id, factory)


def unregister_agent(agent_id: str) -> None:
    default_registry().unregister(agent_id)


def resolve_agent(
    agent_id: str,
    spec: AgentSpec,
    config: Mapping[str, Any],
    *,
    transport_context: TransportBuildContext | None = None,
    auth_resolver: AuthTokenResolver | None = None,
) -> RuntimeAgent:
    return default_registry().resolve(
        agent_id,
        spec,
        config,
        transport_context=transport_context,
        auth_resolver=auth_resolver,
    )
