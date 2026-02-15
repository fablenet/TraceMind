"""Utilities to rebuild metric windows from binlog exports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from tm.storage.binlog import BinaryLogReader

LabelKey = Tuple[Tuple[str, str], ...]


def _to_timestamp(value: datetime | float | int) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


def load_window(dir_path: str, since: datetime | float | int, until: datetime | float | int) -> List[Dict[str, object]]:
    since_ts = _to_timestamp(since)
    until_ts = _to_timestamp(until)
    reader = BinaryLogReader(dir_path)

    counters: Dict[Tuple[str, LabelKey], float] = {}
    gauges: Dict[Tuple[str, LabelKey], float] = {}
    histograms: Dict[Tuple[str, LabelKey], Dict[str, float]] = {}

    for etype, payload in reader.scan():
        if etype != "MetricSample":
            continue
        data = json.loads(payload.decode("utf-8"))
        ts = float(data.get("ts", 0.0))
        if ts < since_ts or ts > until_ts:
            continue
        name = data.get("name")
        labels = data.get("labels", {})
        label_key: LabelKey = tuple(sorted((str(k), str(v)) for k, v in labels.items() if k != "le"))
        typ = data.get("type")
        value = float(data.get("value", 0.0))

        if typ == "counter":
            counters[(name, label_key)] = counters.get((name, label_key), 0.0) + value
        elif typ == "gauge":
            gauges[(name, label_key)] = value
        elif typ == "hist":
            bucket = str(labels.get("le", "inf"))
            key = (name, label_key)
            hist = histograms.setdefault(key, {})
            hist[bucket] = hist.get(bucket, 0.0) + value

    entries: List[Dict[str, object]] = []
    for (name, label_key), total in counters.items():
        entries.append(
            {
                "type": "counter",
                "name": name,
                "labels": dict(label_key),
                "value": total,
            }
        )
    for (name, label_key), value in gauges.items():
        entries.append(
            {
                "type": "gauge",
                "name": name,
                "labels": dict(label_key),
                "value": value,
            }
        )
    for (name, label_key), buckets in histograms.items():
        for bucket, total in buckets.items():
            labels = dict(label_key)
            labels["le"] = bucket
            entries.append(
                {
                    "type": "hist",
                    "name": name,
                    "labels": labels,
                    "value": total,
                }
            )

    return entries
