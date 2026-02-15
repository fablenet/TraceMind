"""Tiny HTTP driver to produce diverse payloads.

Run server (Hypercorn) first, then invoke::

    python -m tm.examples.pipeline_drive_http --url https://localhost:8443 --insecure
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.request
from typing import Dict

PAYLOADS: list[Dict[str, object]] = [
    {
        "kind": "NFProfile",
        "obj_id": "nf-1",
        "payload": {
            "nfInstanceId": "nf-1",
            "nfType": "NRF",
            "services": [{"name": " nrf-disc ", "state": "UP"}],
            "meta": {"version": 0},
        },
    },
    {
        "kind": "NFProfile",
        "obj_id": "nf-1",
        "payload": {
            "nfInstanceId": "nf-1",
            "nfType": "NRF",
            "services": [{"name": "nrf-disc", "state": "DOWN"}],
            "meta": {"version": 1},
        },
    },
    {
        "kind": "NFProfile",
        "obj_id": "nf-1",
        "payload": {
            "nfInstanceId": "nf-1",
            "nfType": "NRF",
            "status": "ALIVE",
            "services": [{"name": "nrf-disc", "state": "UP"}],
            "meta": {"version": 2},
            "policy": {"forceAlive": True},
        },
    },
]


def _build_context(*, insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def post(url: str, obj: Dict[str, object], insecure: bool) -> None:
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/commands/upsert",
        data=body,
        headers={"content-type": "application/json"},
    )
    ctx = _build_context(insecure=insecure)
    with urllib.request.urlopen(req, context=ctx) as resp:
        payload = resp.read().decode("utf-8")
        print("status:", resp.status, payload)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    for payload in PAYLOADS:
        post(args.url, payload, args.insecure)
        time.sleep(0.05)


if __name__ == "__main__":  # pragma: no cover - example script
    main()
