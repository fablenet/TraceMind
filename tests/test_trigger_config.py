from __future__ import annotations

import textwrap

import pytest

from tm.triggers.config import (
    TriggerConfigError,
    load_trigger_config_text,
    CronTriggerConfig,
    WebhookTriggerConfig,
    FileSystemTriggerConfig,
)


def test_load_trigger_config_basic() -> None:
    config_text = textwrap.dedent("""
        triggers:
          - id: hourly
            kind: cron
            cron: "*/5 * * * *"
            flow_id: flows/hello.yaml
            input:
              greeting: hello

          - id: hook
            kind: webhook
            route: "/hooks/demo"
            bind_host: 0.0.0.0
            bind_port: 9000
            flow_id: flows/demo.yaml
            secret: abc
            allow_cleartext: false

          - id: dropbox
            kind: filesystem
            path: ./dropbox
            pattern: "*.yaml"
            recursive: true
            interval_seconds: 10
            flow_id: flows/import.yaml
        """)

    cfg = load_trigger_config_text(config_text)
    assert len(cfg.cron) == 1
    assert isinstance(cfg.cron[0], CronTriggerConfig)
    assert cfg.cron[0].cron == "*/5 * * * *"
    assert cfg.cron[0].timezone == "local"

    assert len(cfg.webhook) == 1
    webhook = cfg.webhook[0]
    assert isinstance(webhook, WebhookTriggerConfig)
    assert webhook.route == "/hooks/demo"
    assert webhook.bind_port == 9000
    assert webhook.allow_cleartext is False

    assert len(cfg.filesystem) == 1
    fs = cfg.filesystem[0]
    assert isinstance(fs, FileSystemTriggerConfig)
    assert fs.interval_seconds == 10
    assert fs.recursive is True


def test_duplicate_id_rejected() -> None:
    text = textwrap.dedent("""
        triggers:
          - id: a
            kind: cron
            cron: "0 * * * *"
            flow_id: flows/a.yaml
          - id: a
            kind: webhook
            route: "/a"
            flow_id: flows/a.yaml
        """)
    with pytest.raises(TriggerConfigError):
        load_trigger_config_text(text)


def test_missing_fields_raise() -> None:
    text = textwrap.dedent("""
        triggers:
          - id: fs
            kind: filesystem
            flow_id: flows/fs.yaml
        """)
    with pytest.raises(TriggerConfigError):
        load_trigger_config_text(text)
