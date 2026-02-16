from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tm.policy import canonical_json_bytes


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "replay" / "v0.1"


def _run_replay(*, trace: Path, policy: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tm",
            "replay",
            "run",
            "--trace",
            str(trace),
            "--policy",
            str(policy),
            "--out",
            str(out),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )


def test_replay_is_byte_deterministic(tmp_path: Path) -> None:
    fixtures = _fixture_dir()
    trace = fixtures / "trace.jsonl"
    policy = fixtures / "policy.json"
    out_a = tmp_path / "replay_a.jsonl"
    out_b = tmp_path / "replay_b.jsonl"

    result_a = _run_replay(trace=trace, policy=policy, out=out_a)
    result_b = _run_replay(trace=trace, policy=policy, out=out_b)
    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    assert out_a.read_bytes() == out_b.read_bytes()


def test_replay_rows_are_canonical_json_lines(tmp_path: Path) -> None:
    fixtures = _fixture_dir()
    out_path = tmp_path / "replay.jsonl"
    result = _run_replay(trace=fixtures / "trace.jsonl", policy=fixtures / "policy.json", out=out_path)
    assert result.returncode == 0, result.stderr

    lines = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    for index, line in enumerate(lines):
        payload = json.loads(line)
        assert payload["index"] == index
        assert line.encode("utf-8") == canonical_json_bytes(payload)
