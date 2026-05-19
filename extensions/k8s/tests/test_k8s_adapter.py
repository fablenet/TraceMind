"""Tests for the in-tree K8s extension (Stage 5-4 task 4.6).

Covers:
- :class:`FakeKubeApiServer` CRUD + metrics + fault injection
- :class:`K8sObserveAdapter` resource + metric queries
- :class:`K8sExecuteAdapter` patch / scale / label / delete actions
- Idempotency via ``idempotency_key``
- Domain-neutrality lint: no HPA / fairness / quota words in source
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from extensions.k8s import (
    FakeKubeApiServer,
    K8sExecuteAdapter,
    K8sObserveAdapter,
    KubeApiError,
    KubeResource,
)


# ─── FakeKubeApiServer ────────────────────────────────────────────


class TestFakeKubeApiServerCRUD:
    def test_create_and_get_resource(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="Deployment",
                namespace="ns1",
                name="app",
                spec={"replicas": 3},
            )
        )
        d = s.get_resource("Deployment", "ns1", "app")
        assert d["metadata"]["name"] == "app"
        assert d["spec"]["replicas"] == 3

    def test_create_duplicate_raises(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(KubeResource(kind="X", namespace="n", name="a"))
        with pytest.raises(KubeApiError) as exc:
            s.create_resource(KubeResource(kind="X", namespace="n", name="a"))
        assert exc.value.status_code == 409

    def test_get_missing_raises_404(self) -> None:
        s = FakeKubeApiServer()
        with pytest.raises(KubeApiError) as exc:
            s.get_resource("X", "ns", "missing")
        assert exc.value.status_code == 404

    def test_list_filters_by_namespace_and_labels(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="X",
                namespace="n1",
                name="a",
                labels={"app": "a"},
            )
        )
        s.create_resource(
            KubeResource(
                kind="X",
                namespace="n1",
                name="b",
                labels={"app": "b"},
            )
        )
        s.create_resource(
            KubeResource(
                kind="X",
                namespace="n2",
                name="c",
                labels={"app": "a"},
            )
        )
        items = s.list_resources("X", namespace="n1")
        assert {x["metadata"]["name"] for x in items} == {"a", "b"}
        items_a = s.list_resources("X", labels={"app": "a"})
        assert {x["metadata"]["name"] for x in items_a} == {"a", "c"}

    def test_patch_strategic_merge(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="X",
                namespace="n",
                name="a",
                spec={"replicas": 1, "image": "x:1"},
            )
        )
        r = s.patch_resource("X", "n", "a", {"spec": {"replicas": 5}})
        assert r["spec"]["replicas"] == 5
        # Other spec fields preserved
        assert r["spec"]["image"] == "x:1"

    def test_scale_resource(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="Deployment",
                namespace="n",
                name="a",
                spec={"replicas": 1},
            )
        )
        r = s.scale_resource("Deployment", "n", "a", 7)
        assert r["spec"]["replicas"] == 7

    def test_scale_with_invalid_replicas_raises(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(KubeResource(kind="X", namespace="n", name="a"))
        with pytest.raises(KubeApiError):
            s.scale_resource("X", "n", "a", -1)

    def test_delete_resource(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(KubeResource(kind="X", namespace="n", name="a"))
        s.delete_resource("X", "n", "a")
        with pytest.raises(KubeApiError):
            s.get_resource("X", "n", "a")

    def test_revision_increments_on_patch(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="X",
                namespace="n",
                name="a",
                spec={"v": 1},
            )
        )
        s.patch_resource("X", "n", "a", {"spec": {"v": 2}})
        r = s.get_resource("X", "n", "a")
        assert int(r["metadata"]["resourceVersion"]) >= 2


class TestFakeKubeApiServerMetrics:
    def test_record_and_query_metric(self) -> None:
        s = FakeKubeApiServer()
        s.record_metric("D", "n", "a", 50.0, observed_at="t0")
        s.record_metric("D", "n", "a", 60.0, observed_at="t1")
        points = s.query_metrics("D", "n", "a")
        assert [p["value"] for p in points] == [50.0, 60.0]

    def test_query_metrics_since_filter(self) -> None:
        s = FakeKubeApiServer()
        s.record_metric("D", "n", "a", 1.0, observed_at="t0")
        s.record_metric("D", "n", "a", 2.0, observed_at="t1")
        s.record_metric("D", "n", "a", 3.0, observed_at="t2")
        late = s.query_metrics("D", "n", "a", since="t1")
        assert [p["value"] for p in late] == [2.0, 3.0]

    def test_metric_jitter_applied(self) -> None:
        s = FakeKubeApiServer()
        s.record_metric("D", "n", "a", 10.0, observed_at="t0")
        s.set_metric_jitter(lambda v: v * 2)
        points = s.query_metrics("D", "n", "a")
        assert points[0]["value"] == 20.0
        s.set_metric_jitter(None)
        points = s.query_metrics("D", "n", "a")
        assert points[0]["value"] == 10.0


class TestFakeKubeApiServerOutage:
    def test_outage_raises_on_read(self) -> None:
        s = FakeKubeApiServer()
        s.create_resource(KubeResource(kind="X", namespace="n", name="a"))
        s.set_outage(True)
        with pytest.raises(KubeApiError) as exc:
            s.get_resource("X", "n", "a")
        assert exc.value.status_code == 503

    def test_outage_raises_on_write(self) -> None:
        s = FakeKubeApiServer()
        s.set_outage(True)
        with pytest.raises(KubeApiError):
            s.create_resource(KubeResource(kind="X", namespace="n", name="a"))

    def test_outage_clears(self) -> None:
        s = FakeKubeApiServer()
        s.set_outage(True)
        s.set_outage(False)
        s.create_resource(KubeResource(kind="X", namespace="n", name="a"))


# ─── K8sObserveAdapter ────────────────────────────────────────────


class TestObserveAdapter:
    def _server(self) -> FakeKubeApiServer:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="D",
                namespace="ns1",
                name="a",
                spec={"replicas": 3},
                labels={"app": "a"},
            )
        )
        s.create_resource(
            KubeResource(
                kind="D",
                namespace="ns1",
                name="b",
                spec={"replicas": 2},
                labels={"app": "b"},
            )
        )
        s.record_metric("D", "ns1", "a", 80.0, observed_at="t0")
        s.record_metric("D", "ns1", "a", 90.0, observed_at="t1")
        return s

    def test_observe_resources_list(self) -> None:
        adapter = K8sObserveAdapter(self._server())
        r = adapter.observe({"resources": [{"kind": "D", "namespace": "ns1"}]})
        assert len(r["resources"]) == 2
        assert r["metrics"] == []

    def test_observe_resources_with_label_filter(self) -> None:
        adapter = K8sObserveAdapter(self._server())
        r = adapter.observe({"resources": [{"kind": "D", "labels": {"app": "a"}}]})
        assert len(r["resources"]) == 1
        assert r["resources"][0]["metadata"]["name"] == "a"

    def test_observe_single_resource_by_name(self) -> None:
        adapter = K8sObserveAdapter(self._server())
        r = adapter.observe({"resources": [{"kind": "D", "namespace": "ns1", "name": "a"}]})
        assert len(r["resources"]) == 1

    def test_observe_metrics(self) -> None:
        adapter = K8sObserveAdapter(self._server())
        r = adapter.observe({"metrics": [{"kind": "D", "namespace": "ns1", "name": "a"}]})
        assert len(r["metrics"]) == 2
        assert r["metrics"][0]["point"]["value"] == 80.0

    def test_outage_surfaces_as_error_section(self) -> None:
        server = self._server()
        server.set_outage(True)
        adapter = K8sObserveAdapter(server)
        r = adapter.observe({"resources": [{"kind": "D", "namespace": "ns1"}]})
        assert "errors" in r
        assert r["errors"][0]["status_code"] == 503

    def test_missing_kind_returns_error_in_response(self) -> None:
        adapter = K8sObserveAdapter(self._server())
        r = adapter.observe({"resources": [{"namespace": "ns1"}]})
        assert r["errors"][0]["status_code"] == 400


# ─── K8sExecuteAdapter ────────────────────────────────────────────


class TestExecuteAdapter:
    def _setup(self) -> tuple[FakeKubeApiServer, K8sExecuteAdapter]:
        s = FakeKubeApiServer()
        s.create_resource(
            KubeResource(
                kind="D",
                namespace="n",
                name="a",
                spec={"replicas": 1},
            )
        )
        return s, K8sExecuteAdapter(s)

    def test_scale_action_succeeds(self) -> None:
        _, exe = self._setup()
        r = exe.execute(
            {
                "kind": "scale",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {"replicas": 5},
            }
        )
        assert r["status"] == "ok"
        assert r["result"]["spec"]["replicas"] == 5

    def test_patch_action_merges_spec(self) -> None:
        _, exe = self._setup()
        r = exe.execute(
            {
                "kind": "patch",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {"spec": {"image": "x:2"}},
            }
        )
        assert r["status"] == "ok"
        assert r["result"]["spec"]["image"] == "x:2"
        assert r["result"]["spec"]["replicas"] == 1

    def test_label_action_updates_labels(self) -> None:
        _, exe = self._setup()
        r = exe.execute(
            {
                "kind": "label",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {"labels": {"tier": "web"}},
            }
        )
        assert r["status"] == "ok"
        assert r["result"]["metadata"]["labels"]["tier"] == "web"

    def test_delete_action_marks_status(self) -> None:
        _, exe = self._setup()
        r = exe.execute(
            {
                "kind": "delete",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {},
            }
        )
        assert r["status"] == "ok"
        assert r["result"]["status"]["deleted"] is True

    def test_unsupported_action_kind(self) -> None:
        _, exe = self._setup()
        r = exe.execute(
            {
                "kind": "reboot",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {},
            }
        )
        assert r["status"] == "error"
        assert r["error_code"] == "UNSUPPORTED_ACTION"

    def test_idempotent_re_execute(self) -> None:
        server, exe = self._setup()
        key = "scale-a-v1"
        a = exe.execute(
            {
                "kind": "scale",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {"replicas": 5},
                "idempotency_key": key,
            }
        )
        b = exe.execute(
            {
                "kind": "scale",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {"replicas": 99},  # different but same key → cached
                "idempotency_key": key,
            }
        )
        assert a == b
        # Server should reflect only the first scale (replicas=5)
        current = server.get_resource("D", "n", "a")
        assert current["spec"]["replicas"] == 5

    def test_missing_resource_returns_404_payload(self) -> None:
        server, exe = self._setup()
        r = exe.execute(
            {
                "kind": "scale",
                "resource": {"kind": "D", "namespace": "n", "name": "ghost"},
                "payload": {"replicas": 1},
            }
        )
        assert r["status"] == "error"
        assert r["status_code"] == 404

    def test_invalid_payload_returns_400(self) -> None:
        _, exe = self._setup()
        r = exe.execute(
            {
                "kind": "scale",
                "resource": {"kind": "D", "namespace": "n", "name": "a"},
                "payload": {"replicas": "five"},
            }
        )
        assert r["status"] == "error"


# ─── Domain neutrality lint ───────────────────────────────────────


class TestDomainNeutrality:
    """The whole point of task 4.6 is to ship adapter PLUMBING, never
    a scenario. This test guards the package against accidentally
    introducing HPA / fairness / multi-tenant / quota knowledge.

    If a future contributor needs to refer to one of these terms for a
    legitimate reason (e.g. documenting that this code is *not* about
    HPA), wrap it in a comment that includes ``# k8s-domain-neutral:
    ok`` — the test will let that line through.
    """

    FORBIDDEN_PATTERNS = [
        r"\bHPA\b",
        r"horizontal[\s-]?pod[\s-]?autoscaler",
        r"\bfairness\b",
        r"\bquota\b",
        r"multi[\s-]?tenant",
        r"anti[\s-]?sybil",
    ]
    PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "extensions" / "k8s"

    def _iter_source_files(self):
        for path in self.PACKAGE_ROOT.rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            yield path

    def test_no_scenario_specific_terms_in_source(self) -> None:
        offenders: list[tuple[Path, str, str]] = []
        for path in self._iter_source_files():
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                if "k8s-domain-neutral: ok" in line:
                    continue
                for pattern in self.FORBIDDEN_PATTERNS:
                    if re.search(pattern, line, flags=re.IGNORECASE):
                        offenders.append((path, line.strip(), pattern))
        assert not offenders, "extensions/k8s/ must remain scenario-free. Offending lines:\n" + "\n".join(
            f"  {p.name}: {ln!r} (matched /{pat}/)" for p, ln, pat in offenders
        )
