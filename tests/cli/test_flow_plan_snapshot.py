from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SNAPSHOT_NAME = "flow_plan_basic.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _snapshot_path(name: str) -> Path:
    return _repo_root() / "tests" / "snapshots" / name


def _normalize(payload: dict) -> dict:
    flows = payload.get("flows", [])
    normalized = []
    root = _repo_root()
    for entry in flows:
        rel_path = str(Path(entry["path"]).resolve().relative_to(root))
        normalized.append(
            {
                "path": rel_path.replace("\\", "/"),
                "stats": entry.get("stats"),
                "layers": entry.get("layers"),
                "issues": entry.get("issues", []),
            }
        )
    normalized.sort(key=lambda item: item["path"])
    return {"flows": normalized}


def test_flow_plan_json_snapshot():
    fixtures = [
        _repo_root() / "fixtures" / "flows" / "tiny_ok.yml",
        _repo_root() / "fixtures" / "flows" / "wide_deep.yml",
    ]
    cmd = [
        sys.executable,
        "-m",
        "tm",
        "flow",
        "plan",
        *(str(path) for path in fixtures),
        "--json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    actual_payload = json.loads(result.stdout)
    actual = _normalize(actual_payload)

    snapshot_path = _snapshot_path(SNAPSHOT_NAME)
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert actual == expected
