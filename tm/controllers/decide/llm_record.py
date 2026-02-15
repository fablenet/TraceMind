from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, MutableMapping

DEFAULT_RECORD_PATH = Path(".tracemind/controller_decide_records.json")


class LlmRecordStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_RECORD_PATH
        self._data: MutableMapping[str, dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, Mapping):
            return
        for key, value in raw.items():
            if isinstance(value, Mapping):
                self._data[str(key)] = dict(value)

    def get(self, key: str) -> Mapping[str, Any] | None:
        record = self._data.get(key)
        if not isinstance(record, Mapping):
            return None
        plan = record.get("plan")
        if not isinstance(plan, Mapping):
            return None
        return deepcopy(plan)

    def set(self, key: str, plan: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {"plan": deepcopy(plan)}
        if metadata is not None:
            entry["metadata"] = dict(metadata)
        self._data[key] = entry
        self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump({key: value for key, value in self._data.items()}, handle, separators=(",", ":"), sort_keys=True)
