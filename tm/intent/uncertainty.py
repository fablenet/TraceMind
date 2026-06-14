"""Uncertainty closure for 5W1H seal — Phase 7 Stage 7-0 · Task 7-0.11.

When sealing a requirement (``mode=seal``), every error-severity dimension that
is not structurally ``satisfied`` must be **closed** by an explicit, accountable
disposition (spec §3b):

* ``resolved`` — the clue has been promoted to a structured field; strict
  re-check must be ``satisfied`` (else the "resolved" claim is rejected).
* ``waived`` — the uncertainty is explicitly accepted/ignored, on the record:
  requires ``rationale`` + ``signer`` (+ optional ``uncertainty_registry_ref``).
* ``dynamic`` — the value is late-bound at runtime by a **registered
  deterministic resolver** (``resolver_ref``); arbitrary code / LLM resolvers
  are rejected (invariant 3).

Hard rule: this module is **zero-LLM** and deterministic, like
``tm/policy/deterministic``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from tm.intent.completeness import Dimension
from tm.utils.yaml import import_yaml

_YAML_SUFFIXES = {".yaml", ".yml"}


class DispositionKind(str, Enum):
    """How a remaining uncertainty is closed at seal time."""

    RESOLVED = "resolved"
    WAIVED = "waived"
    DYNAMIC = "dynamic"


@dataclass(frozen=True)
class Disposition:
    """An accountable closure for one dimension's residual uncertainty."""

    kind: DispositionKind
    rationale: str | None = None
    signer: str | None = None
    uncertainty_registry_ref: str | None = None
    resolver_ref: str | None = None
    schema: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind.value}
        if self.rationale is not None:
            out["rationale"] = self.rationale
        if self.signer is not None:
            out["signer"] = self.signer
        if self.uncertainty_registry_ref is not None:
            out["uncertainty_registry_ref"] = self.uncertainty_registry_ref
        if self.resolver_ref is not None:
            out["resolver_ref"] = self.resolver_ref
        if self.schema is not None:
            out["schema"] = dict(self.schema)
        return out


# ─── Deterministic resolver registry ──────────────────────────────
#
# A ``dynamic`` disposition may only bind to a resolver registered here. Core
# ships ONLY domain-neutral deterministic resolvers; downstream products
# register their own. This keeps the binding structured and verifiable and
# forbids arbitrary code / LLM acting as a resolver (invariant 3).

_RESOLVER_REGISTRY: dict[str, Callable[[Mapping[str, Any]], Any]] = {}


def register_resolver(
    name: str, fn: Callable[[Mapping[str, Any]], Any] | None = None
):
    """Register a deterministic resolver. Usable as a decorator or directly."""

    def _do(func: Callable[[Mapping[str, Any]], Any]):
        key = str(name).strip()
        if not key:
            raise ValueError("resolver name must be non-empty")
        _RESOLVER_REGISTRY[key] = func
        return func

    return _do(fn) if fn is not None else _do


def is_registered_resolver(ref: str | None) -> bool:
    return bool(ref) and ref in _RESOLVER_REGISTRY


def registered_resolvers() -> tuple[str, ...]:
    return tuple(sorted(_RESOLVER_REGISTRY))


@register_resolver("constant")
def _constant_resolver(schema: Mapping[str, Any]) -> Any:
    """Domain-neutral deterministic resolver: returns ``schema['value']``."""
    if "value" not in schema:
        raise ValueError("constant resolver requires schema.value")
    return schema["value"]


# ─── Parsing & validation ─────────────────────────────────────────


def parse_disposition(raw: Mapping[str, Any]) -> Disposition:
    """Parse one disposition mapping. Raises on structurally invalid input."""
    if not isinstance(raw, Mapping):
        raise ValueError("disposition must be a mapping")
    kind_raw = raw.get("kind")
    try:
        kind = DispositionKind(str(kind_raw).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown disposition kind '{kind_raw}'") from exc
    schema = raw.get("schema")
    if schema is not None and not isinstance(schema, Mapping):
        raise ValueError("disposition.schema must be a mapping")
    return Disposition(
        kind=kind,
        rationale=_opt_str(raw.get("rationale")),
        signer=_opt_str(raw.get("signer")),
        uncertainty_registry_ref=_opt_str(raw.get("uncertainty_registry_ref")),
        resolver_ref=_opt_str(raw.get("resolver_ref")),
        schema=schema,
    )


def validate_disposition(disp: Disposition) -> list[str]:
    """Return a list of accountability errors; empty list = valid disposition."""
    errors: list[str] = []
    if disp.kind is DispositionKind.WAIVED:
        if not disp.rationale:
            errors.append("waived disposition requires a rationale")
        if not disp.signer:
            errors.append("waived disposition requires a signer")
    elif disp.kind is DispositionKind.DYNAMIC:
        if not disp.resolver_ref:
            errors.append("dynamic disposition requires resolver_ref")
        elif not is_registered_resolver(disp.resolver_ref):
            errors.append(
                f"resolver_ref '{disp.resolver_ref}' is not a registered "
                f"deterministic resolver (registered: {list(registered_resolvers())})"
            )
    # RESOLVED carries no extra fields; its validity is checked against the
    # dimension's structural status by the seal gate.
    return errors


def parse_dispositions(payload: Mapping[str, Any]) -> dict[Dimension, Disposition]:
    """Parse a ``dimension -> disposition`` mapping (in-memory; no file I/O)."""
    if not isinstance(payload, Mapping):
        raise ValueError("dispositions must be a mapping of dimension -> disposition")
    out: dict[Dimension, Disposition] = {}
    for dim_raw, raw in payload.items():
        try:
            dim = Dimension(str(dim_raw).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown 5W1H dimension '{dim_raw}' in dispositions") from exc
        out[dim] = parse_disposition(raw)
    return out


def load_dispositions(path: Path) -> dict[Dimension, Disposition]:
    """Load a ``dimension -> disposition`` mapping from a JSON/YAML file."""
    path = Path(path).expanduser()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in _YAML_SUFFIXES:
        yaml = import_yaml()
        if yaml is None:
            raise ValueError("PyYAML is required to read YAML dispositions")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: dispositions must be a mapping of dimension -> disposition")
    return parse_dispositions(payload)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "Disposition",
    "DispositionKind",
    "is_registered_resolver",
    "load_dispositions",
    "parse_disposition",
    "parse_dispositions",
    "register_resolver",
    "registered_resolvers",
    "validate_disposition",
]
