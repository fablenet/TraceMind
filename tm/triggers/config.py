from __future__ import annotations

import json
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

try:  # optional dependency
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:  # pragma: no cover - optional path
    yaml = None


class TriggerConfigError(Exception):
    """Raised when trigger configuration fails validation."""


@dataclass(frozen=True)
class TriggerBaseConfig:
    id: str
    kind: str
    flow_id: str
    input_template: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class CronTriggerConfig(TriggerBaseConfig):
    cron: str = "* * * * *"
    timezone: str = "local"


@dataclass(frozen=True)
class WebhookTriggerConfig(TriggerBaseConfig):
    route: str = "/"
    method: str = "POST"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8081
    secret: str | None = None
    allow_cleartext: bool = True
    tls_certfile: str | None = None
    tls_keyfile: str | None = None


@dataclass(frozen=True)
class FileSystemTriggerConfig(TriggerBaseConfig):
    path: str = "."
    pattern: str = "*"
    recursive: bool = False
    interval_seconds: float = 5.0


@dataclass(frozen=True)
class TriggerConfigSet:
    source: str
    cron: tuple[CronTriggerConfig, ...]
    webhook: tuple[WebhookTriggerConfig, ...]
    filesystem: tuple[FileSystemTriggerConfig, ...]

    def all(self) -> Iterable[TriggerBaseConfig]:
        yield from self.cron
        yield from self.webhook
        yield from self.filesystem


def load_trigger_config(path: str) -> TriggerConfigSet:
    if not os.path.exists(path):
        raise TriggerConfigError(f"trigger config not found at {path}")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return load_trigger_config_text(text, source=path)


def load_trigger_config_text(text: str, *, source: str = "<memory>") -> TriggerConfigSet:
    data = _parse_document(text, source)
    if not isinstance(data, Mapping):
        raise TriggerConfigError("top-level triggers config must be an object")
    raw_triggers = data.get("triggers")
    if raw_triggers is None:
        return TriggerConfigSet(source=source, cron=(), webhook=(), filesystem=())
    if not isinstance(raw_triggers, Sequence):
        raise TriggerConfigError("'triggers' must be a list")

    cron_configs: list[CronTriggerConfig] = []
    webhook_configs: list[WebhookTriggerConfig] = []
    fs_configs: list[FileSystemTriggerConfig] = []

    seen_ids: set[str] = set()

    for idx, entry in enumerate(raw_triggers):
        if not isinstance(entry, Mapping):
            raise TriggerConfigError(f"trigger entry #{idx} must be a mapping")
        trigger_id = _require_str(entry, "id")
        if not trigger_id:
            raise TriggerConfigError("trigger id must be non-empty")
        if trigger_id in seen_ids:
            raise TriggerConfigError(f"duplicate trigger id '{trigger_id}'")
        seen_ids.add(trigger_id)

        kind = _require_str(entry, "kind").lower()
        base_kwargs = _extract_base_kwargs(entry)

        if kind == "cron":
            cron_configs.append(_build_cron(trigger_id, base_kwargs, entry))
        elif kind == "webhook":
            webhook_configs.append(_build_webhook(trigger_id, base_kwargs, entry))
        elif kind in {"fs", "filesystem"}:
            fs_configs.append(_build_fs(trigger_id, base_kwargs, entry))
        else:
            raise TriggerConfigError(f"unsupported trigger kind '{kind}'")

    return TriggerConfigSet(
        source=source,
        cron=tuple(cron_configs),
        webhook=tuple(webhook_configs),
        filesystem=tuple(fs_configs),
    )


def _build_cron(trigger_id: str, base: MutableMapping[str, Any], entry: Mapping[str, Any]) -> CronTriggerConfig:
    cron_expr = _require_str(entry, "cron")
    timezone = _optional_str(entry, "timezone") or "local"
    return CronTriggerConfig(**base, cron=cron_expr, timezone=timezone)


def _build_webhook(trigger_id: str, base: MutableMapping[str, Any], entry: Mapping[str, Any]) -> WebhookTriggerConfig:
    route = _require_str(entry, "route")
    method = (_optional_str(entry, "method") or "POST").upper()
    bind_host = _optional_str(entry, "bind_host") or "127.0.0.1"
    bind_port = _optional_int(entry, "bind_port", default=8081)
    secret = _optional_str(entry, "secret")
    allow_cleartext = bool(entry.get("allow_cleartext", True))
    tls_block = entry.get("tls")
    certfile = keyfile = None
    if isinstance(tls_block, Mapping):
        certfile = _optional_str(tls_block, "certfile")
        keyfile = _optional_str(tls_block, "keyfile")
    return WebhookTriggerConfig(
        **base,
        route=route,
        method=method,
        bind_host=bind_host,
        bind_port=bind_port,
        secret=secret,
        allow_cleartext=allow_cleartext,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
    )


def _build_fs(trigger_id: str, base: MutableMapping[str, Any], entry: Mapping[str, Any]) -> FileSystemTriggerConfig:
    path = _require_str(entry, "path")
    pattern = _optional_str(entry, "pattern") or "*"
    recursive = bool(entry.get("recursive", False))
    interval_raw = entry.get("interval_seconds", entry.get("interval"))
    interval = _coerce_float(interval_raw, default=5.0)
    if interval < 0.5:
        raise TriggerConfigError(f"filesystem trigger '{trigger_id}' interval must be >= 0.5s")
    return FileSystemTriggerConfig(
        **base,
        path=path,
        pattern=pattern,
        recursive=recursive,
        interval_seconds=interval,
    )


def _extract_base_kwargs(entry: Mapping[str, Any]) -> MutableMapping[str, Any]:
    flow_id = _require_str(entry, "flow_id")
    input_template = entry.get("input") or entry.get("input_template") or {}
    if not isinstance(input_template, Mapping):
        raise TriggerConfigError("trigger 'input' must be an object")
    idempotency_key = _optional_str(entry, "idempotency_key")
    return {
        "id": _require_str(entry, "id"),
        "kind": _require_str(entry, "kind").lower(),
        "flow_id": flow_id,
        "input_template": dict(input_template),
        "idempotency_key": idempotency_key,
    }


def _parse_document(text: str, source: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped[0] in "[{":
        return json.loads(stripped)
    if yaml is None:
        raise TriggerConfigError(f"cannot parse {source}: install PyYAML or use JSON format")
    try:
        return yaml.safe_load(text)
    except Exception as exc:  # pragma: no cover - YAML error path
        raise TriggerConfigError(f"failed to parse trigger config: {exc}") from exc


def _require_str(obj: Mapping[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise TriggerConfigError(f"trigger field '{key}' must be a string")
    return value.strip()


def _optional_str(obj: Mapping[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TriggerConfigError(f"trigger field '{key}' must be a string")
    return value.strip()


def _optional_int(obj: Mapping[str, Any], key: str, *, default: int) -> int:
    value = obj.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        raise TriggerConfigError(f"trigger field '{key}' must be an integer")
    if not isinstance(value, int):
        raise TriggerConfigError(f"trigger field '{key}' must be an integer")
    return value


def _coerce_float(value: Any, *, default: float) -> float:
    if value is None:
        return default
    try:
        val = float(value)
    except (TypeError, ValueError):
        raise TriggerConfigError("interval must be a number") from None
    return val


def generate_sample_config() -> str:
    return textwrap.dedent("""
        # TraceMind trigger configuration
        version: 1
        triggers:
          - id: hourly-report
            kind: cron
            cron: "0 * * * *"
            timezone: local
            flow_id: flows/hourly.yaml
            input:
              mode: summary

          - id: orders-webhook
            kind: webhook
            route: "/hooks/orders"
            method: POST
            bind_host: 127.0.0.1
            bind_port: 8081
            allow_cleartext: true
            secret: change-me
            flow_id: flows/order-intake.yaml
            input:
              body: "{{ body }}"

          - id: ingest-drop-folder
            kind: filesystem
            path: ./incoming
            pattern: "*.json"
            recursive: false
            interval_seconds: 5
            flow_id: flows/file-import.yaml
            input:
              file_path: "{{ path }}"
        """).strip() + "\n"
