import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeout

import pytest

from tm.flow.core import (
    AirflowStyleTracer,
    CheckRegistry,
    Engine,
    ExecContext,
    FlowBase,
    FlowGraph,
    FlowRepo,
    NodeKind,
    OperatorRegistry,
    RetryPolicy,
    StaticAnalyzer,
    Step,
    StepPolicies,
    StepResult,
    TimeoutPolicy,
    _parse_policies_from_cfg,
    build_demo_flow,
    chain,
    checks,
    registry,
)

# ---------------------------------------------------------------------------
# Basic dataclass / enum sanity
# ---------------------------------------------------------------------------


def test_nodekind_members():
    assert {kind.name for kind in NodeKind} == {"TASK", "FINISH", "SWITCH", "PARALLEL"}


def test_step_and_context_defaults():
    step = Step("id", NodeKind.TASK, uses="op")
    ctx = ExecContext(flow_name="flow", trace_id="run")
    assert step.cfg == {}
    assert ctx.vars == {}


# ---------------------------------------------------------------------------
# Operator and check registry behaviour
# ---------------------------------------------------------------------------


def test_operator_registry_register_and_meta():
    reg = OperatorRegistry()

    @reg.operator("test.op")
    def _op(ctx, inputs):
        return {"ok": True}

    assert reg.get("test.op") is _op
    reg.set_meta("test.op", reads=["a"], writes=["b"], externals=["ext"], pure=True)
    meta = reg.meta("test.op")
    assert meta["reads"] == {"a"}
    assert meta["writes"] == {"b"}
    assert meta["externals"] == {"ext"}
    assert meta["pure"] is True
    assert reg.meta("missing") == {"reads": set(), "writes": set(), "externals": set(), "pure": False}

    with pytest.raises(ValueError):

        @reg.operator("test.op")
        def _duplicate(ctx, inputs):
            return inputs

    with pytest.raises(KeyError):
        reg.get("nope")

    with pytest.raises(KeyError):
        reg.set_meta("nope")


def test_check_registry_register_and_get():
    reg = CheckRegistry()
    calls = []

    @reg.check("chk.one")
    def _check(ctx, inputs):
        calls.append(inputs)

    ctx = ExecContext("flow", "run")
    reg.get("chk.one")(ctx, {"x": 1})
    assert calls == [{"x": 1}]

    with pytest.raises(KeyError):
        reg.get("missing")

    with pytest.raises(ValueError):

        @reg.check("chk.one")
        def _dup(ctx, inputs):
            return None


# ---------------------------------------------------------------------------
# FlowGraph utilities & chain
# ---------------------------------------------------------------------------


def test_flowgraph_builders_and_edges():
    graph = FlowGraph("demo")
    task_id = graph.task("t", uses="op", retry={"max_attempts": 2})
    switch_id = graph.switch("s", key_from="$.vars.t.result", default="_DEFAULT")
    parallel_id = graph.parallel("p", uses=["a", "b"], max_workers=3)
    finish_id = graph.finish("end")

    graph.link(task_id, switch_id)
    graph.link_case(switch_id, parallel_id, case=True)
    graph.link_case(switch_id, finish_id, case="_DEFAULT")
    chain(graph, parallel_id, finish_id)

    graph.set_entry(task_id)
    assert graph.entry() == task_id
    assert graph.successors(task_id) == [switch_id]
    assert graph.edge_attr(switch_id, finish_id, "case") == "_DEFAULT"
    assert graph.node(parallel_id).kind is NodeKind.PARALLEL
    assert graph.node(parallel_id).cfg["max_workers"] == 3

    with pytest.raises(ValueError):
        graph.task("t", uses="dup")

    orphan_graph = FlowGraph("orphan")
    orphan_graph.task("a", uses="op")
    orphan_graph._entry = None  # simulate missing entry configuration
    with pytest.raises(RuntimeError):
        orphan_graph.entry()


def test_chain_adds_edges():
    graph = FlowGraph("chain")
    a = graph.finish("a")
    b = graph.finish("b")
    c = graph.finish("c")
    chain(graph, a, b, c)
    assert graph.successors(a) == [b]
    assert graph.successors(b) == [c]


# ---------------------------------------------------------------------------
# FlowBase / FlowRepo
# ---------------------------------------------------------------------------


def test_flowbase_and_flowrepo_registration():
    class GoodFlow(FlowBase):
        name = "good"

        def build(self, **params):
            f = FlowGraph(self.name)
            end = f.finish("end")
            f.set_entry(end)
            return f

    repo = FlowRepo()
    repo.register(GoodFlow)
    assert repo.instantiate("good").name == "good"
    assert repo.list() == ["good"]

    class BadFlow(FlowBase):
        name = ""

        def build(self, **params):
            return FlowGraph("bad")

    with pytest.raises(ValueError):
        repo.register(BadFlow)

    with pytest.raises(ValueError):
        repo.register(GoodFlow)

    with pytest.raises(KeyError):
        repo.instantiate("missing")

    class AbstractFlow(FlowBase):
        name = "abstract"

    with pytest.raises(NotImplementedError):
        AbstractFlow().build()


# ---------------------------------------------------------------------------
# Static analysis scenarios
# ---------------------------------------------------------------------------


def _make_registry_with_ops():
    reg = OperatorRegistry()

    @reg.operator("alpha")
    def _alpha(ctx, inputs):
        return {"value": 1}

    reg.set_meta("alpha", writes=["vars.alpha"], reads=[], externals=[])

    @reg.operator("beta")
    def _beta(ctx, inputs):
        return {"value": 2}

    reg.set_meta("beta", writes=["vars.shared"], reads=[], externals=["ext"])

    @reg.operator("gamma")
    def _gamma(ctx, inputs):
        return {"value": 3}

    reg.set_meta(
        "gamma",
        writes=["vars.shared"],
        reads=["vars.alpha", "vars.shared"],
        externals=["ext"],
    )
    return reg


def test_static_analyzer_detects_issues():
    reg = _make_registry_with_ops()
    analyzer = StaticAnalyzer(reg)

    # Valid flow
    flow = FlowGraph("ok")
    start = flow.task("start", uses="alpha")
    end = flow.finish("end")
    chain(flow, start, end)
    flow.set_entry(start)
    assert analyzer.check(flow) == []

    # Cycle + unreachable + missing operator + switch default + parallel races
    bad_flow = FlowGraph("bad")
    a = bad_flow.task("a", uses="alpha")
    s = bad_flow.switch("s", key_from="$.vars.a.value", default="_DEFAULT")
    p = bad_flow.parallel("p", uses=["beta", "gamma"], max_workers=2)
    bad_flow.task("z", uses="missing")
    end = bad_flow.finish("end")
    chain(bad_flow, a, s)
    bad_flow.link_case(s, p, case=True)
    # no default branch added -> should trigger switch_no_default
    chain(bad_flow, p, end)
    bad_flow.link(end, a)  # create cycle
    bad_flow.task("orphan", uses="alpha")  # unreachable node
    bad_flow.set_entry(a)

    issues = analyzer.check(bad_flow)
    kinds = {issue["kind"] for issue in issues}
    assert kinds >= {
        "cycle",
        "switch_no_default",
        "race_external",
        "race_write_write",
        "race_read_write",
        "operator_missing",
        "unreachable",
    }

    # Missing entry
    no_entry = FlowGraph("no_entry")
    no_entry.task("step", uses="alpha")
    no_entry.set_entry("step")
    no_entry._entry = None
    issues = analyzer.check(no_entry)
    assert any(issue["kind"] == "entry" for issue in issues)


# ---------------------------------------------------------------------------
# Tracer behaviour
# ---------------------------------------------------------------------------


def test_airflow_style_tracer_records_runs():
    tracer = AirflowStyleTracer(xcom_bytes_limit=5)
    run_id = tracer.begin("flow")
    step = Step("task", NodeKind.TASK, uses="op")
    result = StepResult(status="ok", output={"data": "123456"}, duration_ms=10)
    tracer.on_step(run_id, step, result, {"inputs": {}})
    tracer.record_edges(run_id, [("a", "b")])
    tracer.end(run_id, "ok")

    dag_run, task_instances, edges = tracer.get_run(run_id)
    assert dag_run["state"] == "success"
    assert task_instances[0]["out"]["truncated"] is True
    assert edges == [("a", "b")]


# ---------------------------------------------------------------------------
# Policy parsing
# ---------------------------------------------------------------------------


def test_parse_policies_from_cfg():
    pol = _parse_policies_from_cfg(
        {
            "retry": {"max_attempts": 3, "backoff_ms": 50},
            "timeout": {"timeout_ms": 120},
        }
    )
    assert pol.retry.max_attempts == 3
    assert pol.retry.backoff_ms == 50
    assert pol.timeout.timeout_ms == 120

    pol2 = _parse_policies_from_cfg({})
    assert pol2.retry.max_attempts == 1
    assert pol2.timeout.timeout_ms is None


# ---------------------------------------------------------------------------
# Engine internals
# ---------------------------------------------------------------------------


def test_engine_get_path_and_checks():
    root = {"vars": {"task": {"value": 7}}, "inputs": {}, "cfg": {}}
    assert Engine._get_path(root, "$.vars.task.value") == 7
    assert Engine._get_path(root, "not_a_path") == "not_a_path"
    assert Engine._get_path(root, "$.vars.missing") is None

    ctx = ExecContext("flow", "trace")
    recorded = []
    check_name = f"chk.test.{uuid.uuid4().hex}"

    @checks.check(check_name)
    def _remember(ctx_in, call_in):
        recorded.append(call_in)

    Engine()._run_checks([check_name], ctx, {"x": 1})
    assert recorded == [{"x": 1}]


def test_engine_run_operator_success_retry_timeout():
    engine = Engine()
    ctx = ExecContext("flow", "trace")

    retry_name = f"op.retry.{uuid.uuid4().hex}"
    attempts = {"count": 0}

    @registry.operator(retry_name)
    def _sometimes(ctx_in, call_in):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise ValueError("fail once")
        return {"ok": True}

    pol = StepPolicies(retry=RetryPolicy(max_attempts=2, backoff_ms=0), timeout=TimeoutPolicy())
    assert engine._run_operator(retry_name, ctx, {}, pol) == {"ok": True}
    assert attempts["count"] == 2

    fail_name = f"op.fail.{uuid.uuid4().hex}"

    @registry.operator(fail_name)
    def _always_fail(ctx_in, call_in):
        raise ValueError("boom")

    pol_fail = StepPolicies(retry=RetryPolicy(max_attempts=1), timeout=TimeoutPolicy())
    with pytest.raises(ValueError):
        engine._run_operator(fail_name, ctx, {}, pol_fail)

    slow_name = f"op.slow.{uuid.uuid4().hex}"

    @registry.operator(slow_name)
    def _slow(ctx_in, call_in):
        time.sleep(0.02)
        return {}

    pol_timeout = StepPolicies(retry=RetryPolicy(max_attempts=1), timeout=TimeoutPolicy(timeout_ms=1))
    with pytest.raises(FuturesTimeout):
        engine._run_operator(slow_name, ctx, {}, pol_timeout)


# ---------------------------------------------------------------------------
# Engine end-to-end run
# ---------------------------------------------------------------------------


def test_engine_run_success_and_failure_paths():
    flow = build_demo_flow()
    tracer = AirflowStyleTracer()
    engine = Engine(tracer=tracer)

    run_id, vars_out = engine.run(flow, inputs={"payload": {"name": "Ada"}, "x": 2})
    dag_run, task_instances, _ = tracer.get_run(run_id)

    assert dag_run["state"] == "success"
    assert "annotate" in vars_out
    assert any(ti["task_id"] == "fanout" for ti in task_instances)

    # Failure path via missing payload (before-check)
    bad_run_id, _ = engine.run(flow, inputs={"x": 2})
    dag_run_bad, _, _ = tracer.get_run(bad_run_id)
    assert dag_run_bad["state"] == "failed"
