"""Lint checks for AgentNetwork artifacts (K-Ontology v0.3 / Phase 6 Stage 6-1).

Single responsibility: ``lint_agent_network`` enforces *internal* consistency of
a single AgentNetwork body. Cross-artifact lints (verifying that bundle refs
resolve in a registry, that each bundle declares the KPIs claimed on its edges,
etc.) are owned by Stage 6-3 once a network-aware registry view exists.

Issue codes (kept stable across v0.3+):

| Code | Severity | Trigger |
|------|----------|---------|
| ``AN_TOPOLOGY_UNSUPPORTED`` | error | ``topology="tree"`` (reserved but not implemented) |
| ``AN_CENTER_IN_LEAVES`` | error | ``center_bundle_ref`` also appears in ``leaf_bundle_refs`` |
| ``AN_LEAF_EMPTY`` | error | ``leaf_bundle_refs`` is empty |
| ``AN_LEAF_DUPLICATE`` | error | duplicate entries in ``leaf_bundle_refs`` |
| ``AN_EDGE_EMPTY`` | error | ``edges`` is empty |
| ``AN_EDGE_UNKNOWN_NODE`` | error | ``edge.from`` / ``edge.to`` not in ``{center} ∪ leaves`` |
| ``AN_EDGE_LEAF_TO_LEAF`` | error | for ``topology=star``: edge connects two leaves |
| ``AN_EDGE_SELF_LOOP`` | error | ``edge.from == edge.to`` |
| ``AN_EDGE_LEAF_PATCHES_CENTER`` | error | leaf-to-center edge declares non-empty ``allowed_patches`` |
| ``AN_EDGE_KPI_EMPTY`` | error | ``edge.kpi_keys`` is empty |
| ``AN_EDGE_KPI_NAME`` | error | ``kpi_keys`` entry does not match ``[a-z][a-z0-9_.]*`` |
| ``AN_EDGE_TRANSPORT_UNKNOWN`` | error | ``edge.transport`` not in ``{inprocess, http, file_queue}`` |
| ``AN_LEAF_MISSING_EDGE`` | warning | leaf has no outgoing edge to the center |
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Sequence, Union

from tm.lint.plan_lint import LintIssue

_KPI_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
_SUPPORTED_TRANSPORTS = frozenset({"inprocess", "http", "file_queue"})
_SUPPORTED_TOPOLOGIES_IMPLEMENTED = frozenset({"star"})
_SUPPORTED_TOPOLOGIES_DECLARED = frozenset({"star", "tree"})


def _get(body: Union[Mapping[str, Any], Any], key: str, default: Any = None) -> Any:
    if isinstance(body, Mapping):
        return body.get(key, default)
    return getattr(body, key, default)


def _iter_edges(body: Union[Mapping[str, Any], Any]) -> List[Mapping[str, Any]]:
    edges = _get(body, "edges", []) or []
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes, bytearray)):
        return []
    normalized: List[Mapping[str, Any]] = []
    for edge in edges:
        if isinstance(edge, Mapping):
            normalized.append(edge)
        else:
            normalized.append(
                {
                    "from": getattr(edge, "source", None),
                    "to": getattr(edge, "target", None),
                    "kpi_keys": getattr(edge, "kpi_keys", []) or [],
                    "allowed_patches": getattr(edge, "allowed_patches", []) or [],
                    "transport": getattr(edge, "transport", None),
                    "description": getattr(edge, "description", None),
                }
            )
    return normalized


def _as_list_of_strings(value: Any) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item) for item in value]


def lint_agent_network(body: Union[Mapping[str, Any], Any]) -> List[LintIssue]:
    """Lint a single AgentNetwork body for internal consistency.

    Returns a list of ``LintIssue`` (importable from ``tm.lint``). Empty list
    means the body passes all internal checks. The caller decides how to render
    or escalate issues; the artifact verifier (Stage 6-1.6) escalates any
    ``error``-severity entry to a failed verification.
    """
    issues: List[LintIssue] = []

    topology = _get(body, "topology")
    if isinstance(topology, str) and topology in _SUPPORTED_TOPOLOGIES_DECLARED:
        if topology not in _SUPPORTED_TOPOLOGIES_IMPLEMENTED:
            issues.append(
                LintIssue(
                    code="AN_TOPOLOGY_UNSUPPORTED",
                    message=(
                        f"topology '{topology}' is reserved but not implemented in v0.3; "
                        f"only {sorted(_SUPPORTED_TOPOLOGIES_IMPLEMENTED)} is supported"
                    ),
                    severity="error",
                    path="topology",
                )
            )

    center = _get(body, "center_bundle_ref")
    leaves = _as_list_of_strings(_get(body, "leaf_bundle_refs", []))

    if not leaves:
        issues.append(
            LintIssue(
                code="AN_LEAF_EMPTY",
                message="leaf_bundle_refs must contain at least one entry",
                severity="error",
                path="leaf_bundle_refs",
            )
        )

    seen_leaves: set[str] = set()
    duplicate_leaves: set[str] = set()
    for leaf in leaves:
        if leaf in seen_leaves:
            duplicate_leaves.add(leaf)
        seen_leaves.add(leaf)
    for dup in sorted(duplicate_leaves):
        issues.append(
            LintIssue(
                code="AN_LEAF_DUPLICATE",
                message=f"leaf_bundle_refs contains duplicate entry '{dup}'",
                severity="error",
                path="leaf_bundle_refs",
            )
        )

    if isinstance(center, str) and center in seen_leaves:
        issues.append(
            LintIssue(
                code="AN_CENTER_IN_LEAVES",
                message=(f"center_bundle_ref '{center}' must not also appear in leaf_bundle_refs"),
                severity="error",
                path="center_bundle_ref",
            )
        )

    known_nodes: set[str] = set(seen_leaves)
    if isinstance(center, str):
        known_nodes.add(center)

    edges = _iter_edges(body)
    if not edges:
        issues.append(
            LintIssue(
                code="AN_EDGE_EMPTY",
                message="edges must contain at least one entry",
                severity="error",
                path="edges",
            )
        )

    leaves_with_edge_to_center: set[str] = set()

    for idx, edge in enumerate(edges):
        path = f"edges[{idx}]"
        source = edge.get("from")
        target = edge.get("to")
        if not isinstance(source, str):
            issues.append(
                LintIssue(
                    code="AN_EDGE_UNKNOWN_NODE",
                    message="edge.from must be a non-empty string",
                    severity="error",
                    path=f"{path}.from",
                )
            )
            source = None
        if not isinstance(target, str):
            issues.append(
                LintIssue(
                    code="AN_EDGE_UNKNOWN_NODE",
                    message="edge.to must be a non-empty string",
                    severity="error",
                    path=f"{path}.to",
                )
            )
            target = None

        if source and source not in known_nodes:
            issues.append(
                LintIssue(
                    code="AN_EDGE_UNKNOWN_NODE",
                    message=(
                        f"edge.from '{source}' is not the center bundle or any declared leaf "
                        f"(known: {sorted(known_nodes)})"
                    ),
                    severity="error",
                    path=f"{path}.from",
                )
            )
        if target and target not in known_nodes:
            issues.append(
                LintIssue(
                    code="AN_EDGE_UNKNOWN_NODE",
                    message=(
                        f"edge.to '{target}' is not the center bundle or any declared leaf "
                        f"(known: {sorted(known_nodes)})"
                    ),
                    severity="error",
                    path=f"{path}.to",
                )
            )

        if source and target and source == target:
            issues.append(
                LintIssue(
                    code="AN_EDGE_SELF_LOOP",
                    message=f"edge endpoints must differ; got '{source}' on both ends",
                    severity="error",
                    path=path,
                )
            )

        if topology == "star" and isinstance(center, str) and source in seen_leaves and target in seen_leaves:
            issues.append(
                LintIssue(
                    code="AN_EDGE_LEAF_TO_LEAF",
                    message=(
                        f"topology=star forbids leaf-to-leaf edges; got '{source}' -> '{target}' "
                        f"(neither endpoint is the center '{center}')"
                    ),
                    severity="error",
                    path=path,
                )
            )

        allowed = edge.get("allowed_patches") or []
        if not isinstance(allowed, Sequence) or isinstance(allowed, (str, bytes, bytearray)):
            allowed = []
        if isinstance(center, str) and source in seen_leaves and target == center and len(allowed) > 0:
            issues.append(
                LintIssue(
                    code="AN_EDGE_LEAF_PATCHES_CENTER",
                    message=(
                        "leaves never patch the center: leaf-to-center edge "
                        f"'{source}' -> '{target}' has non-empty allowed_patches {list(allowed)}"
                    ),
                    severity="error",
                    path=f"{path}.allowed_patches",
                )
            )

        kpi_keys = edge.get("kpi_keys") or []
        if not isinstance(kpi_keys, Sequence) or isinstance(kpi_keys, (str, bytes, bytearray)):
            kpi_keys = []
        if not kpi_keys:
            issues.append(
                LintIssue(
                    code="AN_EDGE_KPI_EMPTY",
                    message="edge.kpi_keys must contain at least one entry",
                    severity="error",
                    path=f"{path}.kpi_keys",
                )
            )
        else:
            for k_idx, key in enumerate(kpi_keys):
                if not isinstance(key, str) or not _KPI_KEY_RE.match(key):
                    issues.append(
                        LintIssue(
                            code="AN_EDGE_KPI_NAME",
                            message=(f"edge.kpi_keys[{k_idx}] '{key}' must match [a-z][a-z0-9_.]*"),
                            severity="error",
                            path=f"{path}.kpi_keys[{k_idx}]",
                        )
                    )

        transport = edge.get("transport")
        if transport is not None and transport not in _SUPPORTED_TRANSPORTS:
            issues.append(
                LintIssue(
                    code="AN_EDGE_TRANSPORT_UNKNOWN",
                    message=(
                        f"edge.transport '{transport}' is not supported; "
                        f"expected one of {sorted(_SUPPORTED_TRANSPORTS)}"
                    ),
                    severity="error",
                    path=f"{path}.transport",
                )
            )

        if isinstance(center, str) and source in seen_leaves and target == center:
            leaves_with_edge_to_center.add(source)

    if isinstance(center, str) and seen_leaves:
        missing = sorted(seen_leaves - leaves_with_edge_to_center)
        for leaf in missing:
            issues.append(
                LintIssue(
                    code="AN_LEAF_MISSING_EDGE",
                    message=(
                        f"leaf '{leaf}' has no outgoing edge to center '{center}'; "
                        "every leaf should report at least one KPI to the center"
                    ),
                    severity="warning",
                    path="edges",
                )
            )

    return issues


__all__ = ["lint_agent_network"]
