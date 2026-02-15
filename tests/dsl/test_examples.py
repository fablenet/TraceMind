from __future__ import annotations

from pathlib import Path

import pytest

from tm.dsl import compile_paths

pytest.importorskip("yaml", reason="PyYAML required for DSL compilation")


def _have_networkx() -> bool:
    try:
        import networkx  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _have_networkx(), reason="requires networkx to execute flows")
def test_opcua_example_compiles(tmp_path: Path) -> None:
    example_dir = Path("examples/dsl/opcua")
    artifacts = compile_paths([example_dir], out_dir=tmp_path, force=True)
    flow_artifact = next(artifact for artifact in artifacts if artifact.kind == "flow")
    policy_artifact = next(artifact for artifact in artifacts if artifact.kind == "policy")

    pytest.importorskip("yaml")
    import yaml  # type: ignore

    flow_data = yaml.safe_load(flow_artifact.output.read_text(encoding="utf-8"))
    flow = flow_data.get("flow", {})
    steps = {step["id"]: step for step in flow["steps"]}
    assert "decide" in steps
    decide_step = steps["decide"]
    assert decide_step["config"]["policy_ref"] == str(policy_artifact.output)
    assert flow["entry"] == "read"
