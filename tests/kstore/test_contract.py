import os

import pytest

from tm.kstore.api import open_kstore


@pytest.mark.parametrize(
    "url",
    [
        "jsonl://./.tmp/kstore_contract.jsonl",
        pytest.param(
            "sqlite://./.tmp/kstore_contract.db",
            marks=pytest.mark.skipif(
                os.getenv("NO_SQLITE") == "1",
                reason="sqlite disabled",
            ),
        ),
    ],
)
def test_put_get_scan_delete(tmp_path, url):
    url = url.replace("./.tmp", str(tmp_path))
    ks = open_kstore(url)
    try:
        ks.put("metrics:run:1", {"ok": True, "latency_ms": 12})
        fetched = ks.get("metrics:run:1")
        assert fetched["ok"] is True
        assert fetched["latency_ms"] == 12

        rows = list(ks.scan("metrics:"))
        assert len(rows) == 1
        key, value = rows[0]
        assert key == "metrics:run:1"
        assert value["ok"] is True

        assert ks.delete("metrics:run:1") is True
        assert ks.get("metrics:run:1") is None
    finally:
        ks.close()
