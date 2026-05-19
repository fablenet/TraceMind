"""Generic L2 control plane components (domain-neutral).

This package contains the meta-control plumbing that was extracted from
``fablenet-control`` in Phase 5 Stage 5-2. Everything here is intentionally
domain-neutral: KPI tracking, convergence detection, escalation reports,
proof reports, and the L2 cycle bridge. Domain-specific bindings (e.g.
which KPI keys to extract, which intent IDs map to which categories) are
parameters that the consumer of these classes supplies.

The companion runtime cycle (L1) is ``tm.controllers.cycle.ControllerCycle``;
the bridge between L2 and L1 is ``tm.control.meta.cycle_bridge``.
"""
