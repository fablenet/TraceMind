from __future__ import annotations

import json
from pathlib import Path

import pytest

from tm.dsl import CompileError, compile_paths, parse_pdl_document, parse_wdl_document
from tm.dsl.compiler_flow import compile_workflow
from tm.dsl.compiler_policy import compile_policy

WDL_SAMPLE = """
version: dsl/v0
workflow: plant-monitor
triggers:
  cron:
    schedule: "* * * * *"
inputs:
  endpoint: string
  nodes: list<string>
steps:
  - read(opcua.read):
      endpoint: $input.endpoint
      node_ids: $input.nodes
  - decide(policy.apply):
      values: $step.read.values
  - when $step.decide.action in ["WRITE_BACK","SHUTDOWN"]:
      write(opcua.write):
        endpoint: $input.endpoint
        node_id: $step.decide.target_node
        value: $step.decide.value
outputs:
  decision: $step.decide
"""


PDL_SAMPLE = """
version: pdl/v0
arms:
  default:
    threshold: 75.0
    action_on_violation: WRITE_BACK
    target_node: ns=2;i=5001
epsilon: 0.1
evaluate:
  temp := coalesce(values["ns=2;i=2"], first_numeric(values))
  if temp >= arms.active.threshold:
    choose:
      exploit: action = arms.active.action_on_violation
      explore: random(["NONE","WRITE_BACK","SHUTDOWN"]) with p=epsilon
  else:
    action = "NONE"
emit:
  action: action
  target_node: arms.active.target_node
  value: 1
  reason: { temp: temp, threshold: arms.active.threshold }
"""


def test_compile_workflow_basic(tmp_path: Path) -> None:
    path = tmp_path / "plant.wdl"
    path.write_text(WDL_SAMPLE.strip(), encoding="utf-8")
    workflow = parse_wdl_document(path.read_text(encoding="utf-8"), filename=str(path))
    compilation = compile_workflow(workflow, source=path)
    flow = compilation.data["flow"]

    assert flow["id"] == "plant-monitor"
    assert flow["entry"] == "read"

    steps = {step["id"]: step for step in flow["steps"]}
    assert "read" in steps and steps["read"]["hooks"]["run"] == "tm.dsl.runtime.call"
    switch_step = next(step for step in flow["steps"] if step["kind"] == "switch")
    assert switch_step["config"]["key_from"] == "$.vars.decide.action"
    emit_step = next(step for step in flow["steps"] if step["hooks"]["run"] == "tm.dsl.runtime.emit_outputs")
    assert emit_step["config"]["outputs"]["decision"] == "$step.decide"


def test_compile_policy_to_json(tmp_path: Path) -> None:
    path = tmp_path / "policy.pdl"
    path.write_text(PDL_SAMPLE.strip(), encoding="utf-8")
    policy = parse_pdl_document(path.read_text(encoding="utf-8"), filename=str(path))
    compilation = compile_policy(policy, source=path, policy_id="test-policy")
    payload = compilation.data["policy"]

    assert payload["id"] == "test-policy"
    assert payload["strategy"] == "pdl/v0"
    params = payload["params"]
    assert "default" in params["arms"]
    assert params["emit"]["action"] == "action"


def test_compile_paths_produces_flow_and_policy(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    src_dir = tmp_path / "dsl"
    src_dir.mkdir()
    (src_dir / "sample.wdl").write_text(WDL_SAMPLE.strip(), encoding="utf-8")
    (src_dir / "sample.pdl").write_text(PDL_SAMPLE.strip(), encoding="utf-8")

    out_dir = tmp_path / "out"
    artifacts = compile_paths([src_dir], out_dir=out_dir)

    kinds = {artifact.kind for artifact in artifacts}
    assert kinds == {"flow", "policy", "trigger"}

    flow_artifact = next(artifact for artifact in artifacts if artifact.kind == "flow")
    policy_artifact = next(artifact for artifact in artifacts if artifact.kind == "policy")
    triggers_artifact = next(artifact for artifact in artifacts if artifact.kind == "trigger")

    assert flow_artifact.output.exists()
    assert policy_artifact.output.exists()
    assert triggers_artifact.output.exists()

    import yaml  # type: ignore

    flow_yaml = yaml.safe_load(flow_artifact.output.read_text(encoding="utf-8"))
    steps = flow_yaml["flow"]["steps"]
    call_step = next(step for step in steps if step["id"] == "decide")
    assert call_step["config"]["policy_ref"] == str(policy_artifact.output)

    triggers_yaml = yaml.safe_load(triggers_artifact.output.read_text(encoding="utf-8"))
    trigger_entries = triggers_yaml["triggers"]
    assert trigger_entries
    first_trigger = trigger_entries[0]
    assert first_trigger["kind"] == "cron"
    assert first_trigger["flow_id"] == flow_yaml["flow"]["id"]


def test_compile_paths_emit_ir_outputs_manifest(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    src_dir = tmp_path / "dsl"
    src_dir.mkdir()
    (src_dir / "sample.wdl").write_text(WDL_SAMPLE.strip(), encoding="utf-8")
    (src_dir / "sample.pdl").write_text(PDL_SAMPLE.strip(), encoding="utf-8")

    out_dir = tmp_path / "out"
    artifacts = compile_paths([src_dir], out_dir=out_dir, force=True, emit_ir=True)

    kinds = {artifact.kind for artifact in artifacts}
    assert {"flow", "policy", "trigger", "ir", "manifest"} <= kinds

    ir_file = out_dir / "flows" / "plant-monitor.ir.json"
    assert ir_file.exists()
    ir_payload = json.loads(ir_file.read_text(encoding="utf-8"))
    assert ir_payload["version"] == "1.0.0"
    assert ir_payload["flow"]["name"] == "plant-monitor"
    assert ir_payload["graph"]["nodes"]

    manifest_path = out_dir / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, list) and manifest
    entry = manifest[0]
    assert entry["ir_path"] == "flows/plant-monitor.ir.json"
    assert "policyRef" in entry


def test_compile_paths_ir_validation_failure(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    src_dir = tmp_path / "dsl"
    src_dir.mkdir()
    (src_dir / "sample.wdl").write_text(WDL_SAMPLE.strip(), encoding="utf-8")
    (src_dir / "sample.pdl").write_text(PDL_SAMPLE.strip(), encoding="utf-8")

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"version": {"const": "0.0.0"}},
        "required": ["version"],
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    out_dir = tmp_path / "out"
    with pytest.raises(CompileError) as excinfo:
        compile_paths(
            [src_dir],
            out_dir=out_dir,
            force=True,
            emit_ir=True,
            ir_schema_path=schema_path,
        )
    assert "IR validation failed" in str(excinfo.value)
