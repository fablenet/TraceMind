"""HPA / anti-sybil star demo metrics — Stage 7-V.7 (the 7-V headline DoD).

Asserts the acceptance criteria on the demo star: compositional ``state_count``
is materially below monolithic and the gap widens with the pod count, while
**per-formula verdicts stay identical** to the monolithic product.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root for examples/

from examples.compositional_hpa_star import build_hpa_star, run


def test_verdict_parity_and_reduction_per_size():
    for n in (2, 3, 4):
        m = run(n)
        assert m["parity"] is True, f"n={n} verdict parity broke"
        assert m["compositional_states"] < m["monolithic_states"], f"n={n} no reduction"


def test_reduction_widens_with_pod_count():
    metrics = [run(n) for n in (2, 3, 4)]
    reductions = [m["reduction_x"] for m in metrics]
    # multiplicative blowup monolithically vs near-flat compositionally → the
    # reduction factor strictly grows as pods are added.
    assert reductions[0] < reductions[1] < reductions[2]
    assert reductions[-1] >= 8.0


def test_all_discharge_routes_exercised():
    m = run(3)
    # EF (single-comp) True, safety-true True, safety-false False, decomposable True
    assert m["verdicts"][0] is True
    assert m["verdicts"][1] is True
    assert m["verdicts"][2] is False
    assert m["verdicts"][3] is True
    # the false safety falls back via spurious recheck; AF liveness is out-of-class
    assert "spurious_fail_recheck" in m["fallbacks"]
    assert "out_of_class" in m["fallbacks"]


def test_build_hpa_star_shape():
    network, bundles, formulas = build_hpa_star(3)
    assert network.topology == "star"
    assert network.center_bundle_ref == "hpa.center"
    assert len(network.leaf_bundle_refs) == 3
    assert len(bundles) == 4
    assert len(formulas) == 5
