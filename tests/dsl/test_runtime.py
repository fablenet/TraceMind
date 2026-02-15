from __future__ import annotations

import json
from pathlib import Path

from tm.dsl import parse_pdl_document
from tm.dsl.compiler_policy import compile_policy
from tm.dsl.runtime import call, emit_outputs

PDL_SAMPLE = """
version: pdl/v0
arms:
  default:
    threshold: 70.0
    action_on_violation: WRITE_BACK
    target_node: ns=2;i=1
evaluate:
  temp := first_numeric(values)
  if temp >= arms.active.threshold:
    action = arms.active.action_on_violation
  else:
    action = "NONE"
emit:
  action: action
  target_node: arms.active.target_node
"""


def test_runtime_call_and_emit(tmp_path: Path) -> None:
    policy_path = tmp_path / "sample.pdl"
    policy_path.write_text(PDL_SAMPLE.strip(), encoding="utf-8")
    policy_ir = parse_pdl_document(policy_path.read_text(encoding="utf-8"), filename=str(policy_path))
    policy_compiled = compile_policy(policy_ir, source=policy_path, policy_id="policy-1")
    policy_json = tmp_path / "policy.json"
    policy_json.write_text(json.dumps(policy_compiled.data, indent=2), encoding="utf-8")

    state = {
        "endpoint": "opc.tcp://demo",
        "nodes": ["ns=2;i=1"],
    }

    read_ctx = {
        "step": "read",
        "config": {
            "call": {
                "target": "opcua.read",
                "args": {"endpoint": "$input.endpoint", "node_ids": "$input.nodes"},
            }
        },
    }
    state = call(read_ctx, state)
    assert "steps" in state
    assert "read" in state["steps"]

    decide_ctx = {
        "step": "decide",
        "config": {
            "call": {
                "target": "policy.apply",
                "args": {"values": "$step.read.values"},
            },
            "policy_ref": str(policy_json),
            "policy_id": "policy-1",
        },
    }
    state = call(decide_ctx, state)
    assert state["steps"]["decide"]["action"] in {"NONE", "WRITE_BACK"}

    emit_ctx = {
        "step": "emit",
        "config": {"outputs": {"decision": "$step.decide"}},
    }
    state = emit_outputs(emit_ctx, state)
    assert state["outputs"]["decision"] == state["steps"]["decide"]
