from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path


def _load_cmd_fmt():
    module_path = Path(__file__).resolve().parents[2] / "tm" / "cli" / "fmt.py"
    spec = importlib.util.spec_from_file_location("_tm_cli_fmt_test_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._cmd_fmt


_CMD_FMT = _load_cmd_fmt()


def _fixture(*parts: str) -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "fmt" / "v0.1" / Path(*parts)


def _run_fmt(*, target: Path, check: bool = False, write: bool = False, json_output: bool = False) -> int:
    args = argparse.Namespace(
        target=str(target),
        check=check,
        write=write,
        json=json_output,
        no_validate=True,
    )
    return _CMD_FMT(args)


def test_fmt_idempotent(tmp_path: Path) -> None:
    source = _fixture("unformatted", "unformatted_proposal.json")
    target = tmp_path / "proposal.json"
    shutil.copy2(source, target)

    assert _run_fmt(target=target) == 0
    first_pass = target.read_bytes()

    assert _run_fmt(target=target) == 0
    second_pass = target.read_bytes()
    assert first_pass == second_pass


def test_fmt_sorts_set_like_lists(tmp_path: Path) -> None:
    proposal_path = tmp_path / "proposal.json"
    testsuite_path = tmp_path / "testsuite.json"
    shutil.copy2(_fixture("unformatted", "unformatted_proposal.json"), proposal_path)
    shutil.copy2(_fixture("unformatted", "unformatted_testsuite.json"), testsuite_path)

    assert _run_fmt(target=tmp_path) == 0

    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    testsuite = json.loads(testsuite_path.read_text(encoding="utf-8"))
    assert proposal["metadata"]["trace_links"]["related_intents"] == ["TM-INT-0001", "TM-INT-0003"]
    assert proposal["spec"]["impacted_intents"] == ["TM-INT-0001", "TM-INT-0002"]
    assert testsuite["spec"]["intent_refs"] == ["TM-INT-0001", "TM-INT-0003"]
    assert testsuite["spec"]["tests"][0]["intent_refs"] == ["TM-INT-0001", "TM-INT-0002"]
    assert testsuite["spec"]["tests"][1]["intent_refs"] == ["TM-INT-0002", "TM-INT-0003"]
    assert proposal_path.read_text(encoding="utf-8") == _fixture("valid", "formatted_proposal.json").read_text(
        encoding="utf-8"
    )
    assert testsuite_path.read_text(encoding="utf-8") == _fixture("valid", "formatted_testsuite.json").read_text(
        encoding="utf-8"
    )


def test_fmt_preserves_sequence_lists(tmp_path: Path) -> None:
    target = tmp_path / "testsuite.json"
    shutil.copy2(_fixture("unformatted", "unformatted_testsuite.json"), target)

    assert _run_fmt(target=target) == 0

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [test_case["id"] for test_case in payload["spec"]["tests"]] == ["ts-z", "ts-a"]


def test_fmt_check_mode(capsys, tmp_path: Path) -> None:
    source_dir = _fixture("unformatted")
    target_dir = tmp_path / "unformatted"
    shutil.copytree(source_dir, target_dir)

    code = _run_fmt(target=target_dir, check=True)
    captured = capsys.readouterr()
    assert code == 1
    assert "unformatted_proposal.json: non-canonical" in captured.out
    assert "unformatted_testsuite.json: non-canonical" in captured.out
