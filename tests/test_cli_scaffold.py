from __future__ import annotations

from pathlib import Path

from tm.scaffold import create_flow, create_policy, init_project


def test_init_project_creates_yaml_layout(tmp_path: Path):
    root = init_project("demo", tmp_path)
    assert (root / "trace-mind.toml").exists()
    assert (root / "flows" / "hello.yaml").exists()
    assert (root / "policies" / "default.yaml").exists()
    assert (root / "steps" / "impl.py").exists()
    assert (root / "services" / "hello.py").exists()
    readme = (root / "README.md").read_text("utf-8")
    assert "tm run recipe flows/hello.yaml" in readme


def test_create_flow_variants_appends_stubs(tmp_path: Path):
    root = init_project("demo", tmp_path)
    flow_file = create_flow("greet", project_root=root, switch=True)
    assert flow_file.suffix == ".yaml"
    content = flow_file.read_text("utf-8")
    assert "kind: switch" in content
    impl = (root / "steps" / "impl.py").read_text("utf-8")
    assert "def greet_route" in impl


def test_create_policy_yaml(tmp_path: Path):
    root = init_project("demo", tmp_path)
    policy_path = create_policy("traffic", project_root=root, strategy="ucb", mcp_endpoint="mcp:policy")
    data = policy_path.read_text("utf-8")
    assert "strategy" in data and "ucb" in data
    config = (root / "trace-mind.toml").read_text("utf-8")
    assert 'policy_endpoint = "mcp:policy"' in config
