from __future__ import annotations

from pathlib import Path

import pytest

from tm.agents import builtins  # noqa: F401 registers builtin runtime agents
from tm.artifacts import load_yaml_artifact
from tm.artifacts.models import ArtifactStatus
from tm.artifacts.verify import verify
from tm.runtime.context import ExecutionContext
from tm.runtime.executor import AgentBundleExecutor, AgentBundleExecutorError

EXAMPLE_PATH = Path("specs/examples/agent_bundle_v0/agent_bundle_demo.yaml")


def _load_example() -> tuple:
    return load_yaml_artifact(EXAMPLE_PATH)


def test_golden_agent_bundle_verifies() -> None:
    artifact = _load_example()
    candidate, report = verify(artifact)
    assert candidate is not None
    assert report.success
    assert candidate.envelope.status == ArtifactStatus.ACCEPTED
    assert report.details["body_hash"] == candidate.envelope.body_hash
    assert candidate.envelope.meta.get("determinism") is True


def test_executor_halts_when_shell_policy_denied() -> None:
    artifact = _load_example()
    candidate, _ = verify(artifact)
    assert candidate is not None
    context = ExecutionContext()
    context.set_ref("artifact:http_request", {"method": "GET", "url": "https://example.com/api/demo"})
    executor = AgentBundleExecutor()
    with pytest.raises(AgentBundleExecutorError) as excinfo:
        executor.execute(candidate, context=context)
    assert "policy guard denied" in str(excinfo.value)
    assert context.get_ref("artifact:http_response")["status"] == 200
    assert context.get_ref("state:noop.out") == {"method": "GET", "url": "https://example.com/api/demo"}
    guard_records = [record for record in context.evidence.records() if record.kind == "policy_guard"]
    assert guard_records
    assert any(not record.payload["allowed"] for record in guard_records)
