from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, MutableMapping

from tm.runtime.evidence import EvidenceRecorder
from tm.runtime.idempotency import ExecutionIdempotencyGuard

RuntimeContextMapping = Mapping[str, Any]
RuntimeInputs = Mapping[str, Any]
RuntimeOutputs = Mapping[str, Any]


@dataclass
class ExecutionContext:
    """Minimal deterministic execution context for runtime agents."""

    idempotency_guard: ExecutionIdempotencyGuard = field(default_factory=ExecutionIdempotencyGuard)
    _refs: MutableMapping[str, Any] = field(default_factory=dict, init=False)
    _files: MutableMapping[str, Path] = field(default_factory=dict, init=False)
    events: List[Mapping[str, Any]] = field(default_factory=list, init=False)
    audits: List[Mapping[str, Any]] = field(default_factory=list, init=False)
    metrics: List[Mapping[str, Any]] = field(default_factory=list, init=False)
    evidence: EvidenceRecorder = field(default_factory=EvidenceRecorder, init=False)
    metadata: Dict[str, Any] = field(default_factory=dict, init=False)

    def get_ref(self, ref: str) -> Any:
        if ref in self._refs:
            return self._refs[ref]
        path = self._files.get(ref)
        if path is None or not path.exists():
            raise KeyError(f"ref '{ref}' not found")
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def set_ref(self, ref: str, value: Any, file_path: str | Path | None = None) -> None:
        self._refs[ref] = value
        if file_path is not None:
            target = Path(file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"))
            self._files[ref] = target

    def emit_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        entry = {"type": event_type, "payload": dict(payload)}
        self.events.append(entry)
        self.evidence.record("event", entry)

    def record_audit(self, action: str, metadata: Mapping[str, Any]) -> None:
        entry = {"action": action, "metadata": dict(metadata)}
        self.audits.append(entry)
        self.evidence.record("audit", entry)

    def record_metric(self, name: str, value: Any, tags: Mapping[str, Any] | None = None) -> None:
        entry = {"name": name, "value": value, "tags": dict(tags) if tags is not None else {}}
        self.metrics.append(entry)
        self.evidence.record("metric", entry)

    def log(self, message: str, level: str = "info") -> None:
        record = {"level": level, "message": message}
        self.evidence.record("log", record)

    def run_idempotent(self, key: str, func: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
        cached = self.idempotency_guard.lookup(key)
        if cached is not None:
            return cached
        result = func()
        self.idempotency_guard.record(key, result)
        self.evidence.record("idempotency", {"key": key, "result": dict(result)})
        return result
