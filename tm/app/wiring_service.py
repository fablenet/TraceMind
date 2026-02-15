from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any

from fastapi import APIRouter, HTTPException

from tm.flow.operations import Operation as FlowStepOp, ResponseMode
from tm.flow.spec import FlowSpec, StepDef
from tm.service import BindingRule, BindingSpec, Operation, ServiceBody
from tm.service.router import OperationRouter
from tm.model.spec import ModelSpec, FieldSpec

from tm.app.wiring_flows import _runtime as flow_runtime, _tuner as flow_tuner, _policy_adapter

router = APIRouter(prefix="/service", tags=["service"])


@dataclass
class _InlineFlow:
    spec_obj: FlowSpec

    @property
    def name(self) -> str:
        return self.spec_obj.name

    def spec(self) -> FlowSpec:
        return self.spec_obj


def _ensure_generic_flow() -> None:
    spec = FlowSpec(name="read-generic")
    spec.add_step(StepDef("finish", FlowStepOp.FINISH))
    try:
        flow_runtime.choose_flow(spec.name)
    except KeyError:
        flow_runtime.register(_InlineFlow(spec))


_ensure_generic_flow()

_model_spec = ModelSpec(
    name="Generic",
    fields=(
        FieldSpec(name="id", type="string"),
        FieldSpec(name="data", type="object", required=False, default={}),
    ),
    allow_extra=True,
)

_binding = BindingSpec(
    model=_model_spec.name,
    rules=[BindingRule(operation=Operation.READ, flow_name="read-generic")],
    policy_endpoint="mcp:policy" if _policy_adapter else None,
    policy_ref=_model_spec.name,
)

_service_body = ServiceBody(
    model=_model_spec,
    runtime=flow_runtime,
    binding=_binding,
    router=OperationRouter(
        flow_runtime,
        {_model_spec.name: _binding},
        tuner=flow_tuner,
        policy_adapter=_policy_adapter,
    ),
)


@router.post("/run")
async def run_service(payload: Dict[str, Any]) -> Dict[str, Any]:
    model = payload.get("model")
    if model != _model_spec.name:
        raise HTTPException(status_code=404, detail="Unknown model")

    op_value = payload.get("op")
    if not isinstance(op_value, str):
        raise HTTPException(status_code=400, detail="Missing op")

    try:
        op = Operation(op_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body = payload.get("payload")
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    try:
        result: Dict[str, object] = await _service_body.handle(op, body, response_mode=ResponseMode.DEFERRED)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status = result.get("status")
    output_raw = result.get("output", {})
    output: Dict[str, object] = output_raw if isinstance(output_raw, dict) else {}

    if status == "rejected":
        raise HTTPException(status_code=429, detail=result.get("error_message", "queue full"))
    if status == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message", "flow error"))

    if output.get("mode") == "deferred":
        if output.get("status") == "pending":
            return {"status": "accepted", "correlationId": output.get("token"), "flow": result.get("flow")}
        if output.get("status") == "ready":
            return {"status": "ok", "result": output.get("result"), "flow": result.get("flow")}

    return {"status": "ok", "flow": result.get("flow"), "result": output}
