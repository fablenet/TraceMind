from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, MutableMapping

from tm.agents.models import AgentContract, AgentSpec

RuntimeContext = Mapping[str, Any]
RuntimeInputs = Mapping[str, Any]
RuntimeOutputs = Mapping[str, Any]


@dataclass
class RuntimeState:
    """Stores runtime-visible context such as config and metadata."""

    config: Mapping[str, Any]
    metadata: MutableMapping[str, Any] = field(default_factory=dict)


class RuntimeAgent(ABC):
    AGENT_ID: ClassVar[str]
    """Base class for TraceMind runtime agents."""

    def __init__(self, spec: AgentSpec, config: Mapping[str, Any]) -> None:
        self._spec = spec
        self._config = dict(config)
        self._state: RuntimeState = RuntimeState(config=self._config)

    @property
    def spec(self) -> AgentSpec:
        """Returns the AgentSpec that describes this agent."""
        return self._spec

    @property
    def contract(self) -> AgentContract:
        """Convenience accessor for the agent contract."""
        return self._spec.contract

    @property
    def config(self) -> Mapping[str, Any]:
        """Returns the resolved configuration map."""
        return dict(self._config)

    @property
    def state(self) -> RuntimeState:
        """Mutable runtime state surfaced to agents."""
        return self._state

    def init(self, ctx: RuntimeContext) -> None:
        """Hook that runs before `run` to prepare the runtime."""
        self._state.metadata.update(ctx)

    @abstractmethod
    def run(self, inputs: RuntimeInputs) -> RuntimeOutputs:
        """Executes the core behavior of the agent."""
        ...

    def finalize(self) -> None:
        """Optional cleanup executed after `run`."""
        self._state.metadata.clear()

    def add_evidence(self, kind: str, payload: Mapping[str, Any]) -> None:
        entries = self._state.metadata.setdefault("agent_evidence", [])
        entries.append({"kind": kind, "payload": dict(payload)})
