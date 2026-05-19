"""In-memory fake Kubernetes API server.

Only the slice of the K8s API surface needed for control-loop testing
is implemented:

- List / get / create / patch namespaced resources
- Replicate count (scale-like) operations on workload-shaped resources
- Metrics retrieval per resource (a thin in-memory time series)

Resources are stored as plain :class:`KubeResource` dicts; we do **not**
ship the real ``kubernetes`` client library because the goal of the
extension is to be a **simulator**, not a real-cluster bridge.

## Why in-memory?

Phase 5 cross-domain validation aims to prove the Pattern Library is
generic; we *want* to inject pathological scenarios (apiserver
unavailable, partition, metric jitter, quota near-edge) that real
clusters won't reliably reproduce. A fake server is the only way to
hit those corners with CI determinism.

## Determinism

- No real timestamps inside payloads (callers inject ``revision`` /
  ``observed_at`` if they need ordering)
- All operations are synchronous; concurrency is the caller's concern
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional


class KubeApiError(Exception):
    """Raised by :class:`FakeKubeApiServer` to mirror K8s API errors.

    Carries an HTTP-shaped ``status_code`` and a ``reason`` slug so
    adapters can translate to TraceMind error codes consistently.
    """

    def __init__(self, status_code: int, reason: str, message: str = "") -> None:
        super().__init__(f"{status_code} {reason}: {message}")
        self.status_code = status_code
        self.reason = reason
        self.message = message


@dataclass
class KubeResource:
    """Plain-dict view of a namespaced Kubernetes resource.

    Mirror of the structure callers will see over the wire — kind,
    metadata (name/namespace/labels), spec (the desired state), and
    status (observed state).
    """

    kind: str
    namespace: str
    name: str
    spec: Dict[str, Any] = field(default_factory=dict)
    status: Dict[str, Any] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    revision: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "metadata": {
                "namespace": self.namespace,
                "name": self.name,
                "labels": dict(self.labels),
                "resourceVersion": str(self.revision),
            },
            "spec": dict(self.spec),
            "status": dict(self.status),
        }


# ─── Fake API server ──────────────────────────────────────────────


class FakeKubeApiServer:
    """In-memory K8s API simulator.

    Supports:

    - ``list_resources(kind, namespace=None, labels=None)``
    - ``get_resource(kind, namespace, name)``
    - ``create_resource(resource)``
    - ``patch_resource(kind, namespace, name, patch)`` — strategic merge
    - ``scale_resource(kind, namespace, name, replicas)`` — replicates
      common HPA-style scale operations on resources that have a
      ``spec.replicas`` field
    - Metrics: ``record_metric(kind, namespace, name, value, dims=…)``,
      ``query_metrics(kind, namespace, name, *, since=None)``
    - Fault injection hooks (``set_outage`` / ``set_jitter``) so tests
      can exercise partition / apiserver-unavailable corners

    See module docstring for design rationale.
    """

    def __init__(self) -> None:
        self._resources: Dict[tuple[str, str, str], KubeResource] = {}
        self._metrics: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
        self._outage: bool = False
        self._jitter_fn: Optional[Callable[[float], float]] = None

    # ── Fault injection ─────────────────────────────────────────-

    def set_outage(self, outage: bool) -> None:
        """When ``True``, every API call raises a 503 :class:`KubeApiError`."""
        self._outage = bool(outage)

    def set_metric_jitter(self, fn: Optional[Callable[[float], float]]) -> None:
        """Install a jitter function applied to every metric on read.

        Passing ``None`` clears the jitter. Useful for simulating noisy
        metrics pipelines without changing the underlying recorded
        value.
        """
        self._jitter_fn = fn

    # ── Resource CRUD ───────────────────────────────────────────-

    def list_resources(
        self,
        kind: str,
        *,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> List[Dict[str, Any]]:
        self._guard()
        out: List[Dict[str, Any]] = []
        for (k, ns, name), res in sorted(self._resources.items()):
            if k != kind:
                continue
            if namespace is not None and ns != namespace:
                continue
            if labels and not _labels_match(labels, res.labels):
                continue
            out.append(res.to_dict())
        return out

    def get_resource(self, kind: str, namespace: str, name: str) -> Dict[str, Any]:
        self._guard()
        res = self._resources.get((kind, namespace, name))
        if res is None:
            raise KubeApiError(404, "NotFound", f"{kind} {namespace}/{name}")
        return res.to_dict()

    def create_resource(self, resource: KubeResource) -> Dict[str, Any]:
        self._guard()
        key = (resource.kind, resource.namespace, resource.name)
        if key in self._resources:
            raise KubeApiError(409, "AlreadyExists", f"{resource.kind} {key[1]}/{key[2]}")
        stored = copy.deepcopy(resource)
        stored.revision = 1
        self._resources[key] = stored
        return stored.to_dict()

    def patch_resource(
        self,
        kind: str,
        namespace: str,
        name: str,
        patch: Mapping[str, Any],
    ) -> Dict[str, Any]:
        self._guard()
        key = (kind, namespace, name)
        res = self._resources.get(key)
        if res is None:
            raise KubeApiError(404, "NotFound", f"{kind} {namespace}/{name}")
        if "spec" in patch and isinstance(patch["spec"], Mapping):
            res.spec = _strategic_merge(res.spec, patch["spec"])
        if "status" in patch and isinstance(patch["status"], Mapping):
            res.status = _strategic_merge(res.status, patch["status"])
        if "labels" in patch and isinstance(patch["labels"], Mapping):
            res.labels.update({str(k): str(v) for k, v in patch["labels"].items()})
        res.revision += 1
        return res.to_dict()

    def scale_resource(self, kind: str, namespace: str, name: str, replicas: int) -> Dict[str, Any]:
        """Set ``spec.replicas`` to ``replicas`` on a workload-shaped
        resource (Deployment / StatefulSet / ReplicaSet)."""
        self._guard()
        if not isinstance(replicas, int) or replicas < 0:
            raise KubeApiError(400, "Invalid", "replicas must be a non-negative int")
        return self.patch_resource(kind, namespace, name, {"spec": {"replicas": replicas}})

    def delete_resource(self, kind: str, namespace: str, name: str) -> Dict[str, Any]:
        self._guard()
        key = (kind, namespace, name)
        res = self._resources.pop(key, None)
        if res is None:
            raise KubeApiError(404, "NotFound", f"{kind} {namespace}/{name}")
        return res.to_dict()

    # ── Metrics ─────────────────────────────────────────────────-

    def record_metric(
        self,
        kind: str,
        namespace: str,
        name: str,
        value: float,
        *,
        observed_at: str | None = None,
        dims: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a metric point. Caller supplies ``observed_at`` so
        time ordering is deterministic and reproducible."""
        self._guard()
        key = (kind, namespace, name)
        entry: Dict[str, Any] = {"value": float(value)}
        if observed_at is not None:
            entry["observed_at"] = observed_at
        if dims:
            entry["dims"] = dict(dims)
        self._metrics.setdefault(key, []).append(entry)

    def query_metrics(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        since: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Return metric points for a resource, optionally filtered by
        ``observed_at >= since``."""
        self._guard()
        key = (kind, namespace, name)
        entries = list(self._metrics.get(key, []))
        if since is not None:
            entries = [e for e in entries if e.get("observed_at", "") >= since]
        if self._jitter_fn is not None:
            jitter = self._jitter_fn
            entries = [{**e, "value": jitter(float(e["value"]))} for e in entries]
        return entries

    # ── Internals ────────────────────────────────────────────────

    def _guard(self) -> None:
        if self._outage:
            raise KubeApiError(503, "ServiceUnavailable", "fake-apiserver is offline")


# ─── Helpers ──────────────────────────────────────────────────────


def _labels_match(selector: Mapping[str, str], labels: Mapping[str, str]) -> bool:
    """All entries in ``selector`` must be present and equal in ``labels``."""
    for k, v in selector.items():
        if labels.get(k) != v:
            return False
    return True


def _strategic_merge(target: MutableMapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Naive strategic merge: nested dicts merge, lists / scalars replace.

    Sufficient for the small subset of K8s patch semantics this fake
    apiserver supports; production tools should use the official
    ``kubernetes`` library's strategic merge implementation.
    """
    merged: Dict[str, Any] = dict(target)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _strategic_merge(dict(merged[key]), value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
