from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Tuple, cast
from urllib import request

from tm.flow.runtime import FlowRunRecord
from tm.ai.tuner import BanditTuner

logger = logging.getLogger(__name__)


class McpError(RuntimeError):
    pass


@dataclass
class PolicyDecision:
    arms: Sequence[str]
    remote_version: Optional[str]
    fallback: bool


@dataclass
class BindingPolicy:
    endpoint: Optional[str] = None
    policy_ref: Optional[str] = None


class AsyncMcpClient:
    """Async-friendly MCP client with retry and backoff."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        transport: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]] | Dict[str, Any]]] = None,
        timeout: float = 2.0,
        retries: int = 2,
        backoff: float = 0.25,
    ) -> None:
        if base_url is None and transport is None:
            raise ValueError("Either base_url or transport must be provided")
        self._base_url = base_url
        self._transport = transport
        self._timeout = max(0.1, float(timeout))
        self._retries = max(0, int(retries))
        self._backoff = max(0.0, float(backoff))

    async def call(self, tool: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": f"{tool}.{method}",
            "params": params,
        }

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self._retries:
            try:
                if self._transport is not None:
                    result = await _maybe_await(self._transport(payload))
                else:
                    result = await self._http_call(payload)
            except Exception as exc:  # pragma: no cover - network/transport errors
                last_exc = exc
                if attempt == self._retries:
                    break
                await asyncio.sleep(self._backoff * (2**attempt))
                attempt += 1
                continue

            if "error" in result:
                raise McpError(str(result["error"]))
            return result.get("result", {})

        raise McpError(str(last_exc) if last_exc else "Unknown MCP error")

    async def _http_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._base_url is None:  # pragma: no cover - defensive
            raise McpError("HTTP transport not configured")

        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self._base_url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

        loop = asyncio.get_running_loop()
        try:
            body = await asyncio.wait_for(
                loop.run_in_executor(None, _execute_request, req, self._timeout),
                timeout=self._timeout,
            )
        except Exception as exc:  # pragma: no cover - network
            raise McpError(str(exc)) from exc
        return json.loads(body)


def _execute_request(req: request.Request, timeout: float) -> str:
    with request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset)


async def _maybe_await(result: Awaitable[Dict[str, Any]] | Dict[str, Any]) -> Dict[str, Any]:
    if asyncio.iscoroutine(result):
        awaited = await result
        return awaited
    return cast(Dict[str, Any], result)


class PolicyAdapter:
    async def prepare(
        self,
        binding_key: str,
        local_candidates: Sequence[str],
        context: Mapping[str, Any],
        local_version: str,
    ) -> PolicyDecision:
        return PolicyDecision(arms=local_candidates, remote_version=None, fallback=True)

    async def post_run(self, record: FlowRunRecord) -> None:
        return None

    def register_binding(self, binding_key: str, metadata: BindingPolicy) -> None:
        return None


class McpPolicyAdapter(PolicyAdapter):
    def __init__(
        self,
        tuner: BanditTuner,
        client: AsyncMcpClient,
    ) -> None:
        self._tuner = tuner
        self._client = client
        self._bindings: Dict[str, BindingPolicy] = {}
        self._versions: Dict[str, str] = {}

    def register_binding(self, binding_key: str, metadata: BindingPolicy) -> None:
        self._bindings[binding_key] = metadata

    async def prepare(
        self,
        binding_key: str,
        local_candidates: Sequence[str],
        context: Mapping[str, Any],
        local_version: str,
    ) -> PolicyDecision:
        binding_meta = self._bindings.get(binding_key)
        if not binding_meta or not _is_mcp_endpoint(binding_meta.endpoint):
            return PolicyDecision(arms=list(local_candidates), remote_version=None, fallback=True)

        endpoint = binding_meta.endpoint
        if not isinstance(endpoint, str):
            return PolicyDecision(arms=list(local_candidates), remote_version=None, fallback=True)
        tool = _parse_tool(endpoint)
        try:
            remote_version = await self._fetch_policy(
                binding_key, tool, binding_meta, local_candidates, context, local_version
            )
            arms = await self._fetch_arms(binding_key, tool, binding_meta, local_candidates, context)
            return PolicyDecision(arms=arms, remote_version=remote_version, fallback=False)
        except Exception as exc:  # pragma: no cover - network/transport errors
            logger.warning("policy adapter fallback for %s: %s", binding_key, exc)
            return PolicyDecision(arms=list(local_candidates), remote_version=None, fallback=True)

    async def post_run(self, record: FlowRunRecord) -> None:
        if not record.binding:
            return
        binding_meta = self._bindings.get(record.binding)
        if not binding_meta or not _is_mcp_endpoint(binding_meta.endpoint):
            return
        endpoint = binding_meta.endpoint
        if not isinstance(endpoint, str):
            return
        tool = _parse_tool(endpoint)
        payload = {
            "binding": record.binding,
            "policy_ref": binding_meta.policy_ref,
            "flow": record.selected_flow,
            "run_id": record.run_id,
            "status": record.status,
            "reward": record.reward,
            "duration_ms": record.duration_ms,
            "flow_rev": record.flow_rev,
            "version": self._versions.get(record.binding),
        }
        try:
            result = await self._client.call(tool, "update", payload)
            params, version = _extract_params(result)
            applied_version = await self._apply_params(record.binding, params, version)
            if applied_version:
                self._versions[record.binding] = applied_version
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("policy.update failed for %s: %s", record.binding, exc)

    async def _fetch_policy(
        self,
        binding_key: str,
        tool: str,
        binding_meta: BindingPolicy,
        local_candidates: Sequence[str],
        context: Mapping[str, Any],
        local_version: str,
    ) -> Optional[str]:
        payload = {
            "binding": binding_key,
            "policy_ref": binding_meta.policy_ref,
            "candidates": list(local_candidates),
            "local_version": local_version,
            "flow_id": context.get("flow_id") or (local_candidates[0] if local_candidates else None),
        }
        result = await self._client.call(tool, "get", payload)
        params, version = _extract_params(result)
        applied_version = await self._apply_params(binding_key, params, version)
        if applied_version:
            self._versions[binding_key] = applied_version
        return applied_version

    async def _fetch_arms(
        self,
        binding_key: str,
        tool: str,
        binding_meta: BindingPolicy,
        local_candidates: Sequence[str],
        context: Mapping[str, Any],
    ) -> Sequence[str]:
        payload = {
            "binding": binding_key,
            "policy_ref": binding_meta.policy_ref,
            "candidates": list(local_candidates),
            "flow_id": context.get("flow_id") or (local_candidates[0] if local_candidates else None),
        }
        result = await self._client.call(tool, "list_arms", payload)
        arms = result.get("arms")
        if isinstance(arms, Sequence) and not isinstance(arms, (str, bytes)) and arms:
            unique = []
            seen = set()
            for arm in arms:
                if isinstance(arm, str) and arm and arm not in seen:
                    unique.append(arm)
                    seen.add(arm)
            if unique:
                return unique
        return local_candidates

    async def _apply_params(self, binding_key: str, params: Mapping[str, Any], version: Optional[str]) -> Optional[str]:
        if not params:
            return version
        try:
            cfg = await self._tuner.configure(binding_key, dict(params), version=version or "remote", source="remote")
        except ValueError as exc:
            logger.warning("policy params rejected for %s: %s", binding_key, exc)
            return None
        return cfg.version


def _is_mcp_endpoint(endpoint: Optional[str]) -> bool:
    return isinstance(endpoint, str) and endpoint.startswith("mcp:")


def _parse_tool(endpoint: str) -> str:
    _, tool = endpoint.split(":", 1)
    return tool


def _extract_params(result: Mapping[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    params = result.get("params") if isinstance(result, Mapping) else {}
    if not isinstance(params, Mapping):
        params = {}
    version = result.get("version") if isinstance(result, Mapping) else None
    if version is not None:
        version = str(version)
    return dict(params), version


__all__ = [
    "AsyncMcpClient",
    "BindingPolicy",
    "McpError",
    "McpPolicyAdapter",
    "PolicyAdapter",
    "PolicyDecision",
]
