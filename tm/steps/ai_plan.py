from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from tm.ai.llm_client import make_client
from tm.ai.plan import PLAN_VERSION, Plan, PlanValidationError, validate_plan
from tm.ai.registry import PolicyForbiddenError
from tm.ai.providers.base import LlmError
from tm.obs.recorder import Recorder

STEP_NAME = "ai.plan"


@dataclass
class _PlanRequest:
    provider: str
    model: str
    goal: str
    context: Optional[str]
    constraints: Mapping[str, Any]
    allow_tools: tuple[str, ...]
    allow_flows: tuple[str, ...]
    timeout_ms: Optional[int]
    retries: int
    retry_backoff_ms: int
    temperature: Optional[float]
    top_p: Optional[float]

    def build_prompt(self) -> str:
        sections: list[str] = []
        sections.append(
            "You are TraceMind Planner."
            " Craft an execution plan using only the provided tools and flows."
            " Respond with valid JSON only; do not include commentary, markdown, or explanations."
        )
        sections.append(
            "Output schema:\n"
            "{\n"
            '  "version": "plan.v1",\n'
            '  "goal": string,\n'
            '  "constraints": object (with "max_steps" and/or "budget_usd"),\n'
            '  "allow": {"tools": [...], "flows": [...]},\n'
            '  "steps": [\n'
            '    {"id": string, "kind": "tool"|"flow", "ref": string, "inputs": object, "on_error": {optional}}\n'
            "  ]\n"
            "}\n"
        )
        sections.append(f"Goal: {self.goal}")
        if self.context:
            sections.append(f"Context: {self.context}")
        if self.constraints:
            sections.append("Constraints: " + json.dumps(self.constraints, ensure_ascii=False, separators=(",", ":")))
        sections.append("Allowed tools: " + (", ".join(self.allow_tools) if self.allow_tools else "none"))
        sections.append("Allowed flows: " + (", ".join(self.allow_flows) if self.allow_flows else "none"))
        sections.append("Rules: steps[*].ref must exist in the allow list. Use compact JSON with double quotes.")
        return "\n\n".join(sections)


def _normalize_id_sequence(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array of strings")
    result: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{label} entries must be non-empty strings")
        normalized = entry.strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _parse_request(params: Mapping[str, Any]) -> _PlanRequest:
    provider = str(params.get("provider", "")).strip()
    if not provider:
        raise ValueError("provider is required")

    model = str(params.get("model", "")).strip()
    if not model:
        raise ValueError("model is required")

    goal = str(params.get("goal", "")).strip()
    if not goal:
        raise ValueError("goal is required")

    context_raw = params.get("context")
    context: Optional[str] = None
    if context_raw is not None:
        if isinstance(context_raw, (dict, list, tuple)):
            context = json.dumps(context_raw, ensure_ascii=False, separators=(",", ":"))
        else:
            context = str(context_raw)

    constraints_raw = params.get("constraints") or {}
    if not isinstance(constraints_raw, Mapping):
        raise ValueError("constraints must be an object when provided")

    allow_raw = params.get("allow") or {}
    if not isinstance(allow_raw, Mapping):
        raise ValueError("allow must be an object with tools/flows lists")
    allow_tools = _normalize_id_sequence(allow_raw.get("tools"), "allow.tools")
    allow_flows = _normalize_id_sequence(allow_raw.get("flows"), "allow.flows")

    timeout_ms = params.get("timeout_ms")
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or timeout_ms < 0:
            raise ValueError("timeout_ms must be a non-negative integer")

    retries = params.get("retries", 0)
    if not isinstance(retries, int) or retries < 0:
        raise ValueError("retries must be a non-negative integer")

    retry_backoff_ms = params.get("retry_backoff_ms", 500)
    if not isinstance(retry_backoff_ms, int) or retry_backoff_ms < 0:
        raise ValueError("retry_backoff_ms must be a non-negative integer")

    temperature = params.get("temperature")
    if temperature is not None:
        temperature = float(temperature)

    top_p = params.get("top_p")
    if top_p is not None:
        top_p = float(top_p)

    return _PlanRequest(
        provider=provider,
        model=model,
        goal=goal,
        context=context,
        constraints=dict(constraints_raw),
        allow_tools=allow_tools,
        allow_flows=allow_flows,
        timeout_ms=timeout_ms,
        retries=retries,
        retry_backoff_ms=retry_backoff_ms,
        temperature=temperature,
        top_p=top_p,
    )


def _is_retryable_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    return code in {"RATE_LIMIT", "PROVIDER_ERROR", "RUN_TIMEOUT"}


def _ensure_plan_allows(plan: Plan, request: _PlanRequest) -> None:
    allowed_tools = set(request.allow_tools)
    allowed_flows = set(request.allow_flows)

    plan_tools = set(plan.allow.tools)
    plan_flows = set(plan.allow.flows)

    if plan_tools - allowed_tools:
        raise PolicyForbiddenError(kind="tool", ref=sorted(plan_tools - allowed_tools)[0])
    if plan_flows - allowed_flows:
        raise PolicyForbiddenError(kind="flow", ref=sorted(plan_flows - allowed_flows)[0])

    for step in plan.steps:
        if step.kind == "tool" and step.ref not in allowed_tools:
            raise PolicyForbiddenError(kind="tool", ref=step.ref)
        if step.kind == "flow" and step.ref not in allowed_flows:
            raise PolicyForbiddenError(kind="flow", ref=step.ref)


async def run(params: dict[str, Any], *, flow_id: Optional[str] = None, step_id: Optional[str] = None) -> dict:
    try:
        request = _parse_request(params)
    except ValueError as exc:
        return {"status": "error", "error_code": "BAD_REQUEST", "reason": str(exc)}

    recorder = Recorder.default()
    client = make_client(request.provider)
    prompt = request.build_prompt()

    attempts = request.retries + 1
    attempt = 0
    retries_done = 0
    plan_obj: Optional[Plan] = None
    call_result: Optional[Any] = None
    error_code = "GUARD_BLOCKED"
    error_reason = "invalid response"

    overall_start = time.perf_counter()
    while attempt < attempts:
        attempt += 1
        try:
            call_result = await client.call(
                model=request.model,
                prompt=prompt,
                temperature=request.temperature,
                top_p=request.top_p,
                timeout_ms=request.timeout_ms,
                max_retries=0,
            )
        except LlmError as exc:
            error_code = exc.code
            error_reason = exc.message
            if attempt < attempts and _is_retryable_error(exc):
                retries_done += 1
                await asyncio.sleep(request.retry_backoff_ms / 1000.0)
                continue
            duration_ms = (time.perf_counter() - overall_start) * 1000.0
            recorder.on_plan_result(
                provider=request.provider,
                model=request.model,
                duration_ms=duration_ms,
                steps=0,
                retries=retries_done,
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                status="error",
            )
            return {"status": "error", "error_code": error_code, "reason": error_reason}
        except Exception as exc:  # pragma: no cover - defensive
            error_code = "PROVIDER_ERROR"
            error_reason = str(exc)
            duration_ms = (time.perf_counter() - overall_start) * 1000.0
            recorder.on_plan_result(
                provider=request.provider,
                model=request.model,
                duration_ms=duration_ms,
                steps=0,
                retries=retries_done,
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                status="error",
            )
            return {"status": "error", "error_code": error_code, "reason": error_reason}

        text = call_result.output_text.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            error_code = "GUARD_BLOCKED"
            error_reason = "planner output was not valid JSON"
            if attempt < attempts:
                retries_done += 1
                await asyncio.sleep(request.retry_backoff_ms / 1000.0)
                continue
            break

        try:
            plan_obj = validate_plan(payload)
        except PlanValidationError as exc:
            plan_obj = None
            error_code = "GUARD_BLOCKED"
            error_reason = str(exc)
            if attempt < attempts:
                retries_done += 1
                await asyncio.sleep(request.retry_backoff_ms / 1000.0)
                continue
            break

        try:
            _ensure_plan_allows(plan_obj, request)
        except PolicyForbiddenError as exc:
            plan_obj = None
            error_code = exc.error_code
            error_reason = str(exc)
            break

        break

    duration_ms = (time.perf_counter() - overall_start) * 1000.0

    if plan_obj is None or call_result is None:
        recorder.on_plan_result(
            provider=request.provider,
            model=request.model,
            duration_ms=duration_ms,
            steps=0,
            retries=retries_done,
            tokens_in=0,
            tokens_out=0,
            cost_usd=None,
            status="error",
        )
        return {"status": "error", "error_code": error_code, "reason": error_reason}

    plan_dict = plan_obj.as_dict()
    plan_dict["version"] = PLAN_VERSION
    plan_json = json.dumps(plan_dict, separators=(",", ":"), ensure_ascii=False)

    usage = call_result.usage
    recorder.on_plan_result(
        provider=request.provider,
        model=request.model,
        duration_ms=duration_ms,
        steps=len(plan_obj.steps),
        retries=retries_done,
        tokens_in=getattr(usage, "prompt_tokens", 0),
        tokens_out=getattr(usage, "completion_tokens", 0),
        cost_usd=getattr(usage, "cost_usd", None),
        status="ok",
    )

    return {
        "status": "ok",
        "plan": plan_dict,
        "plan_json": plan_json,
        "planning_ms": duration_ms,
        "retries": retries_done,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": usage.cost_usd,
        },
    }


try:  # pragma: no cover - optional auto-registration
    from tm.steps.registry import register_step

    register_step(STEP_NAME, run)
except Exception:
    pass
