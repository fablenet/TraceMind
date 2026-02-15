from __future__ import annotations

import textwrap

import pytest

from tm.dsl import (
    DslParseError,
    PdlAssignment,
    PdlChoose,
    PdlChooseOption,
    PdlConditional,
    PdlPolicy,
    WdlCallStep,
    WdlWhenStep,
    parse_pdl_document,
    parse_wdl_document,
)
from tm.dsl.parser import RawScalar


def test_parse_wdl_document_success() -> None:
    workflow = parse_wdl_document(
        textwrap.dedent(
            """
        version: dsl/v0
        workflow: plant-monitor
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
        """,
        )
    )

    assert workflow.version == "dsl/v0"
    assert workflow.name == "plant-monitor"
    assert len(workflow.inputs) == 2
    assert workflow.triggers == ()

    read_step = workflow.steps[0]
    assert isinstance(read_step, WdlCallStep)
    assert read_step.step_id == "read"
    assert read_step.target == "opcua.read"
    assert isinstance(read_step.args[0].value, RawScalar)
    assert read_step.args[0].value.value == "$input.endpoint"

    when_step = workflow.steps[-1]
    assert isinstance(when_step, WdlWhenStep)
    assert when_step.condition.startswith("$step.decide.action")
    nested = when_step.steps[0]
    assert isinstance(nested, WdlCallStep)
    last_arg_value = nested.args[-1].value
    assert isinstance(last_arg_value, RawScalar)
    assert last_arg_value.value == "$step.decide.value"


def test_parse_wdl_missing_steps_raises() -> None:
    with pytest.raises(DslParseError) as excinfo:
        parse_wdl_document(
            textwrap.dedent(
                """
            version: dsl/v0
            workflow: sample
            inputs:
              foo: string
            """,
            ),
            filename="example.wdl",
        )
    err = excinfo.value
    assert "Workflow must define 'steps'" in err.message
    assert err.location is not None
    assert err.location.line == 2  # root mapping location


def test_parse_pdl_document_success() -> None:
    policy = parse_pdl_document(
        textwrap.dedent(
            """
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
        """,
        )
    )

    assert isinstance(policy, PdlPolicy)
    assert policy.version == "pdl/v0"
    assert len(policy.arms) == 1
    assert policy.epsilon == "0.1"
    first_statement = policy.evaluate[0]
    assert isinstance(first_statement, PdlAssignment)
    assert first_statement.target == "temp"
    assert first_statement.operator == ":="
    assert first_statement.statement_id is None

    conditional = policy.evaluate[1]
    assert isinstance(conditional, PdlConditional)
    assert conditional.condition.startswith("temp >=")
    choose_stmt = conditional.body[0]
    assert isinstance(choose_stmt, PdlChoose)
    explore = choose_stmt.options[-1]
    assert isinstance(explore, PdlChooseOption)
    assert explore.probability == "epsilon"
    assert conditional.else_body is not None
    assert len(conditional.else_body) == 1
    else_assignment = conditional.else_body[0]
    assert isinstance(else_assignment, PdlAssignment)
    assert else_assignment.expression == '"NONE"'


def test_parse_pdl_assignment_with_override() -> None:
    policy = parse_pdl_document(
        textwrap.dedent(
            """
        version: pdl/v0
        arms:
          baseline:
            default_action: NONE
        evaluate:
          set_action: action = arms.baseline.default_action
        emit:
          action: action
        """,
        )
    )

    assert len(policy.evaluate) == 1
    statement = policy.evaluate[0]
    assert statement.operator == "="
    assert statement.statement_id == "set_action"
    assert statement.target == "action"
    assert statement.expression == "arms.baseline.default_action"


def test_parse_wdl_with_triggers() -> None:
    workflow = parse_wdl_document(textwrap.dedent("""
        version: dsl/v0
        workflow: triggered
        triggers:
          cron:
            schedule: "* * * * *"
          opcua:
            endpoint: opc.tcp://localhost:4840
            nodes: ["ns=2;i=2"]
        steps:
          - first(op.echo):
              value: 1
        """))

    assert len(workflow.triggers) == 2
    cron_trigger = workflow.triggers[0]
    assert cron_trigger.trigger_type == "cron"
    assert cron_trigger.config.get("schedule") == "* * * * *"
    opcua_trigger = workflow.triggers[1]
    assert opcua_trigger.trigger_type == "opcua"
    assert opcua_trigger.config.get("endpoint") == "opc.tcp://localhost:4840"
