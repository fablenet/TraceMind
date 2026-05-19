"""TraceMind K8s extension — plumbing only.

Stage 5-4 task 4.6 deliverable. This package provides a **scenario-free**
adapter for interacting with a Kubernetes-shaped control surface:

- :class:`FakeKubeApiServer` — an in-memory simulation of the subset of
  the Kubernetes API surface needed to exercise control loops (list /
  patch / scale namespaced resources, observe metrics).
- :class:`K8sObserveAdapter` — wraps the fake server (or any object
  implementing the same protocol) with a TraceMind-friendly
  ``observe(query) → dict`` shape.
- :class:`K8sExecuteAdapter` — wraps the fake server with
  ``execute(action) → dict``, applying patches idempotently.

## Design discipline (Stage 5-4 invariant 6)

This package **must not** contain HPA, fairness, multi-tenant, quota,
or any other scenario-specific logic. Anyone reading this file should
be able to use the adapter for *any* control scenario on a K8s-shaped
surface (DaemonSet rollouts, namespace cleanup, secret rotation, …).
Scenario knowledge belongs in downstream repos (``fablenet-ops`` or
standalone scenario repos).
"""

from .adapters import K8sExecuteAdapter, K8sObserveAdapter
from .fake_apiserver import FakeKubeApiServer, KubeApiError, KubeResource

__all__ = [
    "FakeKubeApiServer",
    "K8sExecuteAdapter",
    "K8sObserveAdapter",
    "KubeApiError",
    "KubeResource",
]
