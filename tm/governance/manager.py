"""Governance orchestration for budgets, rate limits, and circuit breakers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from tm.guard import GuardDecision, GuardEngine, GuardRule
from tm.obs import counters

from .audit import AuditTrail
from .budget import BudgetTracker
from .breaker import BreakerState, CircuitBreaker
from .config import (
    BreakerSettings,
    GovernanceConfig,
    LimitKey,
    LimitSettings,
    load_governance_config,
)
from .hitl import HitlManager
from .ratelimit import RateTracker

BreakerKey = Tuple[str, Optional[str]]


@dataclass(frozen=True)
class RequestDescriptor:
    flow: str
    binding: Optional[str] = None
    policy_arm: Optional[str] = None


@dataclass
class LimitReservation:
    key: LimitKey
    rate: RateTracker
    budget: BudgetTracker


@dataclass
class BreakerReservation:
    key: BreakerKey
    breaker: CircuitBreaker


@dataclass
class GovernanceDecision:
    allowed: bool
    error_code: Optional[str] = None
    scope: Optional[str] = None
    meta: Dict[str, object] = field(default_factory=dict)
    limit_reservations: Tuple[LimitReservation, ...] = ()
    breaker_reservations: Tuple[BreakerReservation, ...] = ()


@dataclass
class _LimitState:
    settings: LimitSettings
    rate: RateTracker
    budget: BudgetTracker


@dataclass
class _BreakerState:
    settings: BreakerSettings
    breaker: CircuitBreaker


class GovernanceManager:
    """Apply governance controls to flow executions."""

    def __init__(
        self,
        config: Optional[GovernanceConfig] = None,
        *,
        clock=time.monotonic,
    ) -> None:
        self._config = config or load_governance_config()
        self._clock = clock
        self._limits_enabled = self._config.limits_enabled()
        self._breaker_enabled = self._config.breaker_enabled()
        self._limit_states: Dict[LimitKey, _LimitState] = {}
        self._breaker_states: Dict[BreakerKey, _BreakerState] = {}
        self._audit = AuditTrail(self._config.audit)
        self._guard_engine = GuardEngine()
        guard_cfg = self._config.guard
        self._guard_global = GuardEngine.compile_rules(guard_cfg.global_rules)
        self._guard_flow = {name: GuardEngine.compile_rules(rules) for name, rules in guard_cfg.flow_rules.items()}
        self._guard_policy = {name: GuardEngine.compile_rules(rules) for name, rules in guard_cfg.policy_rules.items()}
        self._hitl = HitlManager(self._config.hitl, audit=self._audit)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def check(self, request: RequestDescriptor) -> GovernanceDecision:
        """Evaluate governance controls prior to enqueuing the request."""
        if not (self._limits_enabled or self._breaker_enabled):
            return GovernanceDecision(True)

        now = self._clock()

        breaker_handles: List[BreakerReservation] = []
        if self._breaker_enabled:
            for breaker_key, settings in self._breaker_scopes(request):
                breaker_state = self._ensure_breaker_state(breaker_key, settings)
                decision = breaker_state.breaker.can_execute(now=now)
                if not decision.allowed:
                    self._record_breaker_reject(breaker_key, decision.state)
                    self._release_breakers(breaker_handles)
                    return GovernanceDecision(
                        False,
                        error_code=decision.reason or "CIRCUIT_OPEN",
                        scope=_format_breaker_scope(breaker_key),
                    )
                breaker_handles.append(BreakerReservation(key=breaker_key, breaker=breaker_state.breaker))

        reservations: List[LimitReservation] = []
        if self._limits_enabled:
            for limit_key, limit_settings in self._limit_scopes(request):
                limit_state = self._ensure_limit_state(limit_key, limit_settings)

                rate_decision = limit_state.rate.check_and_reserve(now=now)
                if not rate_decision.allowed:
                    self._record_rate_reject(limit_key)
                    self._rollback_limits(reservations)
                    self._release_breakers(breaker_handles)
                    return GovernanceDecision(
                        False,
                        error_code=rate_decision.reason or "RATE_LIMITED",
                        scope=_format_limit_scope(limit_key),
                    )

                budget_decision = limit_state.budget.can_accept(now=now)
                if not budget_decision.allowed:
                    limit_state.rate.cancel_pending()
                    self._rollback_limits(reservations)
                    self._release_breakers(breaker_handles)
                    meta: Dict[str, object] = {}
                    if budget_decision.kind:
                        meta["budget_kind"] = budget_decision.kind
                    self._record_budget_reject(limit_key, budget_decision.kind)
                    return GovernanceDecision(
                        False,
                        error_code=budget_decision.reason or "BUDGET_EXCEEDED",
                        scope=_format_limit_scope(limit_key),
                        meta=meta,
                    )

                reservations.append(LimitReservation(key=limit_key, rate=limit_state.rate, budget=limit_state.budget))

        return GovernanceDecision(
            True,
            limit_reservations=tuple(reservations),
            breaker_reservations=tuple(breaker_handles),
        )

    def evaluate_guard(self, payload: Mapping[str, Any], request: RequestDescriptor) -> GuardDecision:
        if not self._config.guard_enabled():
            return GuardDecision(True, ())
        rules: List[GuardRule] = list(self._guard_global)
        flow_rules = self._guard_flow.get(request.flow)
        if flow_rules:
            rules.extend(flow_rules)
        policy = request.binding
        if policy:
            policy_rules = self._guard_policy.get(policy)
            if policy_rules:
                rules.extend(policy_rules)
        if not rules:
            return GuardDecision(True, ())
        decision = self._guard_engine.evaluate(
            payload, rules, context={"flow": request.flow, "binding": request.binding}
        )
        if not decision.allowed and self._audit.enabled:
            first = decision.first
            meta = first.as_dict() if first else {"reason": "guard_blocked"}
            meta.setdefault("flow", request.flow)
            if request.binding:
                meta.setdefault("binding", request.binding)
            self._audit.record("guard_block", meta)
        return decision

    def evaluate_custom_guard(
        self,
        rules: Iterable[Mapping[str, Any]],
        payload: Mapping[str, Any],
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> GuardDecision:
        compiled = GuardEngine.compile_rules(rules)
        decision = self._guard_engine.evaluate(payload, compiled, context=context or {})
        if not decision.allowed and self._audit.enabled:
            first = decision.first
            meta = first.as_dict() if first else {"reason": "guard_blocked"}
            self._audit.record("guard_block", meta)
        return decision

    def activate(self, decision: GovernanceDecision) -> None:
        """Mark pending reservations as active when execution begins."""
        if not decision.allowed:
            return
        now = self._clock()
        for reservation in decision.limit_reservations:
            reservation.rate.activate(now=now)
            self._update_concurrency_gauge(reservation)

    def finalize(
        self,
        decision: GovernanceDecision,
        request: RequestDescriptor,
        *,
        status: str,
        error_code: Optional[str],
        tokens: Optional[float],
        cost: Optional[float],
    ) -> None:
        """Update trackers and breakers after a request finishes."""
        if not decision.allowed:
            return
        _ = request  # reserved for audit/trace integration
        now = self._clock()
        success = status == "ok"
        timeout = _looks_like_timeout(error_code)

        for reservation in decision.limit_reservations:
            reservation.rate.release()
            reservation.budget.record(tokens=tokens, cost=cost, now=now)
            self._update_concurrency_gauge(reservation)
            usage_tokens, usage_cost = reservation.budget.snapshot(now=now)
            self._record_budget_usage(reservation.key, "tokens", usage_tokens)
            self._record_budget_usage(reservation.key, "cost", usage_cost)

        for handle in decision.breaker_reservations:
            handle.breaker.release_half_open_slot()
            if success:
                handle.breaker.record_success(now=now)
            else:
                handle.breaker.record_failure(now=now, timeout=timeout)
            self._record_breaker_state(handle.key, handle.breaker.state)

    def cancel(self, decision: GovernanceDecision) -> None:
        """Rollback reservations when a request never executes (e.g. queue rejection)."""
        if not decision.allowed:
            return
        for reservation in decision.limit_reservations:
            reservation.rate.cancel_pending()
        self._release_breakers(decision.breaker_reservations)

    @property
    def hitl(self) -> HitlManager:
        return self._hitl

    @property
    def audit(self) -> AuditTrail:
        return self._audit

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _limit_scopes(self, request: RequestDescriptor) -> Iterable[Tuple[LimitKey, LimitSettings]]:
        limits = self._config.limits
        if not limits.enabled:
            return []

        scopes: List[Tuple[LimitKey, LimitSettings]] = []
        if request.binding and request.policy_arm:
            cfg = limits.per_policy_arm.get((request.binding, request.policy_arm))
            if cfg is not None:
                scopes.append((("policy", request.binding, request.policy_arm), cfg))
        if request.flow:
            cfg = limits.per_flow.get(request.flow)
            if cfg is not None:
                scopes.append((("flow", request.flow, None), cfg))
        if limits.global_scope.is_active():
            scopes.append((("global", None, None), limits.global_scope))
        return scopes

    def _breaker_scopes(self, request: RequestDescriptor) -> Iterable[Tuple[BreakerKey, BreakerSettings]]:
        breaker = self._config.breaker
        if not breaker.enabled:
            return []
        scopes: List[Tuple[BreakerKey, BreakerSettings]] = []
        if request.binding:
            cfg = breaker.per_policy.get(request.binding)
            if cfg is not None:
                scopes.append((("policy", request.binding), cfg))
        if request.flow:
            cfg = breaker.per_flow.get(request.flow)
            if cfg is not None:
                scopes.append((("flow", request.flow), cfg))
        if breaker.global_scope.is_active():
            scopes.append((("global", None), breaker.global_scope))
        return scopes

    def _ensure_limit_state(self, key: LimitKey, settings: LimitSettings) -> _LimitState:
        state = self._limit_states.get(key)
        if state is None:
            state = _LimitState(settings=settings, rate=RateTracker(settings), budget=BudgetTracker(settings))
            self._limit_states[key] = state
        return state

    def _ensure_breaker_state(self, key: BreakerKey, settings: BreakerSettings) -> _BreakerState:
        state = self._breaker_states.get(key)
        if state is None:
            state = _BreakerState(settings=settings, breaker=CircuitBreaker(settings))
            self._breaker_states[key] = state
        return state

    def _rollback_limits(self, reservations: Iterable[LimitReservation]) -> None:
        for reservation in reservations:
            reservation.rate.cancel_pending()

    def _release_breakers(self, reservations: Iterable[BreakerReservation]) -> None:
        for handle in reservations:
            handle.breaker.release_half_open_slot()

    def _record_rate_reject(self, key: LimitKey) -> None:
        counters.metrics.get_counter("tm_govern_qps_limited_total").inc(labels={"scope": _format_limit_scope(key)})

    def _record_budget_reject(self, key: LimitKey, kind: Optional[str]) -> None:
        labels = {"scope": _format_limit_scope(key)}
        if kind:
            labels["kind"] = kind
        counters.metrics.get_counter("tm_govern_budget_exceeded_total").inc(labels=labels)

    def _record_breaker_reject(self, key: BreakerKey, state: BreakerState) -> None:
        labels = {"target": _format_breaker_scope(key), "state": state.value}
        counters.metrics.get_counter("tm_breaker_trips_total").inc(labels=labels)
        self._record_breaker_state(key, state)

    def _record_budget_usage(self, key: LimitKey, kind: str, value: Optional[float]) -> None:
        if value is None:
            return
        labels = {"scope": _format_limit_scope(key), "kind": kind}
        counters.metrics.get_gauge("tm_govern_budget_usage").set(float(value), labels=labels)

    def _record_breaker_state(self, key: BreakerKey, state: BreakerState) -> None:
        value = {
            BreakerState.CLOSED: 0.0,
            BreakerState.HALF_OPEN: 0.5,
            BreakerState.OPEN: 1.0,
        }[state]
        counters.metrics.get_gauge("tm_breaker_state").set(
            value,
            labels={"target": _format_breaker_scope(key)},
        )

    def _update_concurrency_gauge(self, reservation: LimitReservation) -> None:
        level, name, _ = reservation.key
        if level != "flow" or not name:
            return
        counters.metrics.get_gauge("tm_govern_current_concurrency").set(
            float(reservation.rate.active),
            labels={"flow": name},
        )


def _format_limit_scope(key: LimitKey) -> str:
    level, name, arm = key
    if level == "global":
        return "global"
    if level == "flow" and name:
        return f"flow:{name}"
    if level == "policy" and name and arm:
        return f"policy:{name}:{arm}"
    return level


def _format_breaker_scope(key: BreakerKey) -> str:
    level, name = key
    if level == "global" or not name:
        return "global"
    return f"{level}:{name}"


def _looks_like_timeout(error_code: Optional[str]) -> bool:
    if not error_code:
        return False
    lowered = error_code.lower()
    return "timeout" in lowered or lowered == "asyncio.timeouterror"


__all__ = [
    "BreakerReservation",
    "GovernanceDecision",
    "GovernanceManager",
    "LimitReservation",
    "RequestDescriptor",
]
