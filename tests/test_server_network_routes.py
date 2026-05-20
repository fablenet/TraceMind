"""HTTP routes for AgentNetwork cycles — Phase 6 Stage 6-3.4."""

from __future__ import annotations

import httpx
import pytest

from tm.artifacts.models import AgentNetworkBody
from tm.control.meta.controller import L1CycleResult
from tm.server.app import create_app
from tm.server.routes_network import NetworkRuntime


def _two_leaf_network() -> AgentNetworkBody:
    return AgentNetworkBody.from_mapping(
        {
            "network_id": "network.demo.two_leaf",
            "topology": "star",
            "center_bundle_ref": "bundle.center",
            "leaf_bundle_refs": ["bundle.leaf_a", "bundle.leaf_b"],
            "edges": [
                {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
                {"from": "bundle.leaf_b", "to": "bundle.center", "kpi_keys": ["kpi.beta"]},
            ],
            "transport_default": "inprocess",
        }
    )


def _mock_runner(prefix: str):
    def runner(cycle_number: int) -> L1CycleResult:
        return L1CycleResult(
            cycle_id=f"{prefix}-cycle-{cycle_number}",
            snapshot={"environment": {"metrics": {f"{prefix}.kpi": float(cycle_number)}}},
            report={"report_id": f"rep-{prefix}-{cycle_number}", "status": "ok"},
        )

    return runner


def _app_with_network():
    app = create_app()
    runtime = NetworkRuntime.build(
        _two_leaf_network(),
        center_l1_runner=_mock_runner("bundle.center"),
        leaf_runners={
            "bundle.leaf_a": _mock_runner("bundle.leaf_a"),
            "bundle.leaf_b": _mock_runner("bundle.leaf_b"),
        },
        intent_ref="test.network",
    )
    app.state.network_runtime = runtime
    return app


@pytest.mark.asyncio
async def test_topology_requires_runtime() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/network/topology")
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_topology_returns_peers() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/network/topology")
        assert response.status_code == 200
        payload = response.json()
        assert payload["network_id"] == "network.demo.two_leaf"
        assert payload["center_peer_id"] == "bundle.center"
        assert set(payload["leaf_peer_ids"]) == {"bundle.leaf_a", "bundle.leaf_b"}


@pytest.mark.asyncio
async def test_run_network_cycle_success() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/network/cycle/run", json={"cycle_number": 1})
        assert response.status_code == 200
        payload = response.json()
        assert payload["peer_chain_valid"] is True
        assert payload["peer_chain_errors"] == []
        assert "center_proof" in payload
        assert set(payload["leaf_proofs"].keys()) == {"bundle.leaf_a", "bundle.leaf_b"}


@pytest.mark.asyncio
async def test_run_network_cycle_default_cycle_number() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/network/cycle/run", json={})
        assert response.status_code == 200
        assert response.json()["cycle_number"] == 1


@pytest.mark.asyncio
async def test_run_leaf_cycle_success() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/network/leaf/cycle/run",
            json={"peer_node_id": "bundle.leaf_a", "cycle_number": 2},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["peer_node_id"] == "bundle.leaf_a"
        assert payload["cycle_id"] == "bundle.leaf_a-cycle-2"
        assert payload["proof_report"]["peer_node_id"] == "bundle.leaf_a"


@pytest.mark.asyncio
async def test_run_leaf_cycle_unknown_peer_404() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/network/leaf/cycle/run",
            json={"peer_node_id": "bundle.missing"},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_center_proof_contains_peer_evidence() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/network/cycle/run", json={"cycle_number": 1})
        center = response.json()["center_proof"]
        peer_entries = [e for e in center["evidence_chain"] if e["event_type"] == "peer_proof_report"]
        assert len(peer_entries) == 2


@pytest.mark.asyncio
async def test_leaf_proofs_have_hashes() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/network/cycle/run", json={"cycle_number": 1})
        for proof in response.json()["leaf_proofs"].values():
            assert proof["report_hash"]


@pytest.mark.asyncio
async def test_network_cycle_number_propagates() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/network/cycle/run", json={"cycle_number": 3})
        payload = response.json()
        assert payload["cycle_number"] == 3
        assert payload["leaf_proofs"]["bundle.leaf_a"]["cycle_id"] == "bundle.leaf_a-cycle-3"


@pytest.mark.asyncio
async def test_existing_controller_cycle_route_still_present() -> None:
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1/controller/cycle" in paths
    assert "/api/controller/cycle" in paths


@pytest.mark.asyncio
async def test_network_routes_under_api_v1_prefix() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        topo = await client.get("/api/v1/network/topology")
        assert topo.status_code == 200
        cycle = await client.post("/api/v1/network/cycle/run", json={"cycle_number": 1})
        assert cycle.status_code == 200


@pytest.mark.asyncio
async def test_leaf_cycle_default_cycle_number() -> None:
    transport = httpx.ASGITransport(app=_app_with_network())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/network/leaf/cycle/run",
            json={"peer_node_id": "bundle.leaf_b"},
        )
        assert response.status_code == 200
        assert response.json()["cycle_id"] == "bundle.leaf_b-cycle-1"
