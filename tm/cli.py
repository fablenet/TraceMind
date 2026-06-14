# tm/cli.py
import argparse
import fnmatch
import json
import os
import shutil
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

try:
    import yaml
except ModuleNotFoundError:  # optional dependency; commands that need it will check explicitly
    yaml = None
from tm.app.demo_plan import build_plan
from tm.pipeline.analysis import analyze_plan
from tm.obs.retrospect import load_window
from tm.scaffold import create_flow, create_policy, init_project, find_project_root
from tm.run_recipe import run_recipe
from tm.governance.audit import AuditTrail
from tm.governance.config import load_governance_config
from tm.governance.hitl import HitlManager
from tm.runtime.workers import WorkerOptions, TaskWorkerSupervisor, install_signal_handlers
from tm.runtime.dlq import DeadLetterStore
from tm.runtime.queue import FileWorkQueue, InMemoryWorkQueue
from tm.runtime.idempotency import IdempotencyStore
from tm.runtime.queue.manager import EnqueueOutcome, TaskQueueManager
from tm.runtime.retry import load_retry_policy
from tm.daemon import DaemonStatus, build_paths, collect_status, start_daemon, stop_daemon
from tm.triggers.config import (
    TriggerConfigError,
    generate_sample_config,
    load_trigger_config,
)
from tm.triggers.runner import run_triggers
from tm.runtime import configure_engine, run_ir_flow, IrRunnerError
from tm.dsl import compile_paths, CompileError
from tm.runtime.config import RuntimeConfigError, load_runtime_config
from tm.runtime.workflow_executor import (
    WorkflowExecutionError,
    WorkflowVerificationError,
    execute_workflow,
)
from tm.verifier import WorkflowVerifier
from tm.monitoring.report import (
    IntegratedStateReportError,
    build_integrated_state_report,
)

__path__ = [str(Path(__file__).with_name("cli"))]
from tm.cli.verify_network import run_verify_network
from tm.cli.plugin_verify import run as plugin_verify_run
from tm.cli.artifacts_cli import register_artifacts_commands, register_plan_commands
from tm.cli.dsl import register_dsl_commands
from tm.cli.flow import register_flow_commands
from tm.cli.validate import register_validate_command
from tm.cli.fmt import register_fmt_command
from tm.cli.simulate import register_simulate_command
from tm.cli.caps import register_caps_commands
from tm.cli.intent import register_intent_commands
from tm.cli.intents import register_intents_commands
from tm.cli.pattern import register_pattern_commands
from tm.cli.compose import register_compose_commands
from tm.cli.iterate import register_iterate_commands
from tm.cli.patch import register_patch_commands
from tm.cli.rerun import register_rerun_command
from tm.cli.run_cli import register_run_commands
from tm.cli.replay import register_replay_commands
from tm.cli.controller_cli import register_controller_commands
from tm.cli.tests_runner import register_tests_commands
from tm.cli.proposal import register_proposal_commands
from tm.cli.gate import register_gate_commands
from tm.cli.ci import register_ci_commands
from tm.verify import (
    Explorer,
    TraceMindAdapter,
    build_report,
    load_plan as load_verify_plan,
    load_spec as load_verify_spec,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"
_DAEMON_FLAG_ENV = "TM_ENABLE_DAEMON"
_DEFAULT_TRIGGER_CONFIG = "triggers.yaml"


def _cli_version() -> str:
    try:
        return importlib_metadata.version("trace-mind")
    except importlib_metadata.PackageNotFoundError:
        return "trace-mind (development)"
    except Exception:
        return "trace-mind (unknown)"


def _init_from_template(template: str, project_name: str, *, force: bool) -> Path:
    template_dir = _TEMPLATE_ROOT / template
    if not template_dir.is_dir():
        raise FileNotFoundError(f"Unknown template '{template}'")
    project_root = Path.cwd() / project_name
    if project_root.exists():
        if not project_root.is_dir():
            raise FileExistsError(f"Destination '{project_root}' exists and is not a directory")
        if not force and any(project_root.iterdir()):
            raise FileExistsError(
                f"Destination '{project_root}' already exists; use --force to overwrite scaffolding files"
            )
    else:
        project_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_dir, project_root, dirs_exist_ok=True)
    return project_root


def _is_feature_enabled(value: Optional[str]) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _daemon_feature_enabled() -> bool:
    return _is_feature_enabled(os.getenv(_DAEMON_FLAG_ENV))


def _require_daemon_enabled() -> None:
    if not _daemon_feature_enabled():
        print(f"Daemon commands require {_DAEMON_FLAG_ENV} to be enabled", file=sys.stderr)
        sys.exit(1)


def _default_daemon_state_dir() -> Path:
    override = os.getenv("TM_DAEMON_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".trace-mind" / "daemon"


def _print_daemon_status(status: DaemonStatus) -> None:
    state_label = "running" if status.running else ("stale" if status.stale else "stopped")
    print(f"daemon status : {state_label}")
    if status.pid is not None:
        print(f"pid           : {status.pid}")
    if status.uptime_s is not None:
        print(f"uptime        : {status.uptime_s:.1f}s")
    if status.queue is not None:
        queue = status.queue
        print(f"queue backend : {queue.backend}")
        print(f"queue path    : {queue.path}")
        print(f"backlog       : {queue.backlog}")
        print(f"pending       : {queue.pending}")
        print(f"inflight      : {queue.inflight}")
        if queue.oldest_available_at is not None:
            lag = max(0.0, time.time() - queue.oldest_available_at)
            print(f"lag_seconds   : {lag:.3f}")
    if status.stale and status.pid:
        print("note          : recorded PID is stale (process no longer running)")


def _load_json_arg(value: Optional[str], *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if value is None:
        return dict(default or {})
    raw = value
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError("JSON payload must be an object")
    return dict(data)


def _resolve_flow_id(arg: str) -> str:
    path = Path(arg)
    if not path.is_file():
        return arg
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse flow spec at {path}: {exc}") from exc
        if isinstance(data, Mapping):
            flow = data.get("flow")
            if isinstance(flow, Mapping):
                flow_id = flow.get("id")
                if isinstance(flow_id, str) and flow_id.strip():
                    return flow_id.strip()
        return path.stem
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to load flow specifications; install the 'yaml' extra, e.g. `pip install trace-mind[yaml]`."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to parse flow spec at {path}: {exc}") from exc
    if isinstance(data, Mapping):
        flow = data.get("flow")
        if isinstance(flow, Mapping):
            flow_id = flow.get("id")
            if isinstance(flow_id, str) and flow_id.strip():
                return flow_id.strip()
    return path.stem


def _enqueue_task(
    *,
    flow_id: str,
    payload: Mapping[str, Any],
    headers: Optional[Mapping[str, Any]],
    trace: Optional[Mapping[str, Any]],
    queue_backend: str,
    queue_dir: str,
    idempotency_dir: str,
    idempotency_key: Optional[str] = None,
) -> EnqueueOutcome:
    backend = queue_backend or "file"
    headers_payload = dict(headers or {})
    if idempotency_key:
        headers_payload.setdefault("idempotency_key", idempotency_key)
    if backend == "file":
        queue_path = Path(queue_dir).resolve()
        queue_path.mkdir(parents=True, exist_ok=True)
        queue = FileWorkQueue(str(queue_path))
    elif backend == "memory":
        queue = InMemoryWorkQueue()
    else:
        raise ValueError(f"Unsupported queue backend '{backend}'")
    idem_path = Path(idempotency_dir).resolve()
    idem_path.mkdir(parents=True, exist_ok=True)
    idem_store = IdempotencyStore(dir_path=str(idem_path))
    manager = TaskQueueManager(queue, idem_store)
    try:
        outcome = manager.enqueue(
            flow_id=flow_id,
            input=payload,
            headers=headers_payload or None,
            trace=dict(trace or {}),
        )
    finally:
        if backend == "file":
            try:
                queue.flush()
            finally:
                queue.close()
    return outcome


def _report_enqueue_outcome(flow_id: str, outcome: EnqueueOutcome) -> None:
    if outcome.queued and outcome.envelope:
        print(f"enqueued task {outcome.envelope.task_id} for flow '{flow_id}'")
    elif outcome.cached_result is not None:
        print("duplicate request (served from idempotency cache)")
    else:
        print("task already pending; not enqueued")


def _cmd_pipeline_analyze(args):
    plan = build_plan()
    focus = args.focus or []
    rep = analyze_plan(plan, focus_fields=focus)

    # Print summary
    print("== Step dependency topo ==")
    if rep.graphs.topo:
        print(" -> ".join(rep.graphs.topo))
    else:
        print("CYCLES detected:")
        for cyc in rep.graphs.cycles:
            print("  - " + " -> ".join(cyc))

    print("\n== Conflicts ==")
    if not rep.conflicts:
        print("  (none)")
    else:
        for c in rep.conflicts:
            print(f"  [{c.kind}] where={c.where} a={c.a} b={c.b} detail={c.detail}")

    print("\n== Coverage ==")
    print("  unused_steps:", rep.coverage.unused_steps or "[]")
    print("  empty_rules:", rep.coverage.empty_rules or "[]")
    print("  empty_triggers:", rep.coverage.empty_triggers or "[]")
    if rep.coverage.focus_uncovered:
        print("  focus_uncovered:", rep.coverage.focus_uncovered)


def _cmd_pipeline_export_dot(args):
    plan = build_plan()
    rep = analyze_plan(plan)
    with open(args.out_rules_steps, "w", encoding="utf-8") as f:
        f.write(rep.dot_rules_steps)
    with open(args.out_step_deps, "w", encoding="utf-8") as f:
        f.write(rep.dot_step_deps)
    print("DOT files written:", args.out_rules_steps, "and", args.out_step_deps)


def _build_hitl_manager(config_path: str) -> HitlManager:
    cfg = load_governance_config(config_path)
    hitl_cfg = cfg.hitl
    if not hitl_cfg.enabled:
        raise RuntimeError("HITL approvals are disabled in configuration")
    return HitlManager(hitl_cfg, audit=AuditTrail(cfg.audit))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tm", description="TraceMind CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_cli_version()}")
    parser.add_argument(
        "--runtime-config",
        dest="runtime_config_path",
        help="Path to runtime configuration (default: packaged runtime.yaml)",
    )
    parser.add_argument(
        "--engine",
        choices=["python", "proc"],
        help="Override runtime engine for this invocation",
    )
    parser.add_argument(
        "--executor-path",
        dest="executor_path",
        help="Path to process executor when using --engine proc",
    )
    sub = parser.add_subparsers(dest="cmd")

    plugin_parser = sub.add_parser("plugin", help="plugin tools")
    plugin_sub = plugin_parser.add_subparsers(dest="pcmd")
    plugin_verify = plugin_sub.add_parser("verify", help="verify plugin conformance")
    plugin_verify.add_argument("group")
    plugin_verify.add_argument("name")
    plugin_verify.set_defaults(func=plugin_verify_run)

    sp = sub.add_parser("pipeline", help="pipeline tools")
    ssp = sp.add_subparsers(dest="pcmd")

    sp_an = ssp.add_parser("analyze", help="analyze current plan")
    sp_an.add_argument("--focus", nargs="*", help="fields to check coverage (e.g. services[].state status)")
    sp_an.set_defaults(func=_cmd_pipeline_analyze)

    sp_dot = ssp.add_parser("export-dot", help="export DOT graphs")
    sp_dot.add_argument("--out-rules-steps", required=True, help="output .dot for rule->steps")
    sp_dot.add_argument("--out-step-deps", required=True, help="output .dot for step dependency graph")
    sp_dot.set_defaults(func=_cmd_pipeline_export_dot)

    def _parse_duration(expr: str) -> timedelta:
        units = {"s": 1, "m": 60, "h": 3600}
        suffix = expr[-1:]
        if suffix not in units:
            raise ValueError(f"Invalid duration '{expr}'")
        try:
            value = float(expr[:-1])
        except ValueError as exc:
            raise ValueError(f"Invalid duration '{expr}'") from exc
        return timedelta(seconds=value * units[suffix])

    def _cmd_metrics_dump(args):
        window = _parse_duration(args.window)
        until = datetime.now(timezone.utc)
        since = until - window
        entries = load_window(args.dir, since, until)
        if args.format == "csv":
            print("type,name,labels,value")
            for entry in entries:
                label_str = ";".join(f"{k}={v}" for k, v in sorted(entry["labels"].items()))
                print(f"{entry['type']},{entry['name']},{label_str},{entry['value']}")
        else:
            print(json.dumps(entries, indent=2))

    def _cmd_verify_semantic(args):
        if not args.plan or not args.spec:
            print("verify: --plan and --spec are required", file=sys.stderr)
            return 1
        plan = load_verify_plan(Path(args.plan))
        spec = load_verify_spec(Path(args.spec))
        adapter = TraceMindAdapter.from_plan(
            plan,
            initial_store=spec.initial_store,
            changed_paths=spec.changed_paths,
            initial_pending=spec.initial_pending,
        )
        explorer = Explorer(adapter)
        model = explorer.run(max_depth=int(args.depth), hash_mode=args.hash_mode)
        report = build_report(
            invariants=spec.invariants,
            properties=spec.properties,
            model=model,
            adapter=adapter,
        )
        ok = all(inv.ok for inv in report.invariants) and all(p.ok for p in report.properties) and not model.deadlocks
        if args.format == "json":
            print(json.dumps(report.as_dict(), indent=2))
        else:
            print(f"explored={report.explored_states} depth<={report.max_depth} deadlocks={len(report.deadlocks)}")
            if model.deadlocks:
                print(f"deadlock_states={model.deadlocks}")
            for inv in report.invariants:
                status = "OK" if inv.ok else "FAIL"
                print(f"invariant: {inv.expr} -> {status}")
                if inv.reason and not inv.ok:
                    print(f"  reason: {inv.reason}")
                if inv.path and not inv.ok:
                    print(f"  path: {inv.path}")
            for prop in report.properties:
                status = "OK" if prop.ok else "FAIL"
                print(f"property {prop.name}: {status} ({prop.formula})")
                if prop.reason and not prop.ok:
                    print(f"  reason: {prop.reason}")
                if prop.counterexample and not prop.ok:
                    print(f"  counterexample: {prop.counterexample}")
        return 0 if ok else 1

    def _cmd_verify_online(args):
        manifest_path = Path(args.manifest)
        if args.sources:
            sources = [Path(src) for src in args.sources]
            out_dir = Path(args.out) if args.out else manifest_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                compile_paths(
                    sources,
                    out_dir=out_dir,
                    force=args.force,
                    run_lint=not args.no_lint,
                    emit_ir=True,
                )
            except CompileError as exc:
                print(f"verify: compilation failed: {exc}", file=sys.stderr)
                return 1
            manifest_path = out_dir / "manifest.json"

        if not manifest_path.exists():
            print(f"verify: manifest not found at {manifest_path}", file=sys.stderr)
            return 1

        inputs = _load_json_arg(getattr(args, "inputs", None), default={})
        try:
            result = run_ir_flow(args.flow, manifest_path=manifest_path, inputs=inputs)
        except IrRunnerError as exc:
            print(f"verify: runtime error: {exc}", file=sys.stderr)
            return 1

        payload = {
            "flow": args.flow,
            "manifest": str(manifest_path),
            "status": result.status,
            "summary": result.summary or {},
            "events": list(result.events),
        }
        print(json.dumps(payload, indent=2))
        return 0 if result.status == "completed" else 1

    register_compose_commands(sub)
    register_iterate_commands(sub)
    register_patch_commands(sub)
    register_rerun_command(sub)

    sp_metrics = sub.add_parser("metrics", help="metrics tools")
    spm_sub = sp_metrics.add_subparsers(dest="mcmd")
    spm_dump = spm_sub.add_parser("dump", help="dump metrics window")
    spm_dump.add_argument("--dir", required=True, help="binlog directory")
    spm_dump.add_argument("--window", default="5m", help="window size (e.g. 5m, 1h)")
    spm_dump.add_argument("--format", choices=["csv", "json"], default="csv")
    spm_dump.set_defaults(func=_cmd_metrics_dump)

    runtime_parser = sub.add_parser("runtime", help="Runtime utilities")
    runtime_sub = runtime_parser.add_subparsers(dest="rcmd")

    runtime_run = runtime_sub.add_parser(
        "run",
        help="Execute a flow IR using the configured runtime engine",
        description="Run a flow from manifest.json produced by `tm dsl compile --emit-ir`.",
    )
    runtime_run.add_argument("--manifest", required=True, help="Path to manifest.json")
    runtime_run.add_argument("--flow", required=True, help="Flow name to execute")
    runtime_run.add_argument(
        "--inputs",
        help="JSON object with run inputs or @path to a JSON file",
    )
    runtime_run.set_defaults(func=_cmd_runtime_run_ir)

    run_workflow_parser = runtime_sub.add_parser(
        "run-workflow",
        help="Run a WorkflowPolicy through the governance-bound executor",
        description="Execute a verified workflow policy, honor policy guards, and emit an ExecutionTrace.",
    )
    run_workflow_parser.add_argument("--workflow", required=True, help="WorkflowPolicy JSON/YAML path")
    run_workflow_parser.add_argument("--policy", required=True, help="PolicySpec JSON/YAML path")
    run_workflow_parser.add_argument(
        "--capabilities",
        nargs="+",
        required=True,
        help="Capability spec JSON/YAML paths covering referenced capabilities",
    )
    run_workflow_parser.add_argument(
        "--guard-decision",
        action="append",
        help="Guard decision in the form name=true|false (repeatable)",
    )
    run_workflow_parser.add_argument(
        "--events",
        nargs="+",
        help="Additional events to append to the execution trace",
    )
    run_workflow_parser.add_argument("--format", choices=["json"], default="json", help="output format")
    run_workflow_parser.set_defaults(func=_cmd_runtime_run_workflow)

    report_state_parser = runtime_sub.add_parser(
        "report-state",
        help="Describe the integrated semantic state inferred from an ExecutionTrace",
        description="Combine a validated WorkflowPolicy, PolicySpec, and capability catalog with an ExecutionTrace to emit an IntegratedStateReport with evidence/blame.",
    )
    report_state_parser.add_argument("--workflow", required=True, help="WorkflowPolicy JSON/YAML path")
    report_state_parser.add_argument("--policy", required=True, help="PolicySpec JSON/YAML path")
    report_state_parser.add_argument(
        "--capabilities",
        nargs="+",
        required=True,
        help="Capability spec JSON/YAML paths that describe state extractors/events",
    )
    report_state_parser.add_argument("--trace", required=True, help="ExecutionTrace JSON/YAML path")
    report_state_parser.add_argument("--format", choices=["json"], default="json", help="output format")
    report_state_parser.set_defaults(func=_cmd_runtime_report_state)

    verify_parser = sub.add_parser("verify", help="Verification commands")
    verify_parser.add_argument("--plan", help="Path to pipeline plan (JSON/YAML)")
    verify_parser.add_argument("--spec", help="Path to verification spec (JSON/YAML)")
    verify_parser.add_argument("--depth", type=int, default=8, help="Maximum exploration depth (default: 8)")
    verify_parser.add_argument(
        "--hash-mode", choices=["full", "store"], default="full", help="State hashing mode for deduplication"
    )
    verify_parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    verify_sub = verify_parser.add_subparsers(dest="vcmd")
    verify_parser.set_defaults(func=_cmd_verify_semantic)

    workflow_parser = verify_sub.add_parser("workflow", help="verify WorkflowPolicy artifacts")
    workflow_parser.add_argument("--workflow", required=True, help="WorkflowPolicy JSON/YAML path")
    workflow_parser.add_argument("--policy", required=True, help="PolicySpec JSON/YAML path")
    workflow_parser.add_argument(
        "--capabilities",
        nargs="+",
        required=True,
        help="Capability spec JSON/YAML paths",
    )
    workflow_parser.add_argument("--json", action="store_true", help="emit JSON counterexample")
    workflow_parser.set_defaults(func=_cmd_verify_workflow)

    verify_online = verify_sub.add_parser(
        "online",
        help="Execute Flow IR with the configured runtime engine",
        description="Use precompiled manifest/IR artifacts (or recompile sources) to verify a flow online.",
    )
    verify_online.add_argument("--flow", required=True, help="Flow name to execute")
    verify_online.add_argument(
        "--manifest",
        default="out/dsl/manifest.json",
        help="Path to manifest.json (default: out/dsl/manifest.json)",
    )
    verify_online.add_argument(
        "--inputs",
        help="JSON object with run inputs or @path to a JSON file",
    )
    verify_online.add_argument(
        "--sources",
        nargs="*",
        help="Optional WDL/PDL sources or directories to compile before verification",
    )
    verify_online.add_argument(
        "--out",
        help="Output directory for compiled artifacts (defaults to manifest directory)",
    )
    verify_online.add_argument("--force", action="store_true", help="Overwrite existing artifacts on compile")
    verify_online.add_argument("--no-lint", action="store_true", help="Skip linting before compilation")
    verify_online.set_defaults(func=_cmd_verify_online)

    verify_network = verify_sub.add_parser(
        "network",
        help="verify AgentNetwork joint Kripke properties offline",
    )
    verify_network.add_argument("agent_network", help="AgentNetwork YAML/JSON path")
    verify_network.add_argument(
        "--bundle",
        action="append",
        metavar="REF=PATH",
        help="Bundle artifact path keyed by bundle ref (repeatable)",
    )
    verify_network.add_argument("--formulas", help="YAML/JSON file listing CTL formulas")
    verify_network.add_argument("--max-depth", type=int, default=16, help="Maximum BFS depth (default: 16)")
    verify_network.add_argument("--hash-mode", choices=["full", "store"], default="full", help="State hashing mode")
    verify_network.add_argument(
        "--mode",
        choices=["monolithic", "compositional"],
        default="monolithic",
        help="Verification mode: monolithic full product (default) or compositional assume-guarantee",
    )
    verify_network.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    verify_network.set_defaults(func=run_verify_network)

    approve_parser = sub.add_parser("approve", help="manage human approvals")
    approve_parser.add_argument("--config", default="trace-mind.toml", help="governance config path")
    approve_parser.add_argument("--list", action="store_true", help="list pending approvals")
    approve_parser.add_argument("approval_id", nargs="?", help="approval identifier")
    approve_parser.add_argument("--decision", choices=["approve", "deny"], help="decision to apply")
    approve_parser.add_argument("--actor", default="cli", help="actor identifier")
    approve_parser.add_argument("--note", help="optional note")

    def _cmd_approve(args):
        try:
            manager = _build_hitl_manager(args.config)
        except Exception as exc:  # pragma: no cover - CLI error path
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        if args.list:
            records = manager.pending()
            if not records:
                print("(no pending approvals)")
                return
            for record in records:
                print(
                    json.dumps(
                        {
                            "approval_id": record.approval_id,
                            "flow": record.flow,
                            "step": record.step,
                            "reason": record.reason,
                            "actors": list(record.actors),
                            "created_at": record.created_at,
                            "ttl_ms": record.ttl_ms,
                        },
                        ensure_ascii=False,
                    )
                )
            return

        if not args.approval_id or not args.decision:
            print("approval_id and --decision are required unless using --list", file=sys.stderr)
            sys.exit(1)

        try:
            record = manager.decide(
                args.approval_id,
                decision=args.decision,
                actor=args.actor or "cli",
                note=args.note,
            )
        except Exception as exc:  # pragma: no cover - CLI error path
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        else:
            print(
                json.dumps(
                    {
                        "approval_id": record.approval_id,
                        "decision": record.status,
                        "actor": record.decided_by,
                        "note": record.note,
                    },
                    ensure_ascii=False,
                )
            )

    approve_parser.set_defaults(func=_cmd_approve)

    init_parser = sub.add_parser("init", help="initialize a new TraceMind project")
    init_parser.add_argument("project_name", help="project directory to create")
    init_parser.add_argument("--with-prom", action="store_true", help="include Prometheus hook scaffold")
    init_parser.add_argument("--with-retrospect", action="store_true", help="include Retrospect exporter scaffold")
    init_parser.add_argument("--force", action="store_true", help="overwrite existing scaffold files")
    init_parser.add_argument("--template", choices=["minimal", "recipe-only"], help="use a project template")

    def _cmd_init(args):
        try:
            if args.template:
                _init_from_template(args.template, args.project_name, force=args.force)
            else:
                init_project(
                    args.project_name,
                    Path.cwd(),
                    with_prom=args.with_prom,
                    with_retrospect=args.with_retrospect,
                    force=args.force,
                )
        except (FileExistsError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Project '{args.project_name}' ready")

    init_parser.set_defaults(func=_cmd_init)

    new_parser = sub.add_parser("new", help="generate project assets")
    new_sub = new_parser.add_subparsers(dest="asset")

    flow_parser = new_sub.add_parser("flow", help="create a flow skeleton")
    flow_parser.add_argument("flow_name", help="flow name")
    variant = flow_parser.add_mutually_exclusive_group()
    variant.add_argument("--switch", action="store_true", help="include a switch step")
    variant.add_argument("--parallel", action="store_true", help="include a parallel step")

    def _cmd_new_flow(args):
        try:
            root = find_project_root(Path.cwd())
            created = create_flow(args.flow_name, project_root=root, switch=args.switch, parallel=args.parallel)
        except Exception as exc:  # pragma: no cover - CLI error path
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Flow created: {created.relative_to(root)}")

    flow_parser.set_defaults(func=_cmd_new_flow)

    policy_parser = new_sub.add_parser("policy", help="create a policy skeleton")
    policy_parser.add_argument("policy_name", help="policy identifier")
    strategy = policy_parser.add_mutually_exclusive_group()
    strategy.add_argument("--epsilon", action="store_true", help="generate epsilon-greedy policy")
    strategy.add_argument("--ucb", action="store_true", help="generate UCB policy")
    policy_parser.add_argument("--mcp-endpoint", help="default MCP endpoint", default=None)

    def _cmd_new_policy(args):
        try:
            root = find_project_root(Path.cwd())
            strat = "ucb" if args.ucb else "epsilon"
            created = create_policy(args.policy_name, project_root=root, strategy=strat, mcp_endpoint=args.mcp_endpoint)
        except Exception as exc:  # pragma: no cover - CLI error path
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Policy created: {created.relative_to(root)}")

    policy_parser.set_defaults(func=_cmd_new_policy)

    run_parser = sub.add_parser("run", help="execute a flow recipe or agent bundle")
    run_sub = run_parser.add_subparsers(dest="rcmd", required=True)
    setattr(run_parser, "_run_subparsers", run_sub)
    recipe_parser = run_sub.add_parser("recipe", help="execute a flow recipe (YAML/JSON)")
    recipe_parser.add_argument("recipe", help="path to recipe (JSON or YAML)")
    recipe_parser.add_argument("-i", "--input", default=None, help="JSON string or @file with initial state")
    recipe_parser.add_argument(
        "--detached",
        action="store_true",
        help=f"enqueue the run for background workers (requires {_DAEMON_FLAG_ENV}=1)",
    )
    recipe_parser.add_argument(
        "--queue", choices=["file", "memory"], default="file", help="queue backend for detached runs"
    )
    recipe_parser.add_argument(
        "--queue-dir",
        default="data/queue",
        help="queue directory for detached runs when using the file backend",
    )
    recipe_parser.add_argument(
        "--idempotency-dir",
        default="data/idempotency",
        help="idempotency cache directory for detached runs",
    )
    recipe_parser.add_argument(
        "--idempotency-key",
        help="set idempotency key header for detached runs",
    )
    recipe_parser.add_argument(
        "--headers",
        help="additional headers JSON or @file (detached runs only)",
    )
    recipe_parser.add_argument(
        "--trace",
        help="trace metadata JSON or @file (detached runs only)",
    )

    def _cmd_run(args):
        try:
            payload = _load_json_arg(args.input, default={})
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        if args.detached:
            if not _daemon_feature_enabled():
                print(f"Detached runs require {_DAEMON_FLAG_ENV} to be enabled", file=sys.stderr)
                sys.exit(1)
            try:
                headers = _load_json_arg(args.headers, default={}) if args.headers else {}
                trace = _load_json_arg(args.trace, default={}) if args.trace else {}
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            try:
                flow_id = _resolve_flow_id(args.recipe)
            except (RuntimeError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            outcome = _enqueue_task(
                flow_id=flow_id,
                payload=payload,
                headers=headers,
                trace=trace,
                queue_backend=args.queue,
                queue_dir=args.queue_dir,
                idempotency_dir=args.idempotency_dir,
                idempotency_key=args.idempotency_key,
            )
            _report_enqueue_outcome(flow_id, outcome)
            return 0

        result = run_recipe(Path(args.recipe), payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    recipe_parser.set_defaults(func=_cmd_run)

    enqueue_parser = sub.add_parser(
        "enqueue",
        help="enqueue a flow run",
        description="Enqueue a flow invocation for background workers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm enqueue flows/hello.yaml -i '{\"name\":\"world\"}'""",
    )
    enqueue_parser.add_argument("flow", help="Flow id or YAML spec path containing flow.id")
    enqueue_parser.add_argument("-i", "--input", default="{}", help="JSON payload or @file path")
    enqueue_parser.add_argument("--queue", choices=["file", "memory"], default="file", help="queue backend")
    enqueue_parser.add_argument("--queue-dir", default="data/queue", help="queue directory (file backend)")
    enqueue_parser.add_argument(
        "--idempotency-dir",
        default="data/idempotency",
        help="idempotency cache directory",
    )
    enqueue_parser.add_argument("--idempotency-key", help="set idempotency key header")
    enqueue_parser.add_argument("--headers", help="additional headers JSON or @file")
    enqueue_parser.add_argument("--trace", help="trace metadata JSON or @file")

    def _cmd_enqueue(args):
        try:
            payload = _load_json_arg(args.input, default={})
            headers = _load_json_arg(args.headers, default={}) if args.headers else {}
            trace = _load_json_arg(args.trace, default={}) if args.trace else {}
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        try:
            flow_id = _resolve_flow_id(args.flow)
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        if not flow_id:
            print("Unable to determine flow id", file=sys.stderr)
            sys.exit(1)

        outcome = _enqueue_task(
            flow_id=flow_id,
            payload=payload,
            headers=headers,
            trace=trace,
            queue_backend=args.queue,
            queue_dir=args.queue_dir,
            idempotency_dir=args.idempotency_dir,
            idempotency_key=args.idempotency_key,
        )
        _report_enqueue_outcome(flow_id, outcome)

    enqueue_parser.set_defaults(func=_cmd_enqueue)

    daemon_parser = sub.add_parser(
        "daemon",
        help="manage the background daemon",
        description="Start, inspect, and stop the TraceMind background daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:\n  tm daemon start\n  tm daemon ps --json\n  tm daemon stop""",
    )
    daemon_sub = daemon_parser.add_subparsers(dest="dcmd")
    daemon_sub.required = True

    daemon_start = daemon_sub.add_parser(
        "start",
        help="launch the daemon as a background process",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    daemon_start.add_argument("--state-dir", default=str(_default_daemon_state_dir()), help="daemon metadata directory")
    daemon_start.add_argument("--queue-dir", default="data/queue", help="work queue directory (file backend)")
    daemon_start.add_argument("--idempotency-dir", default="data/idempotency", help="idempotency cache directory")
    daemon_start.add_argument("--dlq-dir", default="data/dlq", help="dead letter queue directory")
    daemon_start.add_argument("--runtime", default="tm.app.wiring_flows:_runtime", help="worker runtime factory")
    daemon_start.add_argument("--workers", type=int, default=1, help="number of worker processes")
    daemon_start.add_argument("--python", default=sys.executable, help="Python interpreter to launch daemon with")
    daemon_start.add_argument(
        "--log-file",
        help="redirect daemon stdout/stderr to this file (default: <state-dir>/daemon.log)",
    )
    daemon_start.add_argument(
        "--inherit-logs",
        action="store_true",
        help="inherit stdout/stderr instead of redirecting to a log file",
    )
    daemon_start.add_argument(
        "--enable-triggers",
        action="store_true",
        help="run triggers alongside workers (requires triggers config)",
    )
    daemon_start.add_argument(
        "--triggers-config",
        help="trigger configuration file to load when --enable-triggers is set",
    )

    def _cmd_daemon_start(args):
        _require_daemon_enabled()
        if args.workers <= 0:
            print("workers must be positive", file=sys.stderr)
            sys.exit(1)

        state_dir = Path(args.state_dir).expanduser()
        paths = build_paths(str(state_dir))

        queue_dir = Path(args.queue_dir).expanduser().resolve()
        queue_dir.mkdir(parents=True, exist_ok=True)
        idem_dir = Path(args.idempotency_dir).expanduser().resolve()
        idem_dir.mkdir(parents=True, exist_ok=True)
        dlq_dir = Path(args.dlq_dir).expanduser().resolve()
        dlq_dir.mkdir(parents=True, exist_ok=True)

        triggers_config = None
        if args.enable_triggers:
            if not args.triggers_config:
                print("--enable-triggers requires --triggers-config", file=sys.stderr)
                return 1
            triggers_config = str(Path(args.triggers_config).expanduser())
            try:
                load_trigger_config(triggers_config)
            except TriggerConfigError as exc:
                print(f"invalid triggers config: {exc}", file=sys.stderr)
                return 1
        python_exec = Path(args.python).expanduser()
        command = [
            str(python_exec),
            "-m",
            "tm.daemon.run",
            "--workers",
            str(args.workers),
            "--queue-dir",
            str(queue_dir),
            "--idempotency-dir",
            str(idem_dir),
            "--dlq-dir",
            str(dlq_dir),
            "--runtime",
            args.runtime,
        ]
        if triggers_config:
            command.extend(["--triggers-config", triggers_config])

        metadata = {
            "workers": args.workers,
            "runtime": args.runtime,
            "idempotency_dir": str(idem_dir),
            "dlq_dir": str(dlq_dir),
        }
        if triggers_config:
            metadata["triggers_config"] = triggers_config

        env = os.environ.copy()
        env.setdefault(_DAEMON_FLAG_ENV, "1")
        if triggers_config:
            env.setdefault("TM_TRIGGERS_CONFIG", triggers_config)

        log_path: Optional[Path] = None
        log_handle = None
        if not args.inherit_logs:
            log_path = Path(args.log_file).expanduser() if args.log_file else Path(paths.root) / "daemon.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "ab")
            metadata["log_file"] = str(log_path)

        try:
            stdout = log_handle if log_handle is not None else None
            stderr = log_handle if log_handle is not None else None
            result = start_daemon(
                paths,
                command=command,
                queue_dir=str(queue_dir),
                metadata=metadata,
                env=env,
                stdout=stdout,
                stderr=stderr,
                triggers_config=triggers_config,
                triggers_queue_dir=str(queue_dir),
                triggers_idem_dir=str(idem_dir),
                triggers_dlq_dir=str(dlq_dir),
            )
        finally:
            if log_handle is not None:
                log_handle.close()

        if result.started:
            notes = []
            if not args.inherit_logs:
                notes.append(f"logs: {log_path}")
            if triggers_config:
                notes.append(f"triggers: {triggers_config}")
            suffix = f" ({', '.join(notes)})" if notes else ""
            print(f"started daemon pid {result.pid} (queue: {queue_dir}){suffix}")
            return 0

        reason = result.reason or "unknown"
        if reason == "already-running" and result.pid:
            print(f"daemon already running (pid {result.pid})", file=sys.stderr)
        else:
            print(f"failed to start daemon: {reason}", file=sys.stderr)
        sys.exit(1)

    daemon_start.set_defaults(func=_cmd_daemon_start)

    daemon_ps = daemon_sub.add_parser(
        "ps",
        help="show daemon status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    daemon_ps.add_argument("--state-dir", default=str(_default_daemon_state_dir()), help="daemon metadata directory")
    daemon_ps.add_argument("--queue-dir", help="override queue directory used for status collection")
    daemon_ps.add_argument("--json", action="store_true", help="output JSON")

    def _cmd_daemon_ps(args):
        _require_daemon_enabled()
        state_dir = Path(args.state_dir).expanduser()
        paths = build_paths(str(state_dir))
        queue_override = None
        if args.queue_dir:
            queue_override = str(Path(args.queue_dir).expanduser().resolve())
        status = collect_status(paths, queue_dir=queue_override)
        if args.json:
            print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_daemon_status(status)
        return 0

    daemon_ps.set_defaults(func=_cmd_daemon_ps)

    daemon_stop = daemon_sub.add_parser(
        "stop",
        help="shut down the daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    daemon_stop.add_argument("--state-dir", default=str(_default_daemon_state_dir()), help="daemon metadata directory")
    daemon_stop.add_argument("--timeout", type=float, default=10.0, help="grace period before forcing termination (s)")
    daemon_stop.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="polling interval when waiting for graceful shutdown (s)",
    )
    daemon_stop.add_argument(
        "--no-force",
        action="store_true",
        help="do not escalate to SIGKILL/TerminateProcess if graceful stop times out",
    )

    def _cmd_daemon_stop(args):
        _require_daemon_enabled()
        state_dir = Path(args.state_dir).expanduser()
        paths = build_paths(str(state_dir))
        result = stop_daemon(
            paths,
            timeout=max(0.0, args.timeout),
            poll_interval=max(0.05, args.poll_interval),
            force=not args.no_force,
        )
        if result.stopped:
            verb = "forced" if result.forced else "graceful"
            pid_display = result.pid if result.pid is not None else "unknown"
            print(f"stopped daemon pid {pid_display} ({verb})")
            return 0

        reason = result.reason or "unknown"
        if reason in {"not-recorded", "not-running"}:
            print("daemon not running")
            return 0
        print(f"failed to stop daemon: {reason}", file=sys.stderr)
        sys.exit(1)

    daemon_stop.set_defaults(func=_cmd_daemon_stop)

    triggers_parser = sub.add_parser(
        "triggers",
        help="manage trigger configuration",
        description="Generate and validate trigger configuration for the TraceMind trigger engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    triggers_sub = triggers_parser.add_subparsers(dest="tcmd")
    triggers_sub.required = True

    triggers_init = triggers_sub.add_parser(
        "init",
        help="write a sample triggers.yaml",
    )
    triggers_init.add_argument(
        "--path",
        default=_DEFAULT_TRIGGER_CONFIG,
        help="destination config file (default: triggers.yaml)",
    )
    triggers_init.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing file",
    )

    def _cmd_triggers_init(args):
        dest = Path(args.path)
        if dest.exists() and not args.force:
            print(f"{dest} already exists; use --force to overwrite", file=sys.stderr)
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = generate_sample_config()
        dest.write_text(content, encoding="utf-8")
        print(f"wrote sample trigger config to {dest}")
        return 0

    triggers_init.set_defaults(func=_cmd_triggers_init)

    triggers_validate = triggers_sub.add_parser(
        "validate",
        help="validate an existing trigger configuration",
    )
    triggers_validate.add_argument(
        "--path",
        default=_DEFAULT_TRIGGER_CONFIG,
        help="config file to validate (default: triggers.yaml)",
    )

    def _cmd_triggers_validate(args):
        try:
            cfg = load_trigger_config(args.path)
        except TriggerConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        total = sum(1 for _ in cfg.all())
        print(f"trigger config OK ({total} trigger{'s' if total != 1 else ''})")
        if cfg.cron:
            print(f"- cron       : {len(cfg.cron)}")
        if cfg.webhook:
            print(f"- webhook    : {len(cfg.webhook)}")
        if cfg.filesystem:
            print(f"- filesystem : {len(cfg.filesystem)}")
        return 0

    triggers_validate.set_defaults(func=_cmd_triggers_validate)

    triggers_run = triggers_sub.add_parser(
        "run",
        help="run trigger adapters and enqueue events",
    )
    triggers_run.add_argument(
        "--config",
        default=_DEFAULT_TRIGGER_CONFIG,
        help="trigger config file (default: triggers.yaml)",
    )
    triggers_run.add_argument(
        "--queue-dir",
        default="data/queue",
        help="queue directory (file backend)",
    )
    triggers_run.add_argument(
        "--idempotency-dir",
        default="data/idempotency",
        help="idempotency directory",
    )
    triggers_run.add_argument(
        "--dlq-dir",
        default="data/dlq",
        help="dead letter queue directory",
    )

    def _cmd_triggers_run(args):
        try:
            run_triggers(
                config_path=args.config,
                queue_dir=args.queue_dir,
                idempotency_dir=args.idempotency_dir,
                dlq_dir=args.dlq_dir,
            )
        except TriggerConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    triggers_run.set_defaults(func=_cmd_triggers_run)

    workers_parser = sub.add_parser(
        "workers",
        help="manage worker processes",
        description="Start, monitor, and gracefully stop worker pools.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:\n  tm workers start -n 4 --queue file --lease-ms 30000\n  tm workers stop""",
    )
    workers_sub = workers_parser.add_subparsers(dest="wcmd")
    workers_sub.required = True

    workers_start = workers_sub.add_parser(
        "start",
        help="start worker pool",
        description="Launch a pool of TraceMind workers and keep it running until signalled.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm workers start -n 4 --queue file --lease-ms 30000""",
    )
    workers_start.add_argument(
        "-n", "--num", dest="worker_count", type=int, default=1, help="number of worker processes"
    )
    workers_start.add_argument("--queue", choices=["file", "memory"], default="file", help="queue backend")
    workers_start.add_argument("--queue-dir", default="data/queue", help="queue directory (file backend)")
    workers_start.add_argument("--idempotency-dir", default="data/idempotency", help="idempotency cache directory")
    workers_start.add_argument("--dlq-dir", default="data/dlq", help="dead letter queue directory")
    workers_start.add_argument(
        "--runtime",
        default="tm.app.wiring_flows:_runtime",
        help="runtime factory in module:attr format",
    )
    workers_start.add_argument("--lease-ms", type=int, default=30_000, help="lease duration in milliseconds")
    workers_start.add_argument("--batch", type=int, default=1, help="tasks to lease per fetch")
    workers_start.add_argument("--poll", type=float, default=0.5, help="poll interval when idle (seconds)")
    workers_start.add_argument("--heartbeat", type=float, default=5.0, help="heartbeat interval (seconds)")
    workers_start.add_argument(
        "--heartbeat-timeout", type=float, default=15.0, help="heartbeat timeout before restart (seconds)"
    )
    workers_start.add_argument("--result-ttl", type=float, default=3600.0, help="idempotency result TTL (seconds)")
    workers_start.add_argument("--config", help="config file for retry policies", default="trace_config.toml")
    workers_start.add_argument("--drain-grace", type=float, default=10.0, help="grace period (s) when draining")
    workers_start.add_argument(
        "--pid-file",
        default="tm-workers.pid",
        help="write supervisor PID for tm workers stop",
    )

    def _cmd_workers_start(args):
        queue_dir = Path(args.queue_dir).resolve()
        queue_dir.mkdir(parents=True, exist_ok=True)
        idem_dir = Path(args.idempotency_dir).resolve()
        idem_dir.mkdir(parents=True, exist_ok=True)
        dlq_dir = Path(args.dlq_dir).resolve()
        dlq_dir.mkdir(parents=True, exist_ok=True)

        opts = WorkerOptions(
            worker_count=args.worker_count,
            queue_backend=args.queue,
            queue_dir=str(queue_dir),
            idempotency_dir=str(idem_dir),
            dlq_dir=str(dlq_dir),
            runtime_spec=args.runtime,
            lease_ms=args.lease_ms,
            batch_size=args.batch,
            poll_interval=args.poll,
            heartbeat_interval=args.heartbeat,
            heartbeat_timeout=args.heartbeat_timeout,
            result_ttl=args.result_ttl,
            config_path=str(Path(args.config).resolve()) if args.config else None,
            drain_grace=args.drain_grace,
        )
        supervisor = TaskWorkerSupervisor(opts)
        install_signal_handlers(supervisor)
        pid_path = Path(args.pid_file).resolve()
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            print(f"warning: failed to write pid file at {pid_path}", file=sys.stderr)
        else:
            print(f"worker supervisor running (pid {os.getpid()}); ctrl-c or 'tm workers stop' to drain")
        try:
            supervisor.run_forever()
        finally:
            try:
                pid_path.unlink(missing_ok=True)
            except Exception:
                pass

    workers_start.set_defaults(func=_cmd_workers_start)

    workers_stop = workers_sub.add_parser(
        "stop",
        help="signal workers to drain",
        description="Send SIGTERM to the running worker supervisor so it drains and exits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm workers stop""",
    )
    workers_stop.add_argument(
        "--pid-file",
        default="tm-workers.pid",
        help="PID file written by 'tm workers start'",
    )
    workers_stop.add_argument(
        "--signal",
        choices=["TERM", "INT"],
        default="TERM",
        help="signal to send (default: TERM)",
    )

    def _cmd_workers_stop(args):
        pid_path = Path(args.pid_file).resolve()
        if not pid_path.exists():
            print(f"pid file not found at {pid_path}", file=sys.stderr)
            sys.exit(1)
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except Exception as exc:
            print(f"failed to read pid from {pid_path}: {exc}", file=sys.stderr)
            sys.exit(1)
        sig = signal.SIGTERM if args.signal == "TERM" else signal.SIGINT
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            print(f"no process with pid {pid}")
            try:
                pid_path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        print(f"sent SIG{args.signal} to worker supervisor (pid {pid})")

    workers_stop.set_defaults(func=_cmd_workers_stop)

    queue_parser = sub.add_parser(
        "queue",
        help="queue utilities",
        description="Inspect queue state without poking running workers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm queue stats --queue file""",
    )
    queue_sub = queue_parser.add_subparsers(dest="qcmd")
    queue_sub.required = True

    queue_stats = queue_sub.add_parser(
        "stats",
        help="show queue metrics",
        description="Inspect queue depth, inflight tasks, and lag without leasing new work.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm queue stats --queue file""",
    )
    queue_stats.add_argument("--queue", choices=["file", "memory"], default="file", help="queue backend")
    queue_stats.add_argument("--queue-dir", default="data/queue", help="queue directory (file backend)")
    queue_stats.add_argument("--json", action="store_true", help="emit JSON instead of text")

    def _cmd_queue_stats(args):
        now = time.monotonic()
        if args.queue == "file":
            queue_dir = Path(args.queue_dir).resolve()
            queue_dir.mkdir(parents=True, exist_ok=True)
            queue = FileWorkQueue(str(queue_dir))
        else:
            queue = InMemoryWorkQueue()
        try:
            depth = queue.pending_count()
            oldest = queue.oldest_available_at()
            lag = max(0.0, now - oldest) if oldest is not None else 0.0
            entries = getattr(queue, "_entries", {})
            inflight = 0
            if isinstance(entries, Mapping):
                inflight = sum(1 for entry in entries.values() if getattr(entry, "token", None))
            ready = max(0, depth - inflight)
            stats = {
                "backend": args.queue,
                "depth": depth,
                "ready": ready,
                "inflight": inflight,
                "lag_seconds": lag,
            }
            if args.json:
                print(json.dumps(stats, ensure_ascii=False, indent=2))
            else:
                print(f"backend       : {stats['backend']}")
                print(f"depth         : {stats['depth']}")
                print(f"ready         : {stats['ready']}")
                print(f"inflight      : {stats['inflight']}")
                print(f"lag_seconds   : {stats['lag_seconds']:.3f}")
        finally:
            if args.queue == "file":
                queue.close()

    queue_stats.set_defaults(func=_cmd_queue_stats)

    dlq_parser = sub.add_parser(
        "dlq",
        help="dead letter queue tools",
        description="Inspect, requeue, or purge entries in the DLQ.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:\n  tm dlq ls --limit 5\n  tm dlq requeue dlq-1700*""",
    )
    dlq_sub = dlq_parser.add_subparsers(dest="dlqcmd")
    dlq_sub.required = True

    dlq_ls = dlq_sub.add_parser(
        "ls",
        help="list DLQ entries",
        description="Print pending dead letter entries for inspection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm dlq ls --since 15m --limit 5""",
    )
    dlq_ls.add_argument("--dlq-dir", default="data/dlq", help="dead letter directory")
    dlq_ls.add_argument("--limit", type=int, default=20, help="maximum entries to display")
    dlq_ls.add_argument("--since", help="only include entries newer than duration (e.g. 10m, 1h)")

    def _cmd_dlq_ls(args):
        store = DeadLetterStore(args.dlq_dir)
        count = 0
        since_cutoff = None
        if args.since:
            try:
                since_cutoff = time.time() - _parse_duration(args.since).total_seconds()
            except Exception as exc:
                print(f"invalid --since value: {exc}", file=sys.stderr)
                sys.exit(1)
        for record in store.list():
            if since_cutoff is not None and record.timestamp < since_cutoff:
                continue
            print(
                json.dumps(
                    {
                        "entry_id": record.entry_id,
                        "flow_id": record.flow_id,
                        "attempt": record.attempt,
                        "timestamp": record.timestamp,
                        "error": record.error,
                    },
                    ensure_ascii=False,
                )
            )
            count += 1
            if args.limit and count >= args.limit:
                break

    dlq_ls.set_defaults(func=_cmd_dlq_ls)

    dlq_requeue = dlq_sub.add_parser(
        "requeue",
        help="requeue DLQ entries",
        description="Return one or more DLQ entries to the work queue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm dlq requeue dlq-1700* --dlq-dir data/dlq""",
    )
    dlq_requeue.add_argument("pattern", help="Entry id or glob-style pattern")
    dlq_requeue.add_argument("--dlq-dir", default="data/dlq")
    dlq_requeue.add_argument("--queue-dir", default="data/queue")
    dlq_requeue.add_argument("--idempotency-dir", default="data/idempotency")
    dlq_requeue.add_argument("--config", default="trace_config.toml")
    dlq_requeue.add_argument("--all", action="store_true", help="requeue all matching entries")

    def _cmd_dlq_requeue(args):
        store = DeadLetterStore(args.dlq_dir)
        matches = [
            record
            for record in store.list()
            if record.entry_id == args.pattern or fnmatch.fnmatch(record.entry_id, args.pattern)
        ]
        if not matches:
            print(f"no DLQ entries match '{args.pattern}'", file=sys.stderr)
            sys.exit(1)
        if not args.all:
            matches = matches[:1]
        queue = FileWorkQueue(str(Path(args.queue_dir).resolve()))
        idem = IdempotencyStore(dir_path=str(Path(args.idempotency_dir).resolve()))
        policy = load_retry_policy(args.config)
        manager = TaskQueueManager(queue, idem, retry_policy=policy)
        try:
            for record in matches:
                headers = dict(record.task.get("headers", {})) if isinstance(record.task, Mapping) else {}
                trace = record.task.get("trace") if isinstance(record.task, Mapping) else {}
                outcome = manager.enqueue(
                    flow_id=record.flow_id,
                    input=record.task.get("input", {}),
                    headers=headers,
                    trace=trace if isinstance(trace, Mapping) else {},
                )
                if outcome.envelope:
                    print(f"requeued {record.entry_id} -> task {outcome.envelope.task_id}")
                else:
                    print(f"skipped {record.entry_id} (duplicate)")
                store.consume(record.entry_id, state="requeued")
        finally:
            queue.flush()
            queue.close()

    dlq_requeue.set_defaults(func=_cmd_dlq_requeue)

    dlq_purge = dlq_sub.add_parser(
        "purge",
        help="purge DLQ entries",
        description="Permanently archive matching DLQ entries after confirmation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:\n  tm dlq purge dlq-1700* --yes""",
    )
    dlq_purge.add_argument("pattern", help="Entry id or glob-style pattern")
    dlq_purge.add_argument("--dlq-dir", default="data/dlq")
    dlq_purge.add_argument("--yes", action="store_true", help="skip confirmation prompt")

    def _cmd_dlq_purge(args):
        store = DeadLetterStore(args.dlq_dir)
        matches = [
            record.entry_id
            for record in store.list()
            if record.entry_id == args.pattern or fnmatch.fnmatch(record.entry_id, args.pattern)
        ]
        if not matches:
            print(f"no DLQ entries match '{args.pattern}'", file=sys.stderr)
            sys.exit(1)
        if not args.yes:
            prompt = (
                f"Permanently purge {len(matches)} entr{'y' if len(matches) == 1 else 'ies'}? type 'purge' to confirm: "
            )
            response = input(prompt)
            if response.strip().lower() != "purge":
                print("aborted")
                return
        for entry_id in matches:
            record = store.consume(entry_id, state="purged")
            if record is None:
                print(f"entry '{entry_id}' already handled")
            else:
                print(f"purged {entry_id}")

    dlq_purge.set_defaults(func=_cmd_dlq_purge)

    register_dsl_commands(sub)
    register_flow_commands(sub)
    register_validate_command(sub)
    register_fmt_command(sub)
    register_simulate_command(sub)
    register_caps_commands(sub)
    register_intent_commands(sub)
    register_intents_commands(sub)
    register_pattern_commands(sub)
    register_artifacts_commands(sub)
    register_plan_commands(sub)
    register_run_commands(sub)
    register_replay_commands(sub)
    register_controller_commands(sub)
    register_tests_commands(sub)
    register_proposal_commands(sub)
    register_gate_commands(sub)
    register_ci_commands(sub)

    return parser


def _cmd_runtime_run_ir(args):
    manifest_path = Path(args.manifest)
    inputs = _load_json_arg(getattr(args, "inputs", None), default={})
    try:
        result = run_ir_flow(args.flow, manifest_path=manifest_path, inputs=inputs)
    except IrRunnerError as exc:
        print(f"runtime run failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "flow": args.flow,
        "status": result.status,
        "summary": result.summary or {},
        "events": list(result.events),
    }
    print(json.dumps(payload, indent=2))
    return 0 if result.status == "completed" else 1


def _cmd_verify_workflow(args):
    try:
        workflow = _load_structured_file(Path(args.workflow))
        policy = _load_structured_file(Path(args.policy))
        capabilities = [_load_structured_file(Path(cap)) for cap in args.capabilities]
    except Exception as exc:
        print(f"verify workflow: failed to load inputs: {exc}", file=sys.stderr)
        raise SystemExit(1)

    report = WorkflowVerifier(policy=policy, capabilities=capabilities).verify(workflow)
    if report.success:
        payload = {"success": True}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("workflow verification succeeded")
        return 0

    counterexample = report.counterexample or {}
    payload = {"success": False, "counterexample": counterexample}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("workflow verification failed")
        print(f"violated invariant: {counterexample.get('violated_invariant')}")
        print(f"condition: {counterexample.get('condition')}")
        print("trace:")
        for step in counterexample.get("steps", []):
            print(f"  {step.get('step_id')} ({step.get('capability_id')}) -> {step.get('state_snapshot')}")
    return 1


def _load_structured_file(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML files")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    return payload


def _parse_guard_decisions(values: Iterable[str] | None) -> dict[str, bool]:
    decisions: dict[str, bool] = {}
    for raw in values or []:
        if "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        decisions[name.strip()] = value.strip().lower() in {"1", "true", "yes", "on"}
    return decisions


def _cmd_runtime_run_workflow(args):
    try:
        workflow = _load_structured_file(Path(args.workflow))
        policy = _load_structured_file(Path(args.policy))
        capabilities = [_load_structured_file(Path(cap)) for cap in args.capabilities]
    except Exception as exc:
        print(f"runtime run-workflow: failed to load inputs: {exc}", file=sys.stderr)
        raise SystemExit(1)

    guard_decisions = _parse_guard_decisions(args.guard_decision)
    try:
        trace = execute_workflow(
            workflow,
            policy=policy,
            capabilities=capabilities,
            guard_decisions=guard_decisions,
            events=args.events or [],
        )
    except WorkflowVerificationError as exc:
        counterexample = exc.report.counterexample or {}
        payload = {"success": False, "counterexample": counterexample}
        if args.format == "json":
            print(json.dumps(payload, indent=2))
        else:
            print("workflow verification failed")
            print(f"violated invariant: {counterexample.get('violated_invariant')}")
            print(f"condition: {counterexample.get('condition')}")
            print("trace:")
            for step in counterexample.get("steps", []):
                print(f"  {step.get('step_id')} ({step.get('capability_id')}) -> {step.get('state_snapshot')}")
        return 1
    except WorkflowExecutionError as exc:
        print(f"runtime run-workflow: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(trace, indent=2, ensure_ascii=False))
    return 0


def _cmd_runtime_report_state(args):
    try:
        workflow = _load_structured_file(Path(args.workflow))
        policy = _load_structured_file(Path(args.policy))
        capabilities = [_load_structured_file(Path(cap)) for cap in args.capabilities]
        trace = _load_structured_file(Path(args.trace))
    except Exception as exc:
        print(f"runtime report-state: failed to load inputs: {exc}", file=sys.stderr)
        raise SystemExit(1)

    try:
        report = build_integrated_state_report(
            trace,
            workflow=workflow,
            policy=policy,
            capabilities=capabilities,
        )
    except IntegratedStateReportError as exc:
        print(f"runtime report-state: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.runtime_config_path).expanduser() if args.runtime_config_path else None
        runtime_cfg = load_runtime_config(config_path)
        engine_override = args.engine
        executor_override = args.executor_path
        if engine_override or executor_override:
            executor_path = runtime_cfg.executor_path
            if executor_override:
                executor_path = Path(executor_override).expanduser()
            runtime_cfg = replace(
                runtime_cfg,
                engine=engine_override or runtime_cfg.engine,
                executor_path=executor_path,
            )
        configure_engine(runtime_cfg)
        setattr(args, "_runtime_config", runtime_cfg)
    except RuntimeConfigError as exc:
        print(f"runtime configuration error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"runtime configuration error: {exc}", file=sys.stderr)
        return 1

    if hasattr(args, "func"):
        result = args.func(args)
        if isinstance(result, int):
            return result
    # return 0 even when no subcommand to mirror previous behavior
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
