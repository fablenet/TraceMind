"""POR (store-hash) measurement — Stage 7-V.5.

store-hash POR reduces the product exactly when the *same store* is reachable
with *different pending/done queues*. Note that ``JointAdapter._project``
already canonicalizes each component's event/pending/done order, so naive
cross-component interleaving does NOT create distinct full-states — the lever
only bites on genuine pending/done divergence. These tests pin both behaviors
so the quantified number is interpreted honestly.
"""

from __future__ import annotations

from pathlib import Path

from tm.artifacts.models import AgentBundleBody
from tm.verify.bundle_adapter import adapter_from_bundle
from tm.verify.network import load_agent_bundle_body, load_agent_network_body
from tm.verify.por import PORMeasurement, measure_network_por, measure_por

FIXTURES = Path("tests/fixtures/network_violation")


def _bundle(meta_verify: dict) -> AgentBundleBody:
    return AgentBundleBody.from_mapping(
        {"bundle_id": "b", "agents": [], "plan": [], "meta": {"verify": meta_verify}}
    )


# Two order-independent steps writing the same key: store {x} is reachable with
# pending [s2], pending [s1], done [s1,s2], done [s2,s1] — all merge under store.
_DIAMOND = {
    "initial_store": {},
    "changed_paths": ["start"],
    "steps": {"s1": {"reads": [], "writes": ["x"]}, "s2": {"reads": [], "writes": ["x"]}},
    "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["s1", "s2"]}],
}


def _single_step(key: str) -> dict:
    return {
        "initial_store": {},
        "changed_paths": ["start"],
        "steps": {f"st_{key}": {"reads": [], "writes": [key]}},
        "rules": [{"name": "s", "triggers": ["start"], "steps": [f"st_{key}"]}],
    }


def test_store_hash_por_reduces_on_pending_done_divergence():
    a = adapter_from_bundle(_bundle(_DIAMOND))
    m = measure_por([a], component_ids=["d"], max_depth=16)
    assert m.full_state_count == 5
    assert m.store_state_count == 2
    assert m.states_eliminated == 3
    assert m.state_reduction_ratio == 3 / 5


def test_canonicalized_interleaving_yields_no_extra_reduction():
    # Independent single-step components: _project already canonicalizes order,
    # so store-hash finds nothing more to merge — honestly reported as 0.
    a = adapter_from_bundle(_bundle(_single_step("xa")))
    b = adapter_from_bundle(_bundle(_single_step("xb")))
    m = measure_por([a, b], component_ids=["A", "B"], max_depth=16)
    assert m.store_state_count == m.full_state_count
    assert m.states_eliminated == 0
    assert m.state_reduction_ratio == 0.0


def test_store_count_never_exceeds_full():
    a = adapter_from_bundle(_bundle(_DIAMOND))
    m = measure_por([a], component_ids=["d"])
    assert m.store_state_count <= m.full_state_count
    assert m.store_edge_count <= m.full_edge_count


def test_measurement_to_dict_shape():
    a = adapter_from_bundle(_bundle(_DIAMOND))
    d = measure_por([a], component_ids=["d"]).to_dict()
    assert set(d) == {
        "full_state_count", "store_state_count", "full_edge_count", "store_edge_count",
        "states_eliminated", "state_reduction_ratio", "max_depth",
    }


def test_measure_network_por_on_fixture():
    net = load_agent_network_body(FIXTURES / "agent_network.yaml")
    bundles = {
        "bundle.center": load_agent_bundle_body(FIXTURES / "bundle.center.yaml"),
        "bundle.leaf_a": load_agent_bundle_body(FIXTURES / "bundle.leaf_a.yaml"),
        "bundle.leaf_b": load_agent_bundle_body(FIXTURES / "bundle.leaf_b.yaml"),
    }
    m = measure_network_por(net, bundles, max_depth=16)
    assert isinstance(m, PORMeasurement)
    assert m.store_state_count <= m.full_state_count
    assert 0.0 <= m.state_reduction_ratio <= 1.0


def test_por_measurement_is_deterministic():
    a = adapter_from_bundle(_bundle(_DIAMOND))
    assert measure_por([a], component_ids=["d"]).to_dict() == measure_por([a], component_ids=["d"]).to_dict()
