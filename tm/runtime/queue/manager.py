from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .base import WorkQueue
from ..idempotency import IdempotencyResult, IdempotencyStore
from ..task import TaskEnvelope
from tm.obs import counters

LOGGER = logging.getLogger("tm.queue.manager")


@dataclass(frozen=True)
class ManagedLease:
    offset: int
    token: str
    deadline: float
    envelope: TaskEnvelope

    @property
    def task_id(self) -> str:
        return self.envelope.task_id

    @property
    def flow_id(self) -> str:
        return self.envelope.flow_id


@dataclass(frozen=True)
class EnqueueOutcome:
    queued: bool
    envelope: Optional[TaskEnvelope]
    cached_result: Optional[IdempotencyResult] = None


class TaskQueueManager:
    """High-level helper that owns a work queue plus idempotency store."""

    def __init__(
        self,
        work_queue: WorkQueue,
        idempotency: IdempotencyStore,
        *,
        dead_letters: Optional[Any] = None,
        retry_policy: Optional[Any] = None,
        default_ttl: float = 3600.0,
        clock=time.monotonic,
    ) -> None:
        self._queue = work_queue
        self._idempotency = idempotency
        self._dead_letters = dead_letters
        self._retry_policy = retry_policy
        self._clock = clock
        self._default_ttl = max(default_ttl, 60.0)
        self._lock = threading.Lock()
        self._pending_keys: dict[str, str] = {}  # composite key -> task_id
        self._task_offsets: dict[str, int] = {}
        self._inflight = 0
        self._record_queue_metrics()

    # ------------------------------------------------------------------
    def enqueue(
        self,
        *,
        flow_id: str,
        input: Mapping[str, Any],
        headers: Optional[Mapping[str, Any]] = None,
        trace: Optional[Mapping[str, Any]] = None,
        idempotency_ttl: Optional[float] = None,
    ) -> EnqueueOutcome:
        envelope = TaskEnvelope.new(flow_id=flow_id, input=input, headers=headers or {}, trace=trace or {})
        idem_key = envelope.idempotency_key
        composite = envelope.composite_key

        if idem_key:
            cached = self._idempotency.get(composite)
            if cached is not None:
                counters.metrics.get_counter(
                    "tm_queue_idempo_hits_total",
                    help="Idempotency cache hits",
                ).inc(labels={"flow": flow_id})
                return EnqueueOutcome(queued=False, envelope=None, cached_result=cached)
            with self._lock:
                existing_task = self._pending_keys.get(composite)
                if existing_task is not None:
                    return EnqueueOutcome(queued=False, envelope=None, cached_result=None)
                self._pending_keys[composite] = envelope.task_id
            self._store_envelope(envelope)
            counters.metrics.get_counter(
                "tm_queue_enqueued_total",
                help="Total tasks enqueued",
            ).inc(labels={"flow": flow_id})
            return EnqueueOutcome(queued=True, envelope=envelope)
        self._store_envelope(envelope)
        counters.metrics.get_counter(
            "tm_queue_enqueued_total",
            help="Total tasks enqueued",
        ).inc(labels={"flow": flow_id})
        return EnqueueOutcome(queued=True, envelope=envelope)

    def lease(self, count: int, lease_ms: int) -> list[ManagedLease]:
        leased = self._queue.lease(count, lease_ms)
        managed: list[ManagedLease] = []
        for item in leased:
            envelope = TaskEnvelope.from_dict(item.task)
            managed.append(
                ManagedLease(
                    offset=item.offset,
                    token=item.token,
                    deadline=item.lease_deadline,
                    envelope=envelope,
                )
            )
        if managed:
            with self._lock:
                self._inflight += len(managed)
            self._record_queue_metrics()
        return managed

    def ack(self, lease: ManagedLease, *, clear_pending: bool = True, record_metrics: bool = True) -> None:
        self._queue.ack(lease.offset, lease.token)
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1
            self._task_offsets.pop(lease.envelope.task_id, None)
        if clear_pending:
            self._clear_pending(lease.envelope)
        if record_metrics:
            counters.metrics.get_counter(
                "tm_queue_acked_total",
                help="Total tasks acknowledged",
            ).inc(labels={"flow": lease.flow_id})
        self._record_queue_metrics()

    def nack(self, lease: ManagedLease, *, requeue: bool = True) -> None:
        self._queue.nack(lease.offset, lease.token, requeue=requeue)
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1
            self._task_offsets.pop(lease.envelope.task_id, None)
        if not requeue:
            self._clear_pending(lease.envelope)
        counters.metrics.get_counter(
            "tm_queue_nacked_total",
            help="Total tasks nacked",
        ).inc(
            labels={
                "flow": lease.flow_id,
                "requeue": str(requeue).lower(),
            }
        )
        self._record_queue_metrics()

    def record_result(
        self,
        envelope: TaskEnvelope,
        result: IdempotencyResult,
        *,
        ttl: Optional[float] = None,
    ) -> None:
        ttl_value = ttl if ttl is not None else self._default_ttl
        self._idempotency.remember(envelope.composite_key, result, ttl_value)
        self._clear_pending(envelope)
        self._record_queue_metrics()

    def record_retry(
        self,
        lease: ManagedLease,
        *,
        delay_seconds: float,
    ) -> TaskEnvelope:
        self.ack(lease, clear_pending=False, record_metrics=False)
        envelope = lease.envelope
        next_attempt = envelope.attempt + 1
        scheduled_at = time.time() + max(delay_seconds, 0.0)
        retry_envelope = envelope.with_retry(attempt=next_attempt, scheduled_at=scheduled_at)
        self._store_envelope(retry_envelope)
        if envelope.idempotency_key:
            with self._lock:
                self._pending_keys[retry_envelope.composite_key] = retry_envelope.task_id
        counters.metrics.get_counter(
            "tm_queue_redelivered_total",
            help="Tasks scheduled for retry",
        ).inc(labels={"flow": envelope.flow_id})
        counters.metrics.get_counter(
            "tm_retries_total",
            help="Retry attempts issued",
        ).inc(labels={"flow": envelope.flow_id})
        return retry_envelope

    def record_dead_letter(
        self,
        lease: ManagedLease,
        *,
        error: Mapping[str, Any],
        reason: str,
    ) -> None:
        self.ack(lease, record_metrics=False)
        envelope = lease.envelope
        if self._dead_letters is not None:
            try:
                self._dead_letters.append(
                    flow_id=envelope.flow_id,
                    task=envelope.to_dict(),
                    error={**dict(error), "reason": reason},
                    attempt=envelope.attempt + 1,
                )
            except Exception:  # pragma: no cover - DLQ failures shouldn't crash
                LOGGER.exception("failed to append task %s to DLQ", envelope.task_id)
        counters.metrics.get_counter(
            "tm_dlq_total",
            help="Tasks routed to the DLQ",
        ).inc(labels={"flow": envelope.flow_id, "reason": reason})

    def retry_policy(self) -> Optional[Any]:
        return self._retry_policy

    def get_cached_result(self, envelope: TaskEnvelope) -> Optional[IdempotencyResult]:
        return self._idempotency.get(envelope.composite_key)

    def handle_failure(
        self,
        lease: ManagedLease,
        *,
        error: Mapping[str, Any],
    ) -> Any:
        policy = self._retry_policy
        attempt = lease.envelope.attempt + 1
        if policy is None:
            self.record_dead_letter(lease, error=error, reason="no_policy")
            return None
        decision = policy.decide(lease.envelope.flow_id, attempt, error)
        if decision.action == "retry":
            self.record_retry(lease, delay_seconds=decision.delay_seconds)
        else:
            reason = decision.reason or "max_attempts"
            self.record_dead_letter(lease, error=error, reason=reason)
        return decision

    # ------------------------------------------------------------------
    def _clear_pending(self, envelope: TaskEnvelope) -> None:
        idem_key = envelope.idempotency_key
        if not idem_key:
            return
        composite = envelope.composite_key
        with self._lock:
            existing = self._pending_keys.get(composite)
            if existing == envelope.task_id:
                self._pending_keys.pop(composite, None)

    def _store_envelope(self, envelope: TaskEnvelope) -> None:
        payload = envelope.to_dict()
        offset = self._queue.put(payload)
        with self._lock:
            self._task_offsets[envelope.task_id] = offset
            if envelope.idempotency_key:
                self._pending_keys.setdefault(envelope.composite_key, envelope.task_id)
        self._record_queue_metrics()

    def _record_queue_metrics(self) -> None:
        depth = self._queue.pending_count()
        counters.metrics.get_gauge(
            "tm_queue_depth",
            help="Current tasks persisted in the work queue",
        ).set(depth)
        oldest = self._queue.oldest_available_at()
        lag = 0.0
        if oldest is not None:
            lag = max(0.0, time.monotonic() - oldest)
        counters.metrics.get_gauge(
            "tm_queue_lag_seconds",
            help="Age of the oldest ready task",
        ).set(lag)
        with self._lock:
            inflight = self._inflight
        counters.metrics.get_gauge(
            "tm_queue_inflight",
            help="Tasks currently leased",
        ).set(inflight)


__all__ = ["TaskQueueManager", "ManagedLease", "EnqueueOutcome"]
