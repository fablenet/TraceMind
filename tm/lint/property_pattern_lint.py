"""Lint checks for PropertyPattern artifacts and IntentBody → Pattern references.

Two responsibilities:

1. ``lint_property_pattern`` — internal consistency of a single PropertyPattern body
   (e.g. every ``{slot}`` placeholder in ``formula_template`` is declared, every
   declared slot is referenced, optional ``category`` / ``pattern_id`` naming hint).

2. ``lint_intent_pattern_refs`` — cross-artifact: an IntentBody's
   ``property_pattern_refs`` / ``slot_fills`` are consistent with a given library
   of PropertyPattern bodies (existence, slot coverage, no extraneous fills).

Both return ``list[LintIssue]`` (importable from ``tm.lint``); callers decide
how to render or escalate.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Sequence, Union

from tm.lint.plan_lint import LintIssue

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

_PATTERN_ID_CATEGORY_PREFIXES: Mapping[str, str] = {
    "safety": "safety",
    "liveness": "liveness",
    "fairness": "fairness",
}


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _get(body: Union[Mapping[str, Any], Any], key: str, default: Any = None) -> Any:
    if isinstance(body, Mapping):
        return body.get(key, default)
    return getattr(body, key, default)


def _iter_slots(body: Union[Mapping[str, Any], Any]) -> List[Mapping[str, Any]]:
    slots = _get(body, "slots", []) or []
    if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes, bytearray)):
        return []
    normalized: List[Mapping[str, Any]] = []
    for slot in slots:
        if isinstance(slot, Mapping):
            normalized.append(slot)
        else:
            normalized.append(
                {
                    "name": getattr(slot, "name", None),
                    "type": getattr(slot, "type", None),
                    "required": getattr(slot, "required", True),
                }
            )
    return normalized


def lint_property_pattern(pattern_body: Union[Mapping[str, Any], Any]) -> List[LintIssue]:
    """Lint a single PropertyPattern body for internal consistency.

    Checks:
    - declared slot names match the regex ``[a-z][a-z0-9_]*``
    - duplicate slot names
    - every ``{slot}`` placeholder in ``formula_template`` resolves to a declared slot
    - every declared slot is referenced at least once in ``formula_template``
    - ``pattern_id`` prefix is consistent with ``category`` (advisory; severity ``warning``)
    """
    issues: List[LintIssue] = []

    pattern_id = _get(pattern_body, "pattern_id")
    category = _get(pattern_body, "category")
    formula = _get(pattern_body, "formula_template") or ""

    slots = _iter_slots(pattern_body)
    declared_names: list[str] = []
    seen_names: set[str] = set()
    for idx, slot in enumerate(slots):
        path = f"slots[{idx}]"
        name = slot.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(
                LintIssue(
                    code="PP_SLOT_NAME",
                    message="slot.name must be a non-empty string",
                    severity="error",
                    path=path,
                )
            )
            continue
        if not re.match(r"^[a-z][a-z0-9_]*$", name):
            issues.append(
                LintIssue(
                    code="PP_SLOT_NAME",
                    message=f"slot.name '{name}' must match [a-z][a-z0-9_]*",
                    severity="error",
                    path=path,
                )
            )
        if name in seen_names:
            issues.append(
                LintIssue(
                    code="PP_SLOT_DUPLICATE",
                    message=f"slot.name '{name}' is duplicated",
                    severity="error",
                    path=path,
                )
            )
        seen_names.add(name)
        declared_names.append(name)

    placeholders = _PLACEHOLDER_RE.findall(formula) if isinstance(formula, str) else []
    placeholder_set = set(placeholders)
    declared_set = set(declared_names)

    for placeholder in placeholder_set - declared_set:
        issues.append(
            LintIssue(
                code="PP_SLOT_UNDECLARED",
                message=(f"formula_template references undeclared slot '{{{placeholder}}}'"),
                severity="error",
                path="formula_template",
            )
        )
    for declared in declared_set - placeholder_set:
        issues.append(
            LintIssue(
                code="PP_SLOT_UNUSED",
                message=(f"slot '{declared}' is declared but not referenced in formula_template"),
                severity="warning",
                path="formula_template",
            )
        )

    if isinstance(pattern_id, str) and isinstance(category, str) and category in _PATTERN_ID_CATEGORY_PREFIXES:
        expected_prefix = _PATTERN_ID_CATEGORY_PREFIXES[category]
        if not pattern_id.startswith(f"{expected_prefix}."):
            issues.append(
                LintIssue(
                    code="PP_ID_CATEGORY",
                    message=(
                        f"pattern_id '{pattern_id}' should start with '{expected_prefix}.' "
                        f"to match category '{category}'"
                    ),
                    severity="warning",
                    path="pattern_id",
                )
            )

    return issues


def lint_intent_pattern_refs(
    intent_body: Union[Mapping[str, Any], Any],
    pattern_library: Mapping[str, Union[Mapping[str, Any], Any]],
) -> List[LintIssue]:
    """Lint an IntentBody's pattern refs against a library of PropertyPattern bodies.

    Checks:
    - every ``property_pattern_refs`` entry exists in ``pattern_library``
    - every referenced pattern has a corresponding entry in ``slot_fills``
    - every **required** slot in a referenced pattern has a value in
      ``slot_fills[pattern_id]``
    - no extraneous slot fills (a fill key with no matching declared slot)
    - no orphan ``slot_fills`` entries (a fill for a pattern not in
      ``property_pattern_refs``)

    Slot **value type** checking is intentionally deferred to Stage 5-3 once the
    domain primitive registry exists; v0.2 only enforces names.
    """
    issues: List[LintIssue] = []

    refs_raw = _get(intent_body, "property_pattern_refs", []) or []
    if isinstance(refs_raw, Sequence) and not isinstance(refs_raw, (str, bytes, bytearray)):
        refs = [str(r) for r in refs_raw]
    else:
        issues.append(
            LintIssue(
                code="INTENT_PATTERN_REFS_SHAPE",
                message="property_pattern_refs must be a list of strings",
                severity="error",
                path="property_pattern_refs",
            )
        )
        refs = []

    fills_raw = _get(intent_body, "slot_fills", {}) or {}
    if not isinstance(fills_raw, Mapping):
        issues.append(
            LintIssue(
                code="INTENT_SLOT_FILLS_SHAPE",
                message="slot_fills must be a mapping",
                severity="error",
                path="slot_fills",
            )
        )
        fills_raw = {}

    refs_set = set(refs)

    for ref in refs:
        if ref not in pattern_library:
            issues.append(
                LintIssue(
                    code="INTENT_PATTERN_UNKNOWN",
                    message=f"property_pattern_refs entry '{ref}' not found in pattern library",
                    severity="error",
                    path="property_pattern_refs",
                )
            )
            continue

        pattern = pattern_library[ref]
        slots = _iter_slots(pattern)
        required_slots: set[str] = set()
        declared_slot_names: set[str] = set()
        for slot in slots:
            slot_name = slot.get("name")
            if not isinstance(slot_name, str):
                continue
            declared_slot_names.add(slot_name)
            is_required = slot.get("required", True)
            if is_required:
                required_slots.add(slot_name)

        fills_for_ref = fills_raw.get(ref)
        if fills_for_ref is None:
            if required_slots:
                issues.append(
                    LintIssue(
                        code="INTENT_SLOT_FILL_MISSING",
                        message=(
                            f"slot_fills missing entry for pattern '{ref}' (required slots: {sorted(required_slots)})"
                        ),
                        severity="error",
                        path="slot_fills",
                    )
                )
            continue
        if not isinstance(fills_for_ref, Mapping):
            issues.append(
                LintIssue(
                    code="INTENT_SLOT_FILLS_SHAPE",
                    message=f"slot_fills['{ref}'] must be a mapping",
                    severity="error",
                    path=f"slot_fills.{ref}",
                )
            )
            continue

        fill_keys = set(fills_for_ref.keys())
        missing_required = required_slots - fill_keys
        for slot_name in sorted(missing_required):
            issues.append(
                LintIssue(
                    code="INTENT_SLOT_FILL_MISSING",
                    message=f"slot_fills['{ref}'] missing required slot '{slot_name}'",
                    severity="error",
                    path=f"slot_fills.{ref}",
                )
            )
        extraneous = fill_keys - declared_slot_names
        for slot_name in sorted(extraneous):
            issues.append(
                LintIssue(
                    code="INTENT_SLOT_FILL_EXTRA",
                    message=(f"slot_fills['{ref}'] contains undeclared slot '{slot_name}'"),
                    severity="error",
                    path=f"slot_fills.{ref}",
                )
            )

    for fill_key in sorted(fills_raw.keys()):
        if fill_key not in refs_set:
            issues.append(
                LintIssue(
                    code="INTENT_SLOT_FILL_ORPHAN",
                    message=(f"slot_fills['{fill_key}'] has no matching entry in property_pattern_refs"),
                    severity="error",
                    path="slot_fills",
                )
            )

    return issues


__all__ = ["lint_property_pattern", "lint_intent_pattern_refs"]
