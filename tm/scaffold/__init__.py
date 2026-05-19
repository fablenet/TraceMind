from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Dict, Mapping, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

PROJECT_CONFIG: Dict[str, Dict[str, object]] = {
    "runtime": {
        "flows_dir": "flows",
        "services_dir": "services",
        "max_concurrency": 100,
        "queue_capacity": 300,
    },
    "observability": {
        "exporters": ["file"],
    },
    "ai": {
        "tuner": "epsilon",
        "policy_endpoint": "",
    },
}


_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def _slug(value: str) -> str:
    """Convert human input into a filesystem-friendly slug."""

    normalized = value.strip().lower().replace(" ", "-")
    normalized = _SLUG_RE.sub("-", normalized)
    normalized = normalized.strip("-")
    return normalized or "item"


@dataclass
class ProjectContext:
    root: Path
    config: Dict[str, Dict[str, object]]

    @property
    def flows_dir(self) -> Path:
        return self.root / str(self.config["runtime"]["flows_dir"])

    @property
    def policies_dir(self) -> Path:
        return self.root / "policies"

    @property
    def services_dir(self) -> Path:
        return self.root / str(self.config["runtime"]["services_dir"])

    @property
    def steps_dir(self) -> Path:
        return self.root / "steps"

    def write_config(self) -> None:
        content = _render_config(self.config)
        (self.root / "trace-mind.toml").write_text(content, encoding="utf-8")


def init_project(
    project_name: str,
    destination: Path | str,
    *,
    with_prom: bool = False,
    with_retrospect: bool = False,
    force: bool = False,
) -> Path:
    dest = Path(destination).resolve()
    project_root = dest / project_name
    if project_root.exists() and any(project_root.iterdir()) and not force:
        raise FileExistsError(
            f"Destination '{project_root}' already exists; use --force to overwrite scaffolding files"
        )
    project_root.mkdir(parents=True, exist_ok=True)

    config = _clone_config()
    exporters_obj = config["observability"].get("exporters", [])
    exporters = list(exporters_obj) if isinstance(exporters_obj, list) else []
    if with_prom and "prom" not in exporters:
        exporters.append("prom")
    config["observability"]["exporters"] = exporters

    context = ProjectContext(project_root, config)
    _create_project_layout(context, with_prom=with_prom, with_retrospect=with_retrospect, overwrite=force)
    context.write_config()
    return project_root


def create_flow(
    flow_name: str,
    *,
    project_root: Path | None = None,
    switch: bool = False,
    parallel: bool = False,
) -> Path:
    ctx = _load_project(project_root)
    ctx.flows_dir.mkdir(parents=True, exist_ok=True)
    flow_slug = _slug(flow_name)
    path = ctx.flows_dir / f"{flow_slug}.yaml"
    if path.exists():
        raise FileExistsError(f"Flow file '{path}' already exists")

    if switch:
        content, stubs = _flow_yaml_switch(flow_slug)
    elif parallel:
        content, stubs = _flow_yaml_parallel(flow_slug)
    else:
        content, stubs = _flow_yaml_basic(flow_slug)
    path.write_text(content, encoding="utf-8")
    _ensure_steps_module(ctx.steps_dir)
    _append_step_stubs(ctx.steps_dir / "impl.py", stubs)
    return path


def create_policy(
    policy_name: str,
    *,
    project_root: Path | None = None,
    strategy: str = "epsilon",
    mcp_endpoint: Optional[str] = None,
) -> Path:
    ctx = _load_project(project_root)
    ctx.policies_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(policy_name)
    path = ctx.policies_dir / f"{slug}.yaml"
    if path.exists():
        raise FileExistsError(f"Policy file '{path}' already exists")

    content = _policy_yaml(slug, strategy=strategy, mcp_endpoint=mcp_endpoint)
    path.write_text(content, encoding="utf-8")
    if mcp_endpoint is not None:
        ctx.config.setdefault("ai", {})
        ctx.config["ai"]["policy_endpoint"] = mcp_endpoint
        ctx.write_config()
    return path


def find_project_root(start: Path | None = None) -> Path:
    start_path = Path(start or Path.cwd()).resolve()
    for candidate in [start_path] + list(start_path.parents):
        if (candidate / "trace-mind.toml").exists():
            return candidate
    raise FileNotFoundError("trace-mind.toml not found in current directory or parents")


def _load_project(root: Path | None) -> ProjectContext:
    project_root = find_project_root(root)
    config_path = project_root / "trace-mind.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing trace-mind.toml at {config_path}")
    with config_path.open("rb") as fh:
        config = tomllib.load(fh)
    return ProjectContext(project_root, config)


def _clone_config() -> Dict[str, Dict[str, object]]:
    cloned: Dict[str, Dict[str, object]] = {}
    for section, fields in PROJECT_CONFIG.items():
        section_copy: Dict[str, object] = {}
        for key, value in fields.items():
            if isinstance(value, list):
                section_copy[key] = list(value)
            elif isinstance(value, dict):
                section_copy[key] = dict(value)
            else:
                section_copy[key] = value
        cloned[section] = section_copy
    return cloned


def _render_config(config: Mapping[str, Mapping[str, object]]) -> str:
    exporters_obj = config["observability"].get("exporters", [])
    exporters = exporters_obj if isinstance(exporters_obj, list) else []
    exporters_repr = ", ".join(f'"{value}"' for value in exporters)
    lines = [
        "[runtime]",
        f'flows_dir = "{config["runtime"]["flows_dir"]}"',
        f'services_dir = "{config["runtime"]["services_dir"]}"',
        f"max_concurrency = {config['runtime']['max_concurrency']}",
        f"queue_capacity = {config['runtime']['queue_capacity']}",
        "",
        "[observability]",
        f"exporters = [{exporters_repr}]",
        "",
        "[ai]",
        f'tuner = "{config["ai"].get("tuner", "epsilon")}"',
        f'policy_endpoint = "{config["ai"].get("policy_endpoint", "")}"',
        "",
    ]
    return "\n".join(lines)


def _create_project_layout(
    context: ProjectContext,
    *,
    with_prom: bool,
    with_retrospect: bool,
    overwrite: bool,
) -> None:
    context.flows_dir.mkdir(parents=True, exist_ok=True)
    hello_flow, hello_stubs = _flow_yaml_basic("hello")
    _write_file(context.flows_dir / "hello.yaml", hello_flow, overwrite=overwrite)

    context.policies_dir.mkdir(parents=True, exist_ok=True)
    _write_file(
        context.policies_dir / "default.yaml",
        _policy_yaml("default", strategy="epsilon", mcp_endpoint=None),
        overwrite=overwrite,
    )

    context.steps_dir.mkdir(parents=True, exist_ok=True)
    _write_file(context.steps_dir / "__init__.py", "", overwrite=overwrite)
    _write_file(context.steps_dir / "impl.py", _steps_impl_template(), overwrite=overwrite)
    _append_step_stubs(context.steps_dir / "impl.py", hello_stubs)

    context.services_dir.mkdir(parents=True, exist_ok=True)
    _write_file(context.services_dir / "hello.py", _service_template(), overwrite=overwrite)

    scripts_dir = context.root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    run_script = scripts_dir / "run_local.sh"
    _write_file(run_script, _RUN_SCRIPT_TEMPLATE, overwrite=overwrite)
    _make_executable(run_script)

    notes: list[str] = []
    if with_prom:
        notes.append(
            "- Prometheus helpers scaffolded. Install extras (e.g. `pip install tm[prom]`) before enabling metrics hooks."
        )
        hooks_dir = context.root / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        _write_file(hooks_dir / "__init__.py", "", overwrite=overwrite)
        _write_file(hooks_dir / "metrics_trace.py", _PROM_HOOK_TEMPLATE, overwrite=overwrite)
    if with_retrospect:
        notes.append("- Retrospect exporter stub added; requires metrics binlogs to function.")
        exporters_dir = context.root / "exporters"
        exporters_dir.mkdir(parents=True, exist_ok=True)
        _write_file(exporters_dir / "__init__.py", "", overwrite=overwrite)
        _write_file(exporters_dir / "retrospect_exporter.py", _RETROSPECT_TEMPLATE, overwrite=overwrite)

    project_readme = _project_readme(notes)
    _write_file(context.root / "README.md", project_readme, overwrite=overwrite)


def _write_file(path: Path, content: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.write_text(content, encoding="utf-8")


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _ensure_steps_module(steps_dir: Path) -> None:
    steps_dir.mkdir(parents=True, exist_ok=True)
    (steps_dir / "__init__.py").touch(exist_ok=True)
    impl = steps_dir / "impl.py"
    if not impl.exists():
        impl.write_text(_steps_impl_template(), encoding="utf-8")


def _append_step_stubs(impl_path: Path, stubs: Dict[str, str]) -> None:
    text = impl_path.read_text(encoding="utf-8")
    for name, snippet in stubs.items():
        if name not in text:
            with impl_path.open("a", encoding="utf-8") as fh:
                fh.write("\n" + snippet.strip() + "\n")


def _flow_yaml_basic(slug: str) -> tuple[str, Dict[str, str]]:
    content = dedent(f"""
        flow:
          id: {slug}
          version: "1.0.0"
          entry: greet
          steps:
            - id: greet
              kind: task
              hooks:
                run: steps.impl.{slug}_greet
            - id: finish
              kind: finish
          edges:
            - {{from: greet, to: finish}}
        """).strip() + "\n"
    stubs = {f"def {slug}_greet": dedent(f"""
            def {slug}_greet(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                name = state.get("name") or "world"
                return {{"name": name, "message": f"Hello, {{name}}!"}}
            """)}
    return content, stubs


def _flow_yaml_switch(slug: str) -> tuple[str, Dict[str, str]]:
    content = dedent(f"""
        flow:
          id: {slug}
          version: "1.0.0"
          entry: start
          steps:
            - id: start
              kind: task
              hooks:
                run: steps.impl.{slug}_start
            - id: router
              kind: switch
              config:
                cases:
                  branch_a: branch_a
                  branch_b: branch_b
                default: branch_a
              hooks:
                run: steps.impl.{slug}_route
            - id: branch_a
              kind: task
              hooks:
                run: steps.impl.{slug}_branch_a
            - id: branch_b
              kind: task
              hooks:
                run: steps.impl.{slug}_branch_b
            - id: finish
              kind: finish
          edges:
            - {{from: start, to: router}}
            - {{from: router, to: branch_a, when: branch_a}}
            - {{from: router, to: branch_b, when: branch_b}}
            - {{from: branch_a, to: finish}}
            - {{from: branch_b, to: finish}}
        """).strip() + "\n"
    stubs = {
        f"def {slug}_start": dedent(f"""
            def {slug}_start(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                return dict(state or {{}})
            """),
        f"def {slug}_route": dedent(f"""
            def {slug}_route(ctx: Dict[str, Any], state: Dict[str, Any]) -> str:
                desired = state.get("route")
                return desired if desired in {{"branch_a", "branch_b"}} else "branch_a"
            """),
        f"def {slug}_branch_a": dedent(f"""
            def {slug}_branch_a(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                data = dict(state or {{}})
                data["path"] = "branch_a"
                return data
            """),
        f"def {slug}_branch_b": dedent(f"""
            def {slug}_branch_b(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                data = dict(state or {{}})
                data["path"] = "branch_b"
                return data
            """),
    }
    return content, stubs


def _flow_yaml_parallel(slug: str) -> tuple[str, Dict[str, str]]:
    content = dedent(f"""
        flow:
          id: {slug}
          version: "1.0.0"
          entry: start
          steps:
            - id: start
              kind: task
              hooks:
                run: steps.impl.{slug}_start
            - id: fanout
              kind: parallel
              config:
                branches_map:
                  branch_a: steps.impl.{slug}_branch_a
                  branch_b: steps.impl.{slug}_branch_b
              hooks:
                run: tm.helpers.parallel
            - id: finish
              kind: finish
          edges:
            - {{from: start, to: fanout}}
            - {{from: fanout, to: finish}}
        """).strip() + "\n"
    stubs = {
        f"def {slug}_start": dedent(f"""
            def {slug}_start(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                return dict(state or {{}})
            """),
        f"def {slug}_branch_a": dedent(f"""
            def {slug}_branch_a(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                return {{"branch_a": True}}
            """),
        f"def {slug}_branch_b": dedent(f"""
            def {slug}_branch_b(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
                return {{"branch_b": True}}
            """),
    }
    return content, stubs


def _policy_yaml(slug: str, *, strategy: str, mcp_endpoint: Optional[str]) -> str:
    params: Dict[str, object]
    if strategy == "ucb":
        params = {"confidence": 2.0}
    else:
        strategy = "epsilon"
        params = {"epsilon": 0.2}
    doc = {
        "policy": {
            "id": slug,
            "strategy": strategy,
            "params": params,
        }
    }
    if mcp_endpoint:
        doc["policy"]["endpoint"] = mcp_endpoint
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _steps_impl_template() -> str:
    return dedent('''
        """Step implementations for the scaffolded project."""

        from __future__ import annotations

        from typing import Any, Dict


        def hello_greet(ctx: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            name = state.get("name") or "world"
            return {"name": name, "message": f"Hello, {name}!"}
        ''').lstrip() + "\n"


def _service_template() -> str:
    return dedent('''
        """Example service wrapper that runs the hello flow."""

        from __future__ import annotations

        from pathlib import Path
        from typing import Any, Dict

        from tm.run_recipe import run_recipe


        def run_hello(payload: Dict[str, Any]) -> Dict[str, Any]:
            recipe_path = Path(__file__).resolve().parent.parent / "flows" / "hello.yaml"
            return run_recipe(recipe_path, payload)
        ''').lstrip() + "\n"


def _project_readme(extra_notes: list[str]) -> str:
    notes_section = ""
    if extra_notes:
        notes = "\n".join(f"- {note}" for note in extra_notes)
        notes_section = f"\n## Extras\n\n{notes}\n"
    return dedent("""
        # TraceMind Project Scaffold

        This project was generated by `tm init`.

        ## Quick start

        ```bash
        pip install -e .
        tm run recipe flows/hello.yaml -i '{"name":"world"}'
        ```

        ## Layout

        - `flows/` – YAML recipes for flows.
        - `policies/` – Policy recipes.
        - `steps/impl.py` – Python step implementations.
        - `services/` – Optional wrappers or HTTP handlers.
        - `scripts/run_local.sh` – Convenience launcher.
        """).lstrip() + notes_section + "\n"


_PROM_HOOK_TEMPLATE = dedent('''
    """Prometheus trace hook placeholder."""

    from prometheus_client import Counter

    TRACE_EVENTS = Counter("trace_events_total", "Flow trace events", ["flow", "status"])


    def on_event(flow: str, status: str) -> None:
        TRACE_EVENTS.labels(flow=flow, status=status).inc()
    ''').lstrip() + "\n"


_RETROSPECT_TEMPLATE = dedent('''
    """Retrospect exporter placeholder."""

    from __future__ import annotations

    from typing import Iterable

    from tm.obs.retrospect import load_window


    def export_recent(dir_path: str, window_seconds: int = 300) -> Iterable[dict]:
        return load_window(dir_path, 0, window_seconds)
    ''').lstrip() + "\n"


_RUN_SCRIPT_TEMPLATE = dedent("""
    #!/usr/bin/env bash
    set -euo pipefail

    tm run recipe flows/hello.yaml "$@"
    """).lstrip() + "\n"


__all__ = [
    "create_flow",
    "create_policy",
    "find_project_root",
    "init_project",
]
