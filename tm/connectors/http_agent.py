from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from urllib import parse, request, error

from tm.agents.models import AgentSpec
from tm.agents.runtime import RuntimeAgent
from tm.artifacts.normalize import normalize_body

SAFE_METHODS = {"GET", "HEAD"}


@dataclass(frozen=True)
class HttpAllowlistEntry:
    name: str
    url_prefix: str
    methods: set[str]

    def allows(self, method: str, url: str) -> bool:
        canonical = url.lower()
        return canonical.startswith(self.url_prefix.lower()) and method in self.methods


class HttpConnectorAgent(RuntimeAgent):
    """Runtime agent that applies HTTP resource effects."""

    AGENT_ID = "tm-agent/controller-http:0.1"

    def __init__(self, spec: AgentSpec, config: Mapping[str, Any]) -> None:
        super().__init__(spec, config)
        self._allowlist = self._parse_allowlist(self._config.get("allowlist") or [])
        self._timeout_seconds = float(self._config.get("timeout_seconds") or 30.0)

    def run(self, inputs: Mapping[str, Any]) -> Mapping[str, object]:
        snapshot = self._resolve_snapshot(inputs)
        plan = self._resolve_plan(inputs)
        decisions = self._collect_decisions(plan)
        artifact_refs: dict[str, Mapping[str, object]] = {}
        policy_decisions: list[dict[str, object]] = []
        errors: list[str] = []

        for decision in decisions:
            effect_ref = decision["effect_ref"]
            idempotency_key = decision.get("idempotency_key", "")
            try:
                result = self._apply_http_effect(effect_ref, decision["target_state"])
                artifact_refs[effect_ref] = {**result, "status": "applied"}
                policy_decisions.append(
                    {
                        "effect_ref": effect_ref,
                        "allowed": True,
                        "reason": "allowlist",
                        "idempotency_key": idempotency_key,
                    }
                )
            except Exception as exc:
                message = str(exc)
                errors.append(message)
                artifact_refs[effect_ref] = {"status": "failed", "error": message}
                policy_decisions.append(
                    {
                        "effect_ref": effect_ref,
                        "allowed": False,
                        "reason": message,
                        "idempotency_key": idempotency_key,
                    }
                )

        status = self._report_status(errors, len(decisions))
        report = self._build_report(plan, snapshot, status, artifact_refs, policy_decisions, errors)
        self.add_evidence("builtin.http_connector", {"artifact_refs": artifact_refs, "status": status})
        return {
            "artifact:execution.report": report,
            "state:act.result": {"artifact_refs": artifact_refs, "status": status},
        }

    def _parse_allowlist(self, entries: Iterable[Mapping[str, Any]]) -> dict[str, HttpAllowlistEntry]:
        allowlist: dict[str, HttpAllowlistEntry] = {}
        for entry in entries:
            name = entry.get("name")
            prefix = entry.get("url_prefix")
            methods = entry.get("methods", ["GET"])
            if not isinstance(name, str) or not isinstance(prefix, str):
                continue
            method_set = {str(m).upper() for m in methods if isinstance(m, str)}
            if not method_set:
                method_set = {"GET"}
            allowlist[name] = HttpAllowlistEntry(name=name, url_prefix=prefix, methods=method_set)
        return allowlist

    def _collect_decisions(self, plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        decisions_raw = plan.get("decisions")
        if not isinstance(decisions_raw, Sequence) or isinstance(decisions_raw, (str, bytes, bytearray)):
            raise RuntimeError("plan.decisions must be a non-string sequence")
        decisions: list[Mapping[str, Any]] = []

        for entry in decisions_raw:
            if not isinstance(entry, Mapping):
                raise RuntimeError("each plan decision must be a mapping")
            effect_ref = entry.get("effect_ref")
            target_state = entry.get("target_state")
            if not isinstance(effect_ref, str) or not effect_ref:
                raise RuntimeError("decision.effect_ref must be a non-empty string")
            if not isinstance(target_state, Mapping):
                raise RuntimeError("decision.target_state must be a mapping")
            decisions.append(
                {
                    "effect_ref": effect_ref,
                    "target_state": dict(target_state),
                    "idempotency_key": entry.get("idempotency_key", ""),
                }
            )
        return decisions

    def _resolve_snapshot(self, inputs: Mapping[str, object]) -> Mapping[str, object]:
        payload = inputs.get("state:env.snapshot")
        if not isinstance(payload, Mapping):
            raise RuntimeError("http connector agent requires 'state:env.snapshot'")
        return dict(payload)

    def _resolve_plan(self, inputs: Mapping[str, object]) -> Mapping[str, object]:
        payload = inputs.get("artifact:proposed.plan")
        if not isinstance(payload, Mapping):
            raise RuntimeError("http connector agent requires 'artifact:proposed.plan'")
        return dict(payload)

    def _apply_http_effect(self, effect_ref: str, target_state: Mapping[str, Any]) -> Mapping[str, object]:
        payload = self._normalize_target_state(target_state)
        allowlist_entry = self._select_allowlist(payload)
        method = payload["method"]
        url = self._build_url(payload["url"], payload.get("params"))
        headers = dict(payload.get("headers") or {})
        if _body := payload.get("body_bytes"):
            headers.setdefault("Content-Type", payload.get("content_type") or "application/json")
        if method not in SAFE_METHODS and payload.get("idempotency_key"):
            headers.setdefault("Idempotency-Key", payload["idempotency_key"])
        req = request.Request(url, data=payload.get("body_bytes"), method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as resp:
                status = resp.getcode()
                _response_headers = dict(resp.headers)
                content = resp.read()
        except error.HTTPError as exc:
            status = exc.code
            _response_headers = dict(exc.headers)
            content = exc.read()
        except Exception as exc:
            raise RuntimeError(f"http connector failed: {exc}") from exc

        fingerprint = self._request_fingerprint(method, url)
        response_hash = hashlib.sha256(content).hexdigest() if content else ""
        snippet = content.decode("utf-8", "ignore")[:512]
        evidence_payload = {
            "effect_ref": effect_ref,
            "method": method,
            "url": url,
            "status": status,
            "request_fingerprint": fingerprint,
            "response_hash": response_hash,
            "response_snippet": snippet,
            "allowlist": allowlist_entry.name,
            "idempotency_key": payload.get("idempotency_key"),
        }
        self.add_evidence("builtin.http_connector", evidence_payload)
        return {
            "status": status,
            "url": url,
            "method": method,
            "fingerprint": fingerprint,
            "response_snippet": snippet,
            "response_hash": response_hash,
        }

    def _normalize_target_state(self, target_state: Mapping[str, Any]) -> Mapping[str, Any]:
        method = str(target_state.get("method", "GET")).upper()
        url = target_state.get("url")
        if not isinstance(url, str):
            raise RuntimeError("http target_state requires 'url'")
        headers = target_state.get("headers")
        if headers and not isinstance(headers, Mapping):
            raise RuntimeError("http target_state headers must be a mapping")
        params = target_state.get("params")
        if params and not isinstance(params, Mapping):
            raise RuntimeError("http target_state params must be a mapping")
        body = target_state.get("body")
        body_bytes: bytes | None = None
        content_type: str | None = None
        if body is not None:
            if isinstance(body, (str, bytes)):
                body_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
            else:
                body_bytes = json.dumps(body).encode("utf-8")
                content_type = "application/json"
        return {
            "method": method,
            "url": url,
            "headers": dict(headers) if isinstance(headers, Mapping) else {},
            "params": dict(params) if isinstance(params, Mapping) else {},
            "body_bytes": body_bytes,
            "content_type": content_type,
            "allowlist_key": target_state.get("allowlist_key"),
            "idempotency_key": target_state.get("idempotency_key", ""),
        }

    def _select_allowlist(self, payload: Mapping[str, Any]) -> HttpAllowlistEntry:
        key = payload.get("allowlist_key")
        if not isinstance(key, str) or not key:
            raise RuntimeError("http target_state must include allowlist_key")
        entry = self._allowlist.get(key)
        if entry is None:
            raise RuntimeError(f"unknown allowlist key '{key}'")
        method = payload["method"]
        url = payload["url"]
        if not entry.allows(method, url):
            raise RuntimeError(f"http target '{url}' is not allowed for '{method}'")
        return entry

    def _build_url(self, base: str, params: Mapping[str, Any] | None) -> str:
        parsed = parse.urlparse(base)
        query = parse.parse_qsl(parsed.query, keep_blank_values=True)
        if params:
            query.extend((str(k), str(v)) for k, v in params.items())
        new_query = parse.urlencode(query)
        parsed = parsed._replace(query=new_query)
        return parse.urlunparse(parsed)

    def _request_fingerprint(self, method: str, url: str) -> str:
        parsed = parse.urlparse(url)
        params = parse.parse_qsl(parsed.query, keep_blank_values=True)
        param_names = ",".join(sorted({name for name, _ in params}))
        path = parsed.path or "/"
        return f"{method} {parsed.scheme}://{parsed.netloc}{path}?{param_names}"

    def _report_status(self, errors: list[str], total: int) -> str:
        if not errors:
            return "succeeded"
        if len(errors) == total:
            return "failed"
        return "partial"

    def _build_report(
        self,
        plan: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        status: str,
        artifact_refs: Mapping[str, Mapping[str, object]],
        policy_decisions: Sequence[Mapping[str, object]],
        errors: Sequence[str],
    ) -> Mapping[str, object]:
        report_id = plan.get("plan_id")
        report: MutableMapping[str, object] = {
            "report_id": report_id,
            "artifact_refs": artifact_refs,
            "status": status,
            "policy_decisions": list(policy_decisions),
            "errors": list(errors),
            "artifacts": {
                "HttpConnectorAgent": {
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "requests": artifact_refs,
                }
            },
        }
        report["execution_hash"] = self._hash_execution(report)
        return dict(report)

    def _hash_execution(self, report: Mapping[str, object]) -> str:
        normalized = normalize_body(
            {
                "artifact_refs": report.get("artifact_refs", {}),
                "policy_decisions": report.get("policy_decisions", []),
                "status": report.get("status", ""),
            }
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
