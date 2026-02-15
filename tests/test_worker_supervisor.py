from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tm.runtime.idempotency import IdempotencyStore
from tm.runtime.queue import FileWorkQueue
from tm.runtime.queue.manager import TaskQueueManager
from tm.runtime.workers import TaskWorkerSupervisor, WorkerOptions
from tm.obs import counters
from tm.obs.counters import _reset_for_tests


def _gauge_value(name: str) -> float:
    samples = counters.metrics.snapshot()["gauges"].get(name, [])
    for label_items, value in samples:
        if not label_items:
            return float(value)
    return 0.0


@pytest.mark.parametrize("worker_count", [1])
def test_worker_supervisor_processes_tasks(tmp_path: Path, worker_count: int) -> None:
    queue_dir = tmp_path / "queue"
    idem_dir = tmp_path / "idem"
    dlq_dir = tmp_path / "dlq"
    queue_dir.mkdir()
    idem_dir.mkdir()
    dlq_dir.mkdir()
    config_path = tmp_path / "trace_config.toml"
    config_path.write_text("""
[retries.default]
max_attempts = 3
base_ms = 20
factor = 2.0
jitter_ms = 0
""".strip())

    # Seed tasks into the queue
    queue = FileWorkQueue(str(queue_dir))
    store = IdempotencyStore(dir_path=str(idem_dir))
    manager = TaskQueueManager(queue, store)
    for idx in range(5):
        headers = {"idempotency_key": "dup"} if idx == 0 else None
        outcome = manager.enqueue(flow_id="demo.flow", input={"idx": idx}, headers=headers)
        assert outcome.queued is True
    queue.flush()
    queue.close()

    result_file = tmp_path / "results.log"
    os.environ["TRACE_MIND_WORKER_RESULT_FILE"] = str(result_file)

    opts = WorkerOptions(
        worker_count=worker_count,
        queue_backend="file",
        queue_dir=str(queue_dir),
        idempotency_dir=str(idem_dir),
        dlq_dir=str(dlq_dir),
        runtime_spec="tests.worker_runtime_stub:build_runtime",
        lease_ms=100,
        batch_size=1,
        poll_interval=0.05,
        heartbeat_interval=0.2,
        heartbeat_timeout=1.0,
        result_ttl=60.0,
        config_path=str(config_path),
    )

    supervisor = TaskWorkerSupervisor(opts)
    # reset metrics
    _reset_for_tests()
    supervisor.start()
    try:
        deadline = time.time() + 8.0
        processed = []
        while time.time() < deadline:
            if result_file.exists():
                processed = [
                    json.loads(line) for line in result_file.read_text(encoding="utf-8").strip().splitlines() if line
                ]
                if len(processed) >= 5:
                    break
            time.sleep(0.1)
        assert len(processed) >= 5
        status = supervisor.status()
        assert status
        for state in status.values():
            assert state["alive"] is True
    finally:
        supervisor.drain(grace_period=1.0)
        supervisor.stop()

    # verify idempotency cache now serves duplicates without enqueueing
    queue2 = FileWorkQueue(str(queue_dir))
    store2 = IdempotencyStore(dir_path=str(idem_dir))
    manager2 = TaskQueueManager(queue2, store2)
    duplicate = manager2.enqueue(
        flow_id="demo.flow",
        input={"idx": 0},
        headers={"idempotency_key": "dup"},
    )
    assert duplicate.queued is False
    assert duplicate.cached_result is not None
    queue2.close()
    os.environ.pop("TRACE_MIND_WORKER_RESULT_FILE", None)
