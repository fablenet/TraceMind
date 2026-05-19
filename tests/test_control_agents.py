"""Tests for ``tm.control.agents`` — generic MAPE-K base classes + Transport.

Verifies that:
- All four base classes (Observe / Analyze / Decide / Act) produce the
  standard artifact shapes regardless of domain
- The Transport seam works: default InProcessTransport is created when none
  is provided; an explicit Transport is honored and exposed via
  ``agent.transport``
- ``Transport`` protocol is satisfied by ``InProcessTransport`` (runtime
  check)
- Phase 6 forward-compat: the seam exists and is wired through
- Domain-specific hooks raise clear errors when violated
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from tm.agents.models import AgentContract, AgentRuntime, AgentSpec
from tm.control.agents import (
    ActBaseAgent,
    ActOutcome,
    AnalyzeBaseAgent,
    DecideBaseAgent,
    InProcessTransport,
    ObserveBaseAgent,
    Transport,
)


def _spec(agent_id: str = "test/agent:0.1") -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        name=agent_id.split("/")[-1],
        version="0.1",
        runtime=AgentRuntime(kind="inprocess", config={}),
        contract=AgentContract(inputs=[], outputs=[], effects=[]),
        config_schema={},
        evidence_outputs=[],
    )


# ─── ObserveBaseAgent ────────────────────────────────────────────


class _FakeObserve(ObserveBaseAgent):
    def collect_environment(self, inputs):
        return {"metrics": {"x": 1.5, "y": 2.0}, "events": []}


class _ObserveWithConstraints(ObserveBaseAgent):
    def collect_environment(self, inputs):
        return {"metrics": {}}

    def build_constraints(self):
        return [{"type": "guard", "rule": "k8s-observe"}]


class TestObserveBase:
    def test_produces_state_env_snapshot(self) -> None:
        agent = _FakeObserve(_spec(), config={})
        out = agent.run({})
        assert "state:env.snapshot" in out
        snap = out["state:env.snapshot"]
        assert "snapshot_id" in snap
        assert "timestamp" in snap
        assert "data_hash" in snap
        assert snap["environment"]["metrics"] == {"x": 1.5, "y": 2.0}

    def test_snapshot_id_prefix_honored(self) -> None:
        agent = _FakeObserve(_spec(), config={"snapshot_id_prefix": "k8s"})
        out = agent.run({})
        assert out["state:env.snapshot"]["snapshot_id"].startswith("k8s-")

    def test_default_constraints_empty(self) -> None:
        agent = _FakeObserve(_spec(), config={})
        out = agent.run({})
        assert out["state:env.snapshot"]["constraints"] == []

    def test_constraints_from_config(self) -> None:
        agent = _FakeObserve(
            _spec(),
            config={"constraints": [{"type": "guard", "rule": "x"}]},
        )
        out = agent.run({})
        assert out["state:env.snapshot"]["constraints"] == [{"type": "guard", "rule": "x"}]

    def test_constraints_override(self) -> None:
        agent = _ObserveWithConstraints(_spec(), config={})
        out = agent.run({})
        assert out["state:env.snapshot"]["constraints"] == [
            {"type": "guard", "rule": "k8s-observe"},
        ]

    def test_data_hash_is_deterministic(self) -> None:
        agent = _FakeObserve(_spec(), config={})
        h1 = agent.run({})["state:env.snapshot"]["data_hash"]
        h2 = agent.run({})["state:env.snapshot"]["data_hash"]
        assert h1 == h2

    def test_bad_return_type_raises(self) -> None:
        class BadObserve(ObserveBaseAgent):
            def collect_environment(self, inputs):
                return "not a mapping"

        agent = BadObserve(_spec(), config={})
        with pytest.raises(TypeError, match="must return a Mapping"):
            agent.run({})


# ─── AnalyzeBaseAgent ────────────────────────────────────────────


class _FakeAnalyze(AnalyzeBaseAgent):
    def extract_observations(self, metrics):
        out = {}
        for k, v in metrics.items():
            try:
                out[k] = float(v)
            except (ValueError, TypeError):
                continue
        return out

    def evaluate_policy(self, obs, state):
        if obs.get("x", 0) > 0.5:
            return {
                "final_patch": {"throttle": {"command_type": "set_rate_limit", "qps": 10}},
                "actions": [{"applied": True, "rule_id": "high-x-rule"}],
            }
        return {"final_patch": {}, "actions": []}


class TestAnalyzeBase:
    def test_produces_state_analysis_result(self) -> None:
        agent = _FakeAnalyze(_spec(), config={})
        snapshot = {
            "snapshot_id": "snap-1",
            "environment": {"metrics": {"x": 1.0, "y": 0.5}},
        }
        out = agent.run({"state:env.snapshot": snapshot})
        assert "state:analysis.result" in out
        result = out["state:analysis.result"]
        assert result["matched_rule"] == "high-x-rule"
        assert "throttle" in result["final_patch"]
        assert result["obs"] == {"x": 1.0, "y": 0.5}

    def test_no_matched_rule_when_obs_below_threshold(self) -> None:
        agent = _FakeAnalyze(_spec(), config={})
        snapshot = {"snapshot_id": "s", "environment": {"metrics": {"x": 0.1}}}
        out = agent.run({"state:env.snapshot": snapshot})
        result = out["state:analysis.result"]
        assert result["matched_rule"] == ""
        assert result["final_patch"] == {}

    def test_missing_snapshot_raises(self) -> None:
        agent = _FakeAnalyze(_spec(), config={})
        with pytest.raises(RuntimeError, match="state:env.snapshot"):
            agent.run({})


# ─── DecideBaseAgent ─────────────────────────────────────────────


class _FakeDecide(DecideBaseAgent):
    pass  # uses base implementation entirely


class TestDecideBase:
    def _inputs(self) -> dict[str, Any]:
        return {
            "state:env.snapshot": {"snapshot_id": "snap-1"},
            "state:analysis.result": {
                "matched_rule": "rule-A",
                "final_patch": {
                    "throttle": {"command_type": "set_rate_limit", "qps": 10},
                },
                "obs": {"x": 1.0},
            },
        }

    def test_default_effect_ref_namespace(self) -> None:
        agent = _FakeDecide(_spec(), config={})
        out = agent.run(self._inputs())
        plan = out["artifact:proposed.plan"]
        assert plan["decisions"][0]["effect_ref"] == "generic:throttle"

    def test_configurable_effect_ref_namespace(self) -> None:
        agent = _FakeDecide(_spec(), config={"effect_ref_namespace": "k8s"})
        out = agent.run(self._inputs())
        plan = out["artifact:proposed.plan"]
        assert plan["decisions"][0]["effect_ref"] == "k8s:throttle"

    def test_neutral_default_intent_id(self) -> None:
        """Stage 5-2 task 2.4: default must not be FableNet-specific."""
        agent = _FakeDecide(_spec(), config={})
        out = agent.run(self._inputs())
        plan = out["artifact:proposed.plan"]
        assert plan["intent_id"] == "intent.unspecified"
        assert plan["plan_id"].startswith("intent.unspecified:")

    def test_configurable_intent_id(self) -> None:
        agent = _FakeDecide(_spec(), config={"intent_id": "FNET-INT-001"})
        out = agent.run(self._inputs())
        assert out["artifact:proposed.plan"]["intent_id"] == "FNET-INT-001"

    def test_decisions_skip_non_mapping_values(self) -> None:
        inputs = self._inputs()
        inputs["state:analysis.result"]["final_patch"]["garbage"] = "not-a-mapping"
        agent = _FakeDecide(_spec(), config={})
        out = agent.run(inputs)
        plan = out["artifact:proposed.plan"]
        # Only "throttle" is a mapping; "garbage" is dropped
        assert len(plan["decisions"]) == 1

    def test_idempotency_key_per_actuator(self) -> None:
        agent = _FakeDecide(_spec(), config={"intent_id": "i1"})
        out = agent.run(self._inputs())
        assert out["artifact:proposed.plan"]["decisions"][0]["idempotency_key"] == ("i1:snap-1:throttle")

    def test_make_effect_ref_override(self) -> None:
        class CustomDecide(DecideBaseAgent):
            def make_effect_ref(self, actuator: str) -> str:
                return f"custom://{actuator}/v2"

        agent = CustomDecide(_spec(), config={})
        out = agent.run(self._inputs())
        assert out["artifact:proposed.plan"]["decisions"][0]["effect_ref"] == "custom://throttle/v2"

    def test_missing_inputs_raises(self) -> None:
        agent = _FakeDecide(_spec(), config={})
        with pytest.raises(RuntimeError, match="state:env.snapshot"):
            agent.run({})
        with pytest.raises(RuntimeError, match="state:analysis.result"):
            agent.run({"state:env.snapshot": {}})


# ─── ActBaseAgent ────────────────────────────────────────────────


class _FakeAct(ActBaseAgent):
    def __init__(self, spec, config, *, transport=None, accept=True, fail_with=None):
        super().__init__(spec, config, transport=transport)
        self._accept = accept
        self._fail_with = fail_with
        self.dispatched: list[Mapping[str, Any]] = []

    def dispatch_decision(self, decision: Mapping[str, Any]) -> ActOutcome:
        self.dispatched.append(dict(decision))
        if self._fail_with is not None:
            raise RuntimeError(self._fail_with)
        return ActOutcome(
            accepted=self._accept,
            execution_id=f"exec-{len(self.dispatched)}",
            message="ok" if self._accept else "rejected",
        )


class TestActBase:
    def _inputs(self) -> dict[str, Any]:
        return {
            "state:env.snapshot": {"snapshot_id": "s1", "data_hash": "h1"},
            "artifact:proposed.plan": {
                "plan_id": "p1",
                "decisions": [
                    {
                        "effect_ref": "x:throttle",
                        "target_state": {"qps": 10},
                        "idempotency_key": "k1",
                    }
                ],
            },
        }

    def test_successful_dispatch(self) -> None:
        agent = _FakeAct(_spec(), config={})
        out = agent.run(self._inputs())
        report = out["artifact:execution.report"]
        assert report["status"] == "succeeded"
        assert report["artifact_refs"]["x:throttle"]["status"] == "applied"
        assert report["policy_decisions"][0]["allowed"] is True
        assert "state:act.result" in out

    def test_exception_captured_as_error(self) -> None:
        agent = _FakeAct(_spec(), config={}, fail_with="connector down")
        out = agent.run(self._inputs())
        report = out["artifact:execution.report"]
        assert report["status"] == "partial"
        assert "connector down" in report["errors"][0]
        assert report["artifact_refs"]["x:throttle"]["status"] == "error"

    def test_execution_hash_deterministic(self) -> None:
        agent1 = _FakeAct(_spec(), config={})
        agent2 = _FakeAct(_spec(), config={})
        h1 = agent1.run(self._inputs())["artifact:execution.report"]["execution_hash"]
        h2 = agent2.run(self._inputs())["artifact:execution.report"]["execution_hash"]
        assert h1 == h2

    def test_missing_plan_raises(self) -> None:
        agent = _FakeAct(_spec(), config={})
        with pytest.raises(RuntimeError, match="artifact:proposed.plan"):
            agent.run({"state:env.snapshot": {"snapshot_id": "s"}})


# ─── Transport seam ──────────────────────────────────────────────


class TestTransportSeam:
    def test_default_transport_is_inprocess(self) -> None:
        agent = _FakeObserve(_spec(), config={})
        assert isinstance(agent.transport, InProcessTransport)

    def test_explicit_transport_honored(self) -> None:
        custom = InProcessTransport(known_peers=["peer-x"])
        agent = _FakeObserve(_spec(), config={}, transport=custom)
        assert agent.transport is custom
        assert "peer-x" in agent.transport.peers()

    def test_all_four_bases_accept_transport(self) -> None:
        t = InProcessTransport()

        class _A(AnalyzeBaseAgent):
            def extract_observations(self, metrics):
                return {}

            def evaluate_policy(self, obs, state):
                return {"final_patch": {}, "actions": []}

        observe = _FakeObserve(_spec(), config={}, transport=t)
        analyze = _A(_spec(), config={}, transport=t)
        decide = _FakeDecide(_spec(), config={}, transport=t)
        act = _FakeAct(_spec(), config={}, transport=t)
        for a in (observe, analyze, decide, act):
            assert a.transport is t


class TestInProcessTransport:
    def test_send_then_recv_fifo(self) -> None:
        t = InProcessTransport()
        t.send("alice", {"n": 1})
        t.send("alice", {"n": 2})
        assert t.recv("alice") == {"n": 1}
        assert t.recv("alice") == {"n": 2}
        assert t.recv("alice") is None

    def test_recv_unknown_peer_returns_none(self) -> None:
        t = InProcessTransport()
        assert t.recv("nobody") is None

    def test_known_peers_initialization(self) -> None:
        t = InProcessTransport(known_peers=["a", "b", "c"])
        peers = set(t.peers())
        assert peers == {"a", "b", "c"}

    def test_broadcast_to_all_known_peers(self) -> None:
        t = InProcessTransport(known_peers=["a", "b"])
        t.broadcast({"hello": "all"})
        assert t.recv("a") == {"hello": "all"}
        assert t.recv("b") == {"hello": "all"}

    def test_broadcast_skips_no_peers(self) -> None:
        t = InProcessTransport()
        t.broadcast({"hello": "all"})  # no peers, no-op
        assert list(t.peers()) == []

    def test_pending_count(self) -> None:
        t = InProcessTransport()
        t.send("a", {"x": 1})
        t.send("a", {"x": 2})
        assert t.pending_count("a") == 2
        assert t.pending_count("missing") == 0

    def test_send_creates_mailbox_lazily(self) -> None:
        t = InProcessTransport()
        t.send("brand-new", {"k": 1})
        assert "brand-new" in t.peers()

    def test_message_is_copied_on_send(self) -> None:
        """Mutating the original payload must not affect the queued message."""
        t = InProcessTransport()
        payload = {"k": 1}
        t.send("a", payload)
        payload["k"] = 999
        assert t.recv("a") == {"k": 1}

    def test_satisfies_transport_protocol(self) -> None:
        """Phase 6 will add HttpTransport etc.; the protocol contract is enforced."""
        t = InProcessTransport()
        assert isinstance(t, Transport)
