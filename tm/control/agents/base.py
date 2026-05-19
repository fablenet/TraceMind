"""Generic MAPE-K agent base classes for the L1 controller cycle.

Provides four abstract base classes — one per MAPE-K stage — that lift the
common skeleton out of any specific domain (FableNet, K8s, etc.). Domain
subclasses fill in three hooks:

1. The actual data-plane I/O (Observe / Act)
2. Metric extraction / decision shape (Analyze / Decide)
3. Optional ``Transport``-mediated coordination with peer agents (Phase 6)

These base classes are **additive** — existing domain agents (e.g. those
in ``fablenet-control/agents/``) keep working unchanged. Future domain
implementations should subclass these to inherit the standard contract.

## Why a Transport seam?

Each base class accepts an optional :class:`Transport` parameter
(``tm.control.agents.transport.Transport``). In Phase 5 the default is
:class:`InProcessTransport`, so the seam has no observable effect on
single-process bundles. In Phase 6 we will introduce remote transports
(HTTP, gRPC, file-queue) to support distributed star / tree AgentNetwork
topologies; agents written against this base class will then become
**network-agnostic** with zero code changes — only configuration.

## Common contract

All four base classes share the standard ``RuntimeAgent`` lifecycle:
``__init__`` → ``init(ctx)`` → ``run(inputs)`` → ``finalize()``. The
``run`` method is intentionally implemented by the base class as a template
method that calls abstract hooks. Subclasses **must not** override ``run``
unless they need to bypass the template completely.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import abstractmethod
from typing import Any, ClassVar, List, Mapping, MutableMapping, Sequence

from tm.agents.models import AgentSpec
from tm.agents.runtime import RuntimeAgent, RuntimeInputs, RuntimeOutputs

from .transport import InProcessTransport, Transport


def _stable_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─── Base: shared transport plumbing ─────────────────────────────


class _TransportAwareAgent(RuntimeAgent):
    """Mixin-style base that adds Transport injection on top of RuntimeAgent.

    All four MAPE-K base classes inherit from this so they uniformly accept a
    ``transport`` keyword argument. The default :class:`InProcessTransport`
    keeps existing single-process behavior intact.
    """

    DEFAULT_SNAPSHOT_PREFIX: ClassVar[str] = "snap"

    def __init__(
        self,
        spec: AgentSpec,
        config: Mapping[str, Any],
        *,
        transport: Transport | None = None,
    ) -> None:
        super().__init__(spec, config)
        self._transport: Transport = transport if transport is not None else InProcessTransport()

    @property
    def transport(self) -> Transport:
        return self._transport


# ─── M: Observe ──────────────────────────────────────────────────


class ObserveBaseAgent(_TransportAwareAgent):
    """MAPE-K **M** (monitor/observe) base.

    Standardizes the ``state:env.snapshot`` artifact shape. Subclasses only
    implement :meth:`collect_environment` — the rest (snapshot id, timestamp,
    data hash, constraint plumbing) is handled here.
    """

    @abstractmethod
    def collect_environment(self, inputs: RuntimeInputs) -> Mapping[str, Any]:
        """Domain-specific: produce the ``environment`` dict for the snapshot.

        Implementations typically call into a data-plane connector (gRPC,
        REST, file watch, K8s API, …) and return a structured dict containing
        at least ``metrics`` (a flat dict[str, str|number]) and any other
        domain-relevant facts (events, phase, etc.).
        """

    def build_constraints(self) -> List[Mapping[str, Any]]:
        """Override to provide domain-specific observation constraints.

        Default behavior: read ``constraints`` from agent config; if absent
        or malformed, return an empty list. Domains can override to provide
        a fixed set of guard rules.
        """
        raw = self.config.get("constraints")
        if isinstance(raw, list):
            return [dict(c) for c in raw if isinstance(c, Mapping)]
        return []

    def snapshot_id(self) -> str:
        """Override to use a domain-specific snapshot id scheme."""
        prefix = str(self.config.get("snapshot_id_prefix", self.DEFAULT_SNAPSHOT_PREFIX))
        return f"{prefix}-{int(time.time() * 1000)}"

    def run(self, inputs: RuntimeInputs) -> RuntimeOutputs:
        environment = self.collect_environment(inputs)
        if not isinstance(environment, Mapping):
            raise TypeError("collect_environment must return a Mapping")
        constraints = self.build_constraints()
        snapshot = {
            "snapshot_id": self.snapshot_id(),
            "timestamp": _utc_now(),
            "environment": dict(environment),
            "constraints": list(constraints),
            "data_hash": _stable_hash(environment),
        }
        self.add_evidence("control.observe.snapshot", {"snapshot_id": snapshot["snapshot_id"]})
        return {"state:env.snapshot": snapshot}


# ─── A: Analyze ──────────────────────────────────────────────────


class AnalyzeBaseAgent(_TransportAwareAgent):
    """MAPE-K **A** (analyze) base.

    Reads ``state:env.snapshot``, extracts numeric observations via the
    domain-specific :meth:`extract_observations` hook, then invokes
    :meth:`evaluate_policy` (also domain-defined) to produce a
    ``final_patch`` (actuator → target_state mapping) plus a matched rule id.
    """

    @abstractmethod
    def extract_observations(self, metrics: Mapping[str, Any]) -> Mapping[str, float]:
        """Domain-specific: pull numeric obs from raw metrics dict."""

    @abstractmethod
    def evaluate_policy(
        self,
        obs: Mapping[str, float],
        state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Domain-specific: run policy engine. Must return a dict containing
        keys ``final_patch`` (Mapping) and ``actions`` (Sequence of mappings).

        Typical implementation: load a ``PolicyEngine`` from
        ``tm.policy.deterministic`` (path from config) and call ``evaluate``.
        """

    def run(self, inputs: RuntimeInputs) -> RuntimeOutputs:
        snapshot = inputs.get("state:env.snapshot")
        if not isinstance(snapshot, Mapping):
            raise RuntimeError(f"{type(self).__name__} requires 'state:env.snapshot'")
        env = snapshot.get("environment", {})
        metrics = env.get("metrics", {}) if isinstance(env, Mapping) else {}
        if not isinstance(metrics, Mapping):
            metrics = {}
        obs = self.extract_observations(metrics)
        if not isinstance(obs, Mapping):
            raise TypeError("extract_observations must return a Mapping")
        result = self.evaluate_policy(obs, {})
        if not isinstance(result, Mapping):
            raise TypeError("evaluate_policy must return a Mapping")

        patch = result.get("final_patch", {})
        if not isinstance(patch, Mapping):
            patch = {}
        actions = result.get("actions", [])
        if not isinstance(actions, Sequence) or isinstance(actions, str):
            actions = []

        matched_rule = ""
        for action in actions:
            if isinstance(action, Mapping) and action.get("applied"):
                matched_rule = str(action.get("rule_id", ""))
                break

        analysis = {
            "obs": dict(obs),
            "matched_rule": matched_rule,
            "final_patch": dict(patch),
            "action_count": len(actions),
        }
        self.add_evidence(
            "control.analyze.result",
            {
                "matched_rule": matched_rule,
                "patch_keys": list(patch.keys()),
                "obs": dict(obs),
            },
        )
        return {"state:analysis.result": analysis}


# ─── P: Plan / Decide ────────────────────────────────────────────


class DecideBaseAgent(_TransportAwareAgent):
    """MAPE-K **P** (plan / decide) base.

    Converts the ``state:analysis.result`` produced by :class:`AnalyzeBaseAgent`
    into a generic ``artifact:proposed.plan``. The effect_ref namespace is
    configurable, so different domains can use ``fablenet:`` / ``k8s:`` / ...
    as prefixes without subclassing this stage at all.
    """

    DEFAULT_EFFECT_REF_NAMESPACE: ClassVar[str] = "generic"
    DEFAULT_INTENT_ID: ClassVar[str] = "intent.unspecified"
    DEFAULT_DETERMINISTIC_MODEL: ClassVar[str] = "deterministic-policy-engine"

    def effect_ref_namespace(self) -> str:
        return str(self.config.get("effect_ref_namespace", self.DEFAULT_EFFECT_REF_NAMESPACE))

    def intent_id(self) -> str:
        return str(self.config.get("intent_id", self.DEFAULT_INTENT_ID))

    def make_effect_ref(self, actuator: str) -> str:
        """Override to provide a domain-specific effect_ref builder."""
        return f"{self.effect_ref_namespace()}:{actuator}"

    def patch_to_decisions(
        self,
        patch: Mapping[str, Any],
        plan_id: str,
    ) -> List[MutableMapping[str, Any]]:
        decisions: List[MutableMapping[str, Any]] = []
        for actuator, value in patch.items():
            if not isinstance(value, Mapping):
                continue
            decisions.append(
                {
                    "effect_ref": self.make_effect_ref(str(actuator)),
                    "target_state": dict(value),
                    "idempotency_key": f"{plan_id}:{actuator}",
                }
            )
        return decisions

    def run(self, inputs: RuntimeInputs) -> RuntimeOutputs:
        snapshot = inputs.get("state:env.snapshot")
        if not isinstance(snapshot, Mapping):
            raise RuntimeError(f"{type(self).__name__} requires 'state:env.snapshot'")
        analysis = inputs.get("state:analysis.result")
        if not isinstance(analysis, Mapping):
            raise RuntimeError(f"{type(self).__name__} requires 'state:analysis.result'")

        intent_id = self.intent_id()
        snapshot_id = str(snapshot.get("snapshot_id", "unknown"))
        plan_id = f"{intent_id}:{snapshot_id}"

        patch = analysis.get("final_patch", {})
        if not isinstance(patch, Mapping):
            patch = {}
        matched_rule = str(analysis.get("matched_rule", ""))

        decisions = self.patch_to_decisions(patch, plan_id)
        policy_requirements = [str(d.get("effect_ref", "")) for d in decisions]

        plan = {
            "plan_id": plan_id,
            "intent_id": intent_id,
            "decisions": decisions,
            "llm_metadata": {
                "model": str(self.config.get("decide_model", self.DEFAULT_DETERMINISTIC_MODEL)),
                "prompt_hash": _stable_hash(dict(analysis)),
                "determinism_hint": "deterministic",
            },
            "summary": (f"Policy rule '{matched_rule}' fired" if matched_rule else "No policy rule matched"),
            "policy_requirements": policy_requirements,
        }

        self.add_evidence(
            "control.decide.plan",
            {
                "plan_id": plan_id,
                "matched_rule": matched_rule,
                "decision_count": len(decisions),
            },
        )
        return {"artifact:proposed.plan": plan}


# ─── E: Execute / Act ────────────────────────────────────────────


class ActBaseAgent(_TransportAwareAgent):
    """MAPE-K **E** (execute / act) base.

    Standardizes the ``artifact:execution.report`` shape. Subclasses implement
    :meth:`dispatch_decision`, which takes one decision (with effect_ref and
    target_state) and performs the actual data-plane mutation, returning an
    :class:`ActOutcome` describing success/failure.
    """

    @abstractmethod
    def dispatch_decision(self, decision: Mapping[str, Any]) -> "ActOutcome":
        """Domain-specific: execute a single decision against the data plane.

        Must return an :class:`ActOutcome` capturing accepted / execution_id /
        message. Raising is allowed but **strongly discouraged** — prefer
        returning ``accepted=False`` with a descriptive ``message`` so the
        controller cycle can record the failure as evidence.
        """

    def run(self, inputs: RuntimeInputs) -> RuntimeOutputs:
        snapshot = inputs.get("state:env.snapshot")
        if not isinstance(snapshot, Mapping):
            raise RuntimeError(f"{type(self).__name__} requires 'state:env.snapshot'")
        plan = inputs.get("artifact:proposed.plan")
        if not isinstance(plan, Mapping):
            raise RuntimeError(f"{type(self).__name__} requires 'artifact:proposed.plan'")

        decisions = plan.get("decisions", [])
        if not isinstance(decisions, Sequence) or isinstance(decisions, str):
            raise RuntimeError("plan.decisions must be a sequence")

        artifact_refs: dict[str, Any] = {}
        policy_decisions: list[dict[str, Any]] = []
        errors: list[str] = []

        for decision in decisions:
            if not isinstance(decision, Mapping):
                raise RuntimeError("each decision must be a mapping")
            effect_ref = str(decision.get("effect_ref", ""))
            try:
                outcome = self.dispatch_decision(decision)
            except Exception as exc:  # noqa: BLE001 — capture domain errors as evidence
                errors.append(f"{effect_ref}: {exc}")
                artifact_refs[effect_ref] = {
                    "status": "error",
                    "execution_id": "",
                    "message": str(exc),
                }
                continue
            artifact_refs[effect_ref] = {
                "status": "applied" if outcome.accepted else outcome.skipped_reason or "rejected",
                "execution_id": outcome.execution_id,
                "message": outcome.message,
            }
            if not outcome.accepted and outcome.skipped_reason:
                errors.append(f"{effect_ref}: {outcome.skipped_reason}")
            policy_decisions.append(
                {
                    "effect_ref": effect_ref,
                    "allowed": outcome.accepted,
                    "reason": "executed" if outcome.accepted else outcome.message,
                    "idempotency_key": str(decision.get("idempotency_key", "")),
                }
            )
            self.add_evidence(
                "control.act.command",
                {
                    "effect_ref": effect_ref,
                    "accepted": outcome.accepted,
                    "execution_id": outcome.execution_id,
                },
            )

        status = "succeeded" if not errors else ("partial" if artifact_refs else "failed")
        report = {
            "report_id": str(plan.get("plan_id", "unknown")),
            "artifact_refs": artifact_refs,
            "status": status,
            "policy_decisions": policy_decisions,
            "errors": errors,
            "artifacts": {
                "ObserveAgent": {
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "data_hash": snapshot.get("data_hash"),
                },
                "DecideAgent": {
                    "plan_id": plan.get("plan_id"),
                    "summary": plan.get("summary"),
                },
                "ActAgent": {"artifact_refs": artifact_refs},
            },
            "execution_hash": _stable_hash(
                {
                    "artifact_refs": artifact_refs,
                    "policy_decisions": policy_decisions,
                    "status": status,
                }
            ),
        }
        return {
            "artifact:execution.report": report,
            "state:act.result": {"artifact_refs": artifact_refs, "status": status},
        }


from dataclasses import dataclass  # noqa: E402 — kept close to its only user


@dataclass
class ActOutcome:
    """Result of dispatching a single decision in :class:`ActBaseAgent`."""

    accepted: bool
    execution_id: str = ""
    message: str = ""
    skipped_reason: str = ""


__all__ = [
    "ObserveBaseAgent",
    "AnalyzeBaseAgent",
    "DecideBaseAgent",
    "ActBaseAgent",
    "ActOutcome",
]
