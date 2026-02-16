from __future__ import annotations

import json
from pathlib import Path

import pytest

from tm import cli as tm_cli
from tm.cli import _enqueue_task
from tm.runtime.queue.file import FileWorkQueue


def test_enqueue_task_writes_file_queue(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    idempotency_dir = tmp_path / "idem"

    outcome = _enqueue_task(
        flow_id="demo-flow",
        payload={"input": {"foo": "bar"}},
        headers={"custom": "hdr"},
        trace={"source": "test"},
        queue_backend="file",
        queue_dir=str(queue_dir),
        idempotency_dir=str(idempotency_dir),
        idempotency_key="idem-123",
    )

    assert outcome.queued is True
    assert outcome.envelope is not None
    assert outcome.envelope.flow_id == "demo-flow"

    queue = FileWorkQueue(str(queue_dir))
    try:
        leases = queue.lease(1, 1000)
        assert leases, "expected enqueued task to be available"
        lease = leases[0]
        assert lease.task["flow_id"] == "demo-flow"
        assert lease.task["headers"]["idempotency_key"] == "idem-123"
    finally:
        queue.close()


def test_run_detached_enqueues_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TM_ENABLE_DAEMON", "1")
    queue_dir = tmp_path / "queue"
    idempotency_dir = tmp_path / "idem"
    recipe_path = tmp_path / "flow.json"
    recipe_path.write_text(
        json.dumps(
            {
                "flow": {"id": "detached-flow"},
                "steps": [],
            }
        ),
        encoding="utf-8",
    )

    rc = tm_cli.main(
        [
            "run",
            "recipe",
            str(recipe_path),
            "--detached",
            "--queue",
            "file",
            "--queue-dir",
            str(queue_dir),
            "--idempotency-dir",
            str(idempotency_dir),
            "-i",
            '{"payload": 42}',
        ]
    )
    assert rc == 0

    captured = capsys.readouterr()
    assert "enqueued task" in captured.out

    queue = FileWorkQueue(str(queue_dir))
    try:
        leases = queue.lease(1, 1000)
        assert leases, "expected detached run to enqueue a task"
        lease = leases[0]
        assert lease.task["flow_id"] == "detached-flow"
        assert lease.task["input"]["payload"] == 42
    finally:
        queue.close()
