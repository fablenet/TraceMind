from __future__ import annotations

import asyncio
import textwrap
import json
from contextlib import suppress
import socket

import pytest

from tm.runtime.queue.file import FileWorkQueue
from tm.triggers.config import load_trigger_config_text
from tm.triggers.manager import TriggerEvent, TriggerManager
from tm.triggers.runner import TriggerRuntime

pytestmark = pytest.mark.asyncio


async def test_cron_trigger_emits_events() -> None:
    cfg_text = textwrap.dedent("""
        triggers:
          - id: short
            kind: cron
            cron: "@every 0.3"
            flow_id: flows/demo.yaml
            input:
              greeting: "{{ trigger_id }}"
        """)
    config = load_trigger_config_text(cfg_text)
    events: list[TriggerEvent] = []
    stop_event = asyncio.Event()

    async def handler(event: TriggerEvent) -> None:
        events.append(event)
        if len(events) >= 2:
            stop_event.set()

    manager = TriggerManager(config, handler)
    task = asyncio.create_task(manager.run(stop_event))
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=2.0)
    finally:
        stop_event.set()
        await task

    assert len(events) >= 2
    for event in events:
        assert event.flow_id == "flows/demo.yaml"
        assert event.payload["greeting"] == "short"
        assert event.payload["_trigger"]["id"] == "short"


async def test_trigger_runtime_webhook(tmp_path) -> None:
    port = _free_port()
    cfg_text = textwrap.dedent(f"""
        triggers:
          - id: hook
            kind: webhook
            route: "/hooks/demo"
            method: POST
            bind_host: 127.0.0.1
            bind_port: {port}
            secret: s3cr3t
            allow_cleartext: true
            flow_id: flows/webhook.yaml
            input:
              body: "{{ body }}"
              method: "{{ method }}"
        """)
    config = load_trigger_config_text(cfg_text)
    queue_dir = tmp_path / "queue"
    idem_dir = tmp_path / "idem"
    dlq_dir = tmp_path / "dlq"
    runtime = TriggerRuntime(
        config,
        queue_dir=str(queue_dir),
        idempotency_dir=str(idem_dir),
        dlq_dir=str(dlq_dir),
    )

    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = '{"message":"hi"}'
        request = (
            "POST /hooks/demo HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "X-TraceMind-Secret: s3cr3t\r\n"
            "\r\n"
            f"{body}"
        )
        writer.write(request.encode("utf-8"))
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.4)
        runtime.request_stop()
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    queue = FileWorkQueue(str(queue_dir))
    try:
        assert queue.pending_count() >= 1
    finally:
        queue.close()


async def test_trigger_runtime_filesystem(tmp_path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    cfg_text = json.dumps(
        {
            "triggers": [
                {
                    "id": "fs",
                    "kind": "filesystem",
                    "path": str(watch_dir),
                    "pattern": "*.json",
                    "recursive": False,
                    "interval_seconds": 0.5,
                    "flow_id": "flows/fs.yaml",
                    "input": {"file": "{{ path }}"},
                }
            ]
        }
    )
    config = load_trigger_config_text(cfg_text)
    queue_dir = tmp_path / "queue2"
    idem_dir = tmp_path / "idem2"
    dlq_dir = tmp_path / "dlq2"
    runtime = TriggerRuntime(
        config,
        queue_dir=str(queue_dir),
        idempotency_dir=str(idem_dir),
        dlq_dir=str(dlq_dir),
    )

    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.sleep(0.6)
        (watch_dir / "sample.json").write_text("{}", encoding="utf-8")
        await asyncio.sleep(0.7)
        runtime.request_stop()
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    queue = FileWorkQueue(str(queue_dir))
    try:
        assert queue.pending_count() >= 1
    finally:
        queue.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
