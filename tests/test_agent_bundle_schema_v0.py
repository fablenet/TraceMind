import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

SCHEMA_PATH = Path("tm/artifacts/schemas/v0/agent_bundle.json")


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _minimal_bundle_body() -> dict:
    return {
        "bundle_id": "tm-bundle/small",
        "agents": [
            {
                "agent_id": "tm-agent/runner:0.1",
                "name": "runner",
                "version": "0.1",
                "runtime": {"kind": "tm-shell", "config": {"image": "runner:v0"}},
                "contract": {
                    "inputs": [
                        {
                            "ref": "artifact:config",
                            "kind": "artifact",
                            "schema": "schemas/config-schema.json",
                            "required": True,
                            "mode": "read",
                        }
                    ],
                    "outputs": [
                        {
                            "ref": "state:workload",
                            "kind": "resource",
                            "schema": {"type": "object"},
                            "required": False,
                            "mode": "write",
                        }
                    ],
                    "effects": [
                        {
                            "name": "configure",
                            "kind": "resource",
                            "target": "state:workload",
                            "idempotency": {"type": "keyed", "key_fields": ["artifact_id"]},
                            "evidence": {"type": "hash", "path": "/state/config.hash"},
                        }
                    ],
                },
                "config_schema": {"type": "object"},
                "evidence_outputs": [{"name": "config_hash", "description": "hash of the config artifact"}],
                "role": "initializer",
            }
        ],
        "plan": [
            {
                "step": "init",
                "agent_id": "tm-agent/runner:0.1",
                "phase": "init",
                "inputs": ["artifact:config"],
                "outputs": ["state:workload"],
            }
        ],
        "meta": {"preconditions": ["artifact:config"]},
    }


def test_agent_bundle_schema_accepts_minimal_body() -> None:
    schema = _load_schema()
    validate(_minimal_bundle_body(), schema)


def test_agent_bundle_schema_rejects_missing_plan() -> None:
    schema = _load_schema()
    payload = _minimal_bundle_body()
    payload.pop("plan")
    with pytest.raises(ValidationError):
        validate(payload, schema)
