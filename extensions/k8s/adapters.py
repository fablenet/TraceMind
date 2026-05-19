"""Generic observe / execute adapters for K8s-shaped control surfaces.

Stage 5-4 task 4.6 — *plumbing only*. The adapters are deliberately
domain-neutral: they understand kinds, namespaces, replicas, labels,
patches, and metrics, but **never** scenario-level concepts like HPA,
fairness budgets, or quotas.

## Adapter shape

Both adapters expose a single primary method whose I/O is a plain
mapping, mirroring the schema TraceMind step / agent runtimes expect:

- :meth:`K8sObserveAdapter.observe` ``(query) → {resources, metrics}``
- :meth:`K8sExecuteAdapter.execute` ``(action) → {result}``

Why mappings instead of typed payloads? Because the **upstream caller
is a generic MAPE-K agent** (compiled by ``compile_intent_to_bundle``)
which speaks the K-Ontology contract — strings, lists, and dicts. Typed
payloads would require domain-specific schemas, which violates the
plumbing-only rule.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Protocol, runtime_checkable

from .fake_apiserver import KubeApiError, KubeResource


# ─── Server protocol (so anything quack-compatible plugs in) ──────


@runtime_checkable
class KubeApiServerProtocol(Protocol):
    """Subset of the K8s API surface the adapters depend on.

    Implementing this protocol is enough to swap the fake server with a
    real client wrapper if a downstream really wants to talk to a
    cluster — the adapters here don't change.
    """

    def list_resources(
        self,
        kind: str,
        *,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> List[Dict[str, Any]]: ...

    def get_resource(self, kind: str, namespace: str, name: str) -> Dict[str, Any]: ...

    def patch_resource(
        self,
        kind: str,
        namespace: str,
        name: str,
        patch: Mapping[str, Any],
    ) -> Dict[str, Any]: ...

    def scale_resource(self, kind: str, namespace: str, name: str, replicas: int) -> Dict[str, Any]: ...

    def query_metrics(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        since: str | None = None,
    ) -> List[Dict[str, Any]]: ...


# ─── Observe adapter ──────────────────────────────────────────────


class K8sObserveAdapter:
    """Reads resources + metrics from a K8s-shaped API server.

    Construct with either a :class:`FakeKubeApiServer` or any object
    implementing :class:`KubeApiServerProtocol`. The adapter does not
    interpret resources — it returns them verbatim, so downstream
    scenario code can apply its own logic.
    """

    def __init__(self, server: KubeApiServerProtocol) -> None:
        self._server = server

    def observe(self, query: Mapping[str, Any]) -> Dict[str, Any]:
        """Read whatever ``query`` asks for.

        Query schema::

            {
                "resources": [               # optional
                    {"kind": str, "namespace": str|None,
                     "labels": dict|None, "name": str|None}
                ],
                "metrics": [                 # optional
                    {"kind": str, "namespace": str, "name": str,
                     "since": str|None}
                ]
            }

        Returns ``{"resources": [...], "metrics": [...]}`` regardless
        of which sections were queried (empty list for omitted sections).

        Errors from the underlying server are surfaced as a structured
        ``errors`` field rather than raising — this lets the caller's
        MAPE-K analyse stage flag transient outages without crashing
        the whole control loop.
        """
        resources: List[Dict[str, Any]] = []
        metrics: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for q in query.get("resources", []) or []:
            try:
                resources.extend(self._observe_resource(q))
            except KubeApiError as exc:
                errors.append(_error_payload("resource", q, exc))

        for q in query.get("metrics", []) or []:
            try:
                metrics.extend(self._observe_metric(q))
            except KubeApiError as exc:
                errors.append(_error_payload("metric", q, exc))

        result: Dict[str, Any] = {"resources": resources, "metrics": metrics}
        if errors:
            result["errors"] = errors
        return result

    def _observe_resource(self, q: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
        kind = _require_str(q, "kind")
        namespace = q.get("namespace")
        name = q.get("name")
        labels = q.get("labels")
        if name is not None:
            if namespace is None:
                raise KubeApiError(400, "Invalid", "namespace required when name is given")
            return [self._server.get_resource(kind, str(namespace), str(name))]
        return self._server.list_resources(
            kind,
            namespace=str(namespace) if namespace is not None else None,
            labels=dict(labels) if isinstance(labels, Mapping) else None,
        )

    def _observe_metric(self, q: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
        kind = _require_str(q, "kind")
        namespace = _require_str(q, "namespace")
        name = _require_str(q, "name")
        since = q.get("since")
        points = self._server.query_metrics(
            kind,
            namespace,
            name,
            since=str(since) if since is not None else None,
        )
        return [
            {
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "point": p,
            }
            for p in points
        ]


# ─── Execute adapter ──────────────────────────────────────────────


class K8sExecuteAdapter:
    """Applies actions (patch / scale / label) to a K8s-shaped server.

    Idempotency is delegated to the caller via the optional
    ``idempotency_key`` field on each action; the adapter records keys
    it has already applied and returns the cached result for repeats.
    """

    SUPPORTED_KINDS = {"patch", "scale", "label", "delete"}

    def __init__(self, server: KubeApiServerProtocol) -> None:
        self._server = server
        self._applied: Dict[str, Dict[str, Any]] = {}

    def execute(self, action: Mapping[str, Any]) -> Dict[str, Any]:
        """Apply a single action.

        Action schema::

            {
                "kind": "patch" | "scale" | "label" | "delete",
                "resource": {"kind": str, "namespace": str, "name": str},
                "payload": dict,         # operation-specific
                "idempotency_key": str   # optional but encouraged
            }

        Returns ``{"status": "ok", "result": dict}`` on success,
        ``{"status": "error", "error_code": str, "reason": str}`` on
        recognised failures. Unexpected exceptions are not caught —
        the caller's runtime supervisor decides whether to retry.
        """
        try:
            kind = _require_str(action, "kind")
        except KubeApiError as exc:
            return _error_response(exc)
        if kind not in self.SUPPORTED_KINDS:
            return {
                "status": "error",
                "error_code": "UNSUPPORTED_ACTION",
                "reason": f"action kind '{kind}' is not supported",
            }
        idem = action.get("idempotency_key")
        if isinstance(idem, str) and idem in self._applied:
            return self._applied[idem]

        try:
            resource = action.get("resource") or {}
            payload = action.get("payload") or {}
            if not isinstance(resource, Mapping) or not isinstance(payload, Mapping):
                raise KubeApiError(400, "Invalid", "resource / payload must be mappings")
            result = self._dispatch(kind, resource, payload)
        except KubeApiError as exc:
            response = _error_response(exc)
            return response

        response = {"status": "ok", "result": result}
        if isinstance(idem, str) and idem:
            self._applied[idem] = response
        return response

    def _dispatch(
        self,
        kind: str,
        resource: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        r_kind = _require_str(resource, "kind")
        r_namespace = _require_str(resource, "namespace")
        r_name = _require_str(resource, "name")
        if kind == "patch":
            return self._server.patch_resource(r_kind, r_namespace, r_name, payload)
        if kind == "scale":
            replicas = payload.get("replicas")
            if not isinstance(replicas, int):
                raise KubeApiError(400, "Invalid", "payload.replicas must be an int")
            return self._server.scale_resource(r_kind, r_namespace, r_name, replicas)
        if kind == "label":
            labels = payload.get("labels")
            if not isinstance(labels, Mapping):
                raise KubeApiError(400, "Invalid", "payload.labels must be a mapping")
            return self._server.patch_resource(
                r_kind,
                r_namespace,
                r_name,
                {"labels": dict(labels)},
            )
        if kind == "delete":
            return self._server.patch_resource(r_kind, r_namespace, r_name, {"status": {"deleted": True}})
        # _SUPPORTED_KINDS guard above prevents reaching here
        raise KubeApiError(500, "Internal", f"unexpected action kind {kind}")


# ─── Helpers ──────────────────────────────────────────────────────


def _require_str(obj: Mapping[str, Any], field: str) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise KubeApiError(400, "Invalid", f"missing or non-string field '{field}'")
    return value


def _error_payload(section: str, q: Mapping[str, Any], exc: KubeApiError) -> Dict[str, Any]:
    return {
        "section": section,
        "query": dict(q),
        "status_code": exc.status_code,
        "reason": exc.reason,
        "message": exc.message,
    }


def _error_response(exc: KubeApiError) -> Dict[str, Any]:
    return {
        "status": "error",
        "error_code": exc.reason.upper().replace(" ", "_"),
        "status_code": exc.status_code,
        "reason": exc.message or exc.reason,
    }


__all__ = [
    "K8sExecuteAdapter",
    "K8sObserveAdapter",
    "KubeApiServerProtocol",
]


# Silence unused-import warning where KubeResource is re-exported above
_ = KubeResource
