"""5W1H completeness contract — Phase 7 Stage 7-0.

Deterministic, **zero-LLM** structural-completeness model for K-Ontology
requirements. This module ships the data model (dimensions / statuses /
severities) and the domain-profile loader. The judging core
(``compute_5w1h_completeness``) lands in Task 7-0.3.

Spec: ``docs/specs/5w1h-completeness-v0_1.md``.

Hard rule (invariant 3): this module MUST NOT import any LLM machinery.
Completeness is judged by deterministic rules only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from tm.utils.yaml import import_yaml

PROFILES_DIR: Path = Path(__file__).resolve().parent / "profiles"

_YAML_SUFFIXES = {".yaml", ".yml"}


class Dimension(str, Enum):
    """The six 5W1H dimensions a requirement must describe."""

    WHO = "who"
    WHY = "why"
    WHAT = "what"
    WHEN = "when"
    WHERE = "where"
    HOW = "how"


ALL_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.WHO,
    Dimension.WHY,
    Dimension.WHAT,
    Dimension.WHEN,
    Dimension.WHERE,
    Dimension.HOW,
)


class DimStatus(str, Enum):
    """Per-dimension completeness verdict."""

    SATISFIED = "satisfied"
    PARTIAL = "partial"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class Severity(str, Enum):
    """How strongly a profile requires a dimension."""

    ERROR = "error"
    WARN = "warn"
    OFF = "off"


class Mode(str, Enum):
    """Two-phase judgment (spec §3b, user-confirmed 2026-06-13).

    ``design`` — exploratory, low-friction. Deterministic keyword heuristics
    scan prose so a hard ``missing`` on an error dim is *downgraded* to a
    non-blocking ``partial`` when the prose mentions the concept. Partials are
    tolerated (do not block).

    ``seal`` — the hard gate before sign-off. Heuristics OFF (only structured
    fields count); every error-severity dim must be ``satisfied`` (or closed by
    a disposition, Task 7-0.11) or it blocks.
    """

    DESIGN = "design"
    SEAL = "seal"


# Deterministic per-dimension keyword lexicon for ``design``-mode prose
# scanning. Substring match, case-folded, no LLM. Bilingual (EN + ZH) because
# intents are authored in either language. Profiles may extend via the optional
# ``heuristic_keywords`` field (merged on top of these built-ins).
_HEURISTIC_LEXICON: dict[Dimension, tuple[str, ...]] = {
    Dimension.WHO: (
        "user", "actor", "agent", "operator", "service", "role", "client",
        "用户", "代理", "操作员", "角色", "服务", "客户",
    ),
    Dimension.WHY: (
        "because", "in order to", "objective", "purpose", "motivation", "rationale",
        "目标", "为了", "因为", "目的", "动机", "理由",
    ),
    Dimension.WHAT: (
        "input", "output", "produce", "generate", "compute", "transform", "result",
        "输入", "输出", "生成", "产出", "计算", "转换", "结果",
    ),
    Dimension.WHEN: (
        "when", "trigger", "every", "periodic", "schedule", "interval", "liveness",
        "deadline", "timeout", "after ", "before ", "on event", "eventually", "real-time",
        "周期", "触发", "每", "定时", "超时", "之后", "之前", "时序", "实时", "最终",
    ),
    Dimension.WHERE: (
        "scope", "namespace", "cluster", "region", "topology", "network", "boundary",
        "node", "tenant", "partition", "environment",
        "作用域", "命名空间", "集群", "拓扑", "节点", "区域", "租户", "边界", "环境", "分区",
    ),
    Dimension.HOW: (
        "via", "using", "by means", "approach", "mechanism", "pattern", "strategy",
        "通过", "使用", "方式", "机制", "手段", "策略",
    ),
}


# Ultimate fallback severities (spec §2 base values) used when a dimension is
# not specified anywhere in the extends chain. Keeps resolution total.
_DEFAULT_SEVERITIES: dict[Dimension, Severity] = {
    Dimension.WHO: Severity.ERROR,
    Dimension.WHY: Severity.ERROR,
    Dimension.WHAT: Severity.ERROR,
    Dimension.WHEN: Severity.WARN,
    Dimension.WHERE: Severity.WARN,
    Dimension.HOW: Severity.ERROR,
}


@dataclass(frozen=True)
class Profile:
    """A resolved 5W1H completeness profile (after ``extends`` merging).

    Severities, required slots and vocabulary hints are fully flattened so
    callers never need to walk the inheritance chain again.
    """

    profile_id: str
    domain: str | None
    severities: Mapping[Dimension, Severity]
    required_slots: tuple[str, ...]
    vocabulary_hints: Mapping[Dimension, str]
    heuristic_keywords: Mapping[Dimension, tuple[str, ...]] = field(default_factory=dict)
    # Which dimension a missing domain ``required_slot`` degrades (spec §4).
    required_slots_dimension: Dimension = Dimension.WHAT

    def severity(self, dim: Dimension) -> Severity:
        return self.severities.get(dim, _DEFAULT_SEVERITIES[dim])

    def hint(self, dim: Dimension) -> str | None:
        return self.vocabulary_hints.get(dim)

    def keywords(self, dim: Dimension) -> tuple[str, ...]:
        """Built-in lexicon for ``dim`` extended by any profile-declared keywords."""
        merged: list[str] = []
        for kw in (*_HEURISTIC_LEXICON.get(dim, ()), *self.heuristic_keywords.get(dim, ())):
            if kw not in merged:
                merged.append(kw)
        return tuple(merged)


def _coerce_dimension(raw: object) -> Dimension:
    try:
        return Dimension(str(raw).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown 5W1H dimension '{raw}'") from exc


def _coerce_severity(raw: object) -> Severity:
    # YAML 1.1 parses a bare ``off`` as boolean False; profile authors will
    # naturally write ``when: off``, so accept that as Severity.OFF.
    if isinstance(raw, bool):
        if raw is False:
            return Severity.OFF
        raise ValueError("severity 'on'/True is not valid; use error/warn/off")
    try:
        return Severity(str(raw).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown severity '{raw}'") from exc


def _resolve_profile_path(name_or_path: str | Path, *, base_dir: Path | None = None) -> Path:
    """Map a profile name or path to a concrete file.

    A value with a YAML suffix (or an existing path) is treated as a path.
    A bare name is looked up as ``<name>.yaml`` first under ``base_dir`` (the
    directory of the referring profile, so sibling ``extends`` work), then
    under the built-in ``profiles/`` directory.
    """
    candidate = Path(name_or_path)
    if candidate.suffix.lower() in _YAML_SUFFIXES or candidate.exists():
        return candidate.expanduser()
    if base_dir is not None:
        sibling = base_dir / f"{name_or_path}.yaml"
        if sibling.exists():
            return sibling
    return PROFILES_DIR / f"{name_or_path}.yaml"


def load_profile(
    name_or_path: str | Path = "base",
    *,
    base_dir: Path | None = None,
    _seen: frozenset[str] = frozenset(),
) -> Profile:
    """Load and fully resolve a 5W1H profile.

    Resolves ``extends`` recursively (cycle-guarded), then layers this
    profile's ``severity_overrides`` / ``required_slots`` / ``vocabulary_hints``
    on top of the parent's. Deterministic; no LLM.
    """
    yaml = import_yaml()
    if yaml is None:
        raise ValueError("PyYAML is required to load 5W1H profiles")

    path = _resolve_profile_path(name_or_path, base_dir=base_dir)
    if not path.exists():
        raise ValueError(f"5W1H profile not found: '{name_or_path}' (resolved to {path})")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: profile must be a YAML mapping")

    profile_id = str(raw.get("profile_id") or path.stem)
    return _resolve_profile(
        raw,
        profile_id=profile_id,
        base_dir=path.parent,
        label=str(path),
        _seen=_seen,
    )


def load_profile_from_mapping(
    raw: Mapping[str, Any],
    *,
    base_dir: Path | None = None,
    _seen: frozenset[str] = frozenset(),
) -> Profile:
    """Resolve a 5W1H profile from an in-memory mapping (no file on disk).

    Used by governance (Task 7-0.9) to evaluate a *proposed* profile body
    before it is materialised. ``extends`` is resolved against ``base_dir``
    (defaults to the built-in ``profiles/`` directory). Deterministic; no LLM.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("profile must be a mapping")
    profile_id = str(raw.get("profile_id") or "")
    if not profile_id:
        raise ValueError("profile_id is required to resolve a profile mapping")
    return _resolve_profile(
        raw,
        profile_id=profile_id,
        base_dir=base_dir or PROFILES_DIR,
        label=f"profile '{profile_id}'",
        _seen=_seen,
    )


def _resolve_profile(
    raw: Mapping[str, Any],
    *,
    profile_id: str,
    base_dir: Path,
    label: str,
    _seen: frozenset[str],
) -> Profile:
    extends = raw.get("extends")
    if extends:
        ext_name = str(extends)
        if ext_name == profile_id or ext_name in _seen:
            raise ValueError(f"profile extends cycle detected involving '{ext_name}'")
        parent = load_profile(ext_name, base_dir=base_dir, _seen=_seen | {profile_id})
        severities: dict[Dimension, Severity] = dict(parent.severities)
        required_slots: list[str] = list(parent.required_slots)
        hints: dict[Dimension, str] = dict(parent.vocabulary_hints)
        keywords: dict[Dimension, tuple[str, ...]] = dict(parent.heuristic_keywords)
        required_slots_dimension = parent.required_slots_dimension
    else:
        severities = dict(_DEFAULT_SEVERITIES)
        required_slots = []
        hints = {}
        keywords = {}
        required_slots_dimension = Dimension.WHAT

    overrides = raw.get("severity_overrides") or {}
    if not isinstance(overrides, Mapping):
        raise ValueError(f"{label}: severity_overrides must be a mapping")
    for dim_raw, sev_raw in overrides.items():
        severities[_coerce_dimension(dim_raw)] = _coerce_severity(sev_raw)

    slots_raw = raw.get("required_slots") or []
    if not isinstance(slots_raw, (list, tuple)):
        raise ValueError(f"{label}: required_slots must be a list")
    for slot in slots_raw:
        slot_name = str(slot)
        if slot_name not in required_slots:
            required_slots.append(slot_name)

    hints_raw = raw.get("vocabulary_hints") or {}
    if not isinstance(hints_raw, Mapping):
        raise ValueError(f"{label}: vocabulary_hints must be a mapping")
    for dim_raw, hint in hints_raw.items():
        hints[_coerce_dimension(dim_raw)] = str(hint)

    kw_raw = raw.get("heuristic_keywords") or {}
    if not isinstance(kw_raw, Mapping):
        raise ValueError(f"{label}: heuristic_keywords must be a mapping")
    for dim_raw, kw_list in kw_raw.items():
        if not isinstance(kw_list, (list, tuple)):
            raise ValueError(f"{label}: heuristic_keywords.{dim_raw} must be a list")
        dim = _coerce_dimension(dim_raw)
        existing = list(keywords.get(dim, ()))
        for kw in kw_list:
            kw_str = str(kw).strip().lower()
            if kw_str and kw_str not in existing:
                existing.append(kw_str)
        keywords[dim] = tuple(existing)

    rsd_raw = raw.get("required_slots_dimension")
    if rsd_raw is not None:
        required_slots_dimension = _coerce_dimension(rsd_raw)

    domain_raw = raw.get("domain")
    domain = str(domain_raw) if domain_raw not in (None, "", "null") else None

    return Profile(
        profile_id=profile_id,
        domain=domain,
        severities=severities,
        required_slots=tuple(required_slots),
        vocabulary_hints=hints,
        heuristic_keywords=keywords,
        required_slots_dimension=required_slots_dimension,
    )


# ─── Completeness verdict model ───────────────────────────────────


@dataclass(frozen=True)
class DimensionVerdict:
    """Per-dimension completeness verdict (spec §5)."""

    dimension: Dimension
    status: DimStatus
    severity: Severity
    evidence: tuple[str, ...]
    missing_reason: str | None
    suggestion: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "severity": self.severity.value,
            "evidence": list(self.evidence),
            "missing_reason": self.missing_reason,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class CompletenessOutcome:
    """Result of :func:`compute_5w1h_completeness` (mirrors coverage.py shape)."""

    exit_code: int
    report: Mapping[str, Any]


# ─── Evidence collection (Intent-only, Task 7-0.3) ────────────────


def _slot_keys(slot_fills: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for slot_map in slot_fills.values():
        if isinstance(slot_map, Mapping):
            keys.extend(str(k) for k in slot_map.keys())
    return keys


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


def _eval_who(body: Mapping[str, Any]) -> tuple[DimStatus, list[str], str | None]:
    actors = body.get("actors") or []
    if _nonempty(actors):
        return DimStatus.SATISFIED, [f"actors[{len(actors)}]"], None
    return DimStatus.MISSING, [], "no actors declared (Who)"


def _eval_why(body: Mapping[str, Any]) -> tuple[DimStatus, list[str], str | None]:
    context_ok = _nonempty(body.get("context"))
    goal_ok = _nonempty(body.get("goal"))
    if context_ok and goal_ok:
        return DimStatus.SATISFIED, ["context", "goal"], None
    if context_ok or goal_ok:
        present = "context" if context_ok else "goal"
        return DimStatus.PARTIAL, [present], "Why needs both context and goal"
    return DimStatus.MISSING, [], "no context/goal (Why)"


def _eval_what(body: Mapping[str, Any]) -> tuple[DimStatus, list[str], str | None]:
    goal_ok = _nonempty(body.get("goal"))
    inputs = body.get("inputs") or []
    outputs = body.get("outputs") or []
    if not goal_ok:
        return DimStatus.MISSING, [], "no goal (What)"
    evidence = ["goal"]
    if _nonempty(inputs):
        evidence.append(f"inputs[{len(inputs)}]")
    if _nonempty(outputs):
        evidence.append(f"outputs[{len(outputs)}]")
    if _nonempty(inputs) or _nonempty(outputs):
        return DimStatus.SATISFIED, evidence, None
    return DimStatus.PARTIAL, ["goal"], "What needs at least one of inputs/outputs"


def _eval_when(
    body: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    pattern_categories: Mapping[str, str],
) -> tuple[DimStatus, list[str], str | None]:
    if plan is not None:
        rules = plan.get("rules") or []
        for rule in rules:
            if isinstance(rule, Mapping) and _nonempty(rule.get("triggers")):
                return DimStatus.SATISFIED, [f"plan:rule.{rule.get('name', '?')}.triggers"], None
    # A referenced liveness PropertyPattern carries temporal/eventuality
    # semantics (e.g. ``EF goal``) — that is When evidence (spec §2).
    for ref in body.get("property_pattern_refs") or []:
        if pattern_categories.get(str(ref)) == "liveness":
            return DimStatus.SATISFIED, [f"pattern:{ref}:liveness"], None
    when_keys = [k for k in _slot_keys(body.get("slot_fills") or {}) if k.startswith("when_")]
    if when_keys:
        return DimStatus.SATISFIED, [f"slot_fills.{when_keys[0]}"], None
    return (
        DimStatus.MISSING,
        [],
        "no linked Plan with triggers, no liveness PropertyPattern, and no when_* slot (When)",
    )


def _eval_where(
    body: Mapping[str, Any],
    network: Mapping[str, Any] | None,
) -> tuple[DimStatus, list[str], str | None]:
    if network is not None and _nonempty(network.get("network_id")):
        return DimStatus.SATISFIED, [f"network:{network.get('network_id')}"], None
    where_keys = [
        k for k in _slot_keys(body.get("slot_fills") or {}) if k == "domain" or k.startswith("where_")
    ]
    if where_keys:
        return DimStatus.SATISFIED, [f"slot_fills.{where_keys[0]}"], None
    return DimStatus.MISSING, [], "no AgentNetwork and no domain/where_* slot (Where)"


def _eval_how(body: Mapping[str, Any]) -> tuple[DimStatus, list[str], str | None]:
    refs = body.get("property_pattern_refs") or []
    if _nonempty(refs):
        return DimStatus.SATISFIED, [f"property_pattern_refs[{len(refs)}]"], None
    return DimStatus.MISSING, [], "no property_pattern_refs (How)"


def _prose_text(body: Mapping[str, Any]) -> str:
    """Case-folded prose blob scanned by design-mode heuristics.

    Pulls the free-text narrative fields only; structured lists (actors,
    inputs, …) are judged by the strict evaluators, not the heuristic.
    """
    parts: list[str] = []
    for key in ("title", "context", "goal"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("assumptions", "constraints", "non_goals"):
        value = body.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
    return "\n".join(parts).lower()


def _scan_prose(body: Mapping[str, Any], dim: Dimension, profile: Profile) -> str | None:
    """Return the first lexicon keyword found in prose for ``dim``, else None.

    Deterministic substring match — the design-mode signal, never a seal gate.
    """
    text = _prose_text(body)
    for kw in profile.keywords(dim):
        if kw in text:
            return kw
    return None


def _load_pattern_categories(patterns_dir: Path | None) -> dict[str, str]:
    """Build a deterministic ``pattern_id -> category`` map (best-effort).

    Defaults to the shipped seed PropertyPattern library; a downstream may
    point ``patterns_dir`` at its own templates. Failures (missing PyYAML /
    directory) degrade to an empty map — When then falls back to Plan/slot
    evidence and never raises.
    """
    try:
        from tm.patterns.library import PatternLibrary, SEED_ROOT

        root = Path(patterns_dir) if patterns_dir is not None else SEED_ROOT
        library = PatternLibrary.from_directory(root)
        return {entry.pattern_id: entry.category for entry in library.entries()}
    except Exception:
        return {}


def _missing_required_slots(body: Mapping[str, Any], profile: Profile) -> list[str]:
    if not profile.required_slots:
        return []
    present = set(_slot_keys(body.get("slot_fills") or {}))
    return [slot for slot in profile.required_slots if slot not in present]


def _load_intent_mapping(path: Path) -> Mapping[str, Any]:
    yaml = import_yaml()
    text = path.expanduser().read_text(encoding="utf-8")
    if path.suffix.lower() in _YAML_SUFFIXES:
        if yaml is None:
            raise ValueError("PyYAML is required to read YAML intent files")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    if isinstance(payload.get("body"), Mapping):
        return payload["body"]
    return payload


def compute_5w1h_completeness(
    *,
    intent_path: Path,
    profile: str | Path = "base",
    plan_path: Path | None = None,
    network_path: Path | None = None,
    mode: str | Mode = Mode.DESIGN,
    dispositions: Mapping[Dimension, Any] | None = None,
    patterns_dir: Path | None = None,
) -> CompletenessOutcome:
    """Judge a single Intent's 5W1H structural completeness (deterministic).

    Two-phase (spec §3b):

    * ``mode="design"`` (default) — exploratory. A keyword heuristic scans
      prose so a hard ``missing`` on an error dim is downgraded to a
      non-blocking ``partial`` when the concept is mentioned in narrative.
      Partials are tolerated (warnings, not errors).
    * ``mode="seal"`` — strict gate. No heuristic; every error-severity dim
      must be structurally ``satisfied`` **or closed by an accountable
      disposition** (``resolved``/``waived``/``dynamic``, Task 7-0.11) or it
      blocks (exit 1). ``dispositions`` maps a :class:`Dimension` to a
      ``tm.intent.uncertainty.Disposition`` and is consumed only in seal mode.
    """
    from tm.policy.deterministic import canonical_json_bytes
    from tm.intent.uncertainty import DispositionKind, validate_disposition

    mode_enum = mode if isinstance(mode, Mode) else Mode(str(mode).strip().lower())
    dispositions = dict(dispositions or {})
    resolved_profile = profile if isinstance(profile, Profile) else load_profile(profile)
    body = _load_intent_mapping(Path(intent_path))
    plan = _load_intent_mapping(Path(plan_path)) if plan_path is not None else None
    network = _load_intent_mapping(Path(network_path)) if network_path is not None else None
    pattern_categories = _load_pattern_categories(patterns_dir)

    raw_status: dict[Dimension, tuple[DimStatus, list[str], str | None]] = {
        Dimension.WHO: _eval_who(body),
        Dimension.WHY: _eval_why(body),
        Dimension.WHAT: _eval_what(body),
        Dimension.WHEN: _eval_when(body, plan, pattern_categories),
        Dimension.WHERE: _eval_where(body, network),
        Dimension.HOW: _eval_how(body),
    }

    verdicts: dict[Dimension, DimensionVerdict] = {}
    for dim in ALL_DIMENSIONS:
        severity = resolved_profile.severity(dim)
        status, evidence, reason = raw_status[dim]
        if severity is Severity.OFF:
            verdicts[dim] = DimensionVerdict(dim, DimStatus.NOT_APPLICABLE, severity, (), None, None)
            continue
        suggestion = None
        # design-mode heuristic: prose mention downgrades a hard MISSING to a
        # tolerated PARTIAL (signal during exploration, never a seal gate).
        if mode_enum is Mode.DESIGN and status is DimStatus.MISSING:
            kw = _scan_prose(body, dim, resolved_profile)
            if kw is not None:
                status = DimStatus.PARTIAL
                evidence = [f"heuristic:{kw}"]
                suggestion = (
                    f"prose hint '{kw}' found; formalize into a structured "
                    f"{dim.value} field before seal"
                )
        if status is not DimStatus.SATISFIED and suggestion is None:
            suggestion = resolved_profile.hint(dim) or reason
        verdicts[dim] = DimensionVerdict(
            dim, status, severity, tuple(evidence), reason if status is not DimStatus.SATISFIED else None, suggestion
        )

    # Domain required_slots enforcement (Task 7-0.4): a missing required slot
    # degrades the profile-designated dimension (default What) and names the
    # offending slot(s). Deterministic; never silently passes.
    missing_slots = _missing_required_slots(body, resolved_profile)
    if missing_slots:
        target = resolved_profile.required_slots_dimension
        tv = verdicts.get(target)
        if tv is not None and tv.severity is not Severity.OFF:
            note = "missing required slot(s): " + ", ".join(missing_slots)
            new_status = DimStatus.PARTIAL if tv.status is DimStatus.SATISFIED else tv.status
            new_reason = note if not tv.missing_reason else f"{tv.missing_reason}; {note}"
            new_suggestion = tv.suggestion or (
                f"fill required slot(s) under slot_fills: {', '.join(missing_slots)}"
            )
            verdicts[target] = replace(
                tv, status=new_status, missing_reason=new_reason, suggestion=new_suggestion
            )

    counts = {s: 0 for s in DimStatus}
    errors = 0
    warnings = 0
    closed_by_disposition = 0
    dim_extra: dict[Dimension, dict[str, Any]] = {}
    for dim in ALL_DIMENSIONS:
        v = verdicts[dim]
        counts[v.status] += 1
        satisfied = v.status in (DimStatus.SATISFIED, DimStatus.NOT_APPLICABLE)

        if mode_enum is not Mode.SEAL:
            if satisfied:
                continue
            if v.severity is Severity.ERROR:
                if v.status is DimStatus.MISSING:
                    errors += 1  # design tolerates partial, blocks hard missing
                else:
                    warnings += 1
            elif v.severity is Severity.WARN:
                warnings += 1
            continue

        # ── seal: strict + accountable closure ──
        disp = dispositions.get(dim)
        closed = satisfied
        closure_reason: str | None = None
        if disp is not None and not satisfied:
            disp_errors = validate_disposition(disp)
            if disp_errors:
                closure_reason = "; ".join(disp_errors)
            elif disp.kind is DispositionKind.RESOLVED:
                closure_reason = "marked resolved but dimension is not structurally satisfied"
            else:  # WAIVED / DYNAMIC, validated
                closed = True
                closed_by_disposition += 1
        dim_extra[dim] = {
            "disposition": disp.to_dict() if disp is not None else None,
            "closed": closed,
            "closure_reason": closure_reason,
        }
        if satisfied:
            continue
        if v.severity is Severity.ERROR and not closed:
            errors += 1
        elif v.severity is Severity.WARN:
            warnings += 1

    missing_dimensions = sorted(
        d.value for d, v in verdicts.items() if v.status is DimStatus.MISSING
    )

    dims_report: dict[str, Any] = {}
    for dim in ALL_DIMENSIONS:
        entry = dict(verdicts[dim].to_dict())
        if mode_enum is Mode.SEAL:
            entry.update(dim_extra[dim])
        dims_report[dim.value] = entry

    report: dict[str, Any] = {
        "profile": resolved_profile.profile_id,
        "mode": mode_enum.value,
        "intent_id": str(body.get("intent_id", "")),
        "dimensions": dims_report,
        "missing_dimensions": missing_dimensions,
        "missing_required_slots": missing_slots,
        "summary": {
            "total": len(ALL_DIMENSIONS),
            "satisfied": counts[DimStatus.SATISFIED],
            "partial": counts[DimStatus.PARTIAL],
            "missing": counts[DimStatus.MISSING],
            "not_applicable": counts[DimStatus.NOT_APPLICABLE],
            "errors": errors,
            "warnings": warnings,
        },
    }
    if mode_enum is Mode.SEAL:
        report["sealed"] = errors == 0
        report["summary"]["closed_by_disposition"] = closed_by_disposition
        report["closure"] = {
            dim.value: dispositions[dim].kind.value
            for dim in ALL_DIMENSIONS
            if dim in dispositions
        }
    canonical = json.loads(canonical_json_bytes(report).decode("utf-8"))
    return CompletenessOutcome(exit_code=1 if errors else 0, report=canonical)


__all__ = [
    "ALL_DIMENSIONS",
    "CompletenessOutcome",
    "Dimension",
    "DimStatus",
    "DimensionVerdict",
    "Mode",
    "PROFILES_DIR",
    "Profile",
    "Severity",
    "compute_5w1h_completeness",
    "load_profile",
]
