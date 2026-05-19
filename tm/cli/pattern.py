"""``tm pattern`` CLI — list / show / instantiate seed PropertyPatterns.

Stage 5-3 task 3.2: provides a non-LLM, declarative entry point for
working with the Pattern Library. Three subcommands:

- ``tm pattern list``                — show all loaded patterns
- ``tm pattern show <pattern_id>``    — print a single pattern's full body
- ``tm pattern instantiate <pat_id> --slot k=v ... --output instance.yaml``
                                      — produce a concrete pattern instance

All output is fully deterministic (sorted, canonical JSON / YAML), making
this CLI suitable for use in CI pipelines and pre-commit hooks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tm.patterns import (
    PatternInstantiationError,
    instantiate_pattern,
    load_seed_patterns,
)
from tm.patterns.library import PatternLibrary
from tm.utils.yaml import import_yaml


def _load_library(args: argparse.Namespace) -> PatternLibrary:
    """Load the library: shipped seeds by default, or a custom dir via --library."""
    custom = getattr(args, "library", None)
    if custom:
        return PatternLibrary.from_directory(Path(custom).expanduser())
    return load_seed_patterns()


def _format_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return ""
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            v = str(row.get(c, ""))
            if len(v) > widths[c]:
                widths[c] = min(len(v), 80)
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    divider = "  ".join("-" * widths[c] for c in columns)
    lines = [header, divider]
    for row in rows:
        line = "  ".join(str(row.get(c, ""))[: widths[c]].ljust(widths[c]) for c in columns)
        lines.append(line)
    return "\n".join(lines)


# ─── list ─────────────────────────────────────────────────────────


def _cmd_pattern_list(args: argparse.Namespace) -> int:
    try:
        lib = _load_library(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"pattern list: {exc}", file=sys.stderr)
        return 1

    rows = lib.summary()
    if args.category:
        rows = [r for r in rows if r["category"] == args.category]

    if args.json:
        print(json.dumps({"patterns": rows}, indent=2, sort_keys=True))
        return 0

    if not rows:
        print("(no patterns)")
        return 0

    display = [
        {
            "pattern_id": r["pattern_id"],
            "category": r["category"],
            "slots": ",".join(r["slots"]),
            "title": r["title"],
        }
        for r in rows
    ]
    print(_format_table(display, ["pattern_id", "category", "slots", "title"]))
    return 0


# ─── show ─────────────────────────────────────────────────────────


def _serialize_body(entry) -> dict[str, Any]:
    body = entry.body
    return {
        "pattern_id": body.pattern_id,
        "category": body.category,
        "title": body.title,
        "description": body.description,
        "formula_template": body.formula_template,
        "slots": [
            {
                "name": s.name,
                "type": s.type,
                "description": s.description,
                "required": s.required,
            }
            for s in body.slots
        ],
        "applicable_conditions": list(body.applicable_conditions),
        "counterexamples": [{"description": c.description, "scenario": c.scenario} for c in body.counterexamples],
        "metadata": dict(body.metadata),
        "_source_path": str(entry.path),
    }


def _cmd_pattern_show(args: argparse.Namespace) -> int:
    try:
        lib = _load_library(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"pattern show: {exc}", file=sys.stderr)
        return 1

    try:
        entry = lib.get(args.pattern_id)
    except KeyError as exc:
        print(f"pattern show: {exc}", file=sys.stderr)
        return 1

    body_dict = _serialize_body(entry)

    if args.json:
        print(json.dumps(body_dict, indent=2, sort_keys=True))
        return 0

    yaml = import_yaml()
    if yaml is None:
        print("pattern show: PyYAML is required for non-JSON output", file=sys.stderr)
        return 1
    print(yaml.safe_dump(body_dict, sort_keys=False, allow_unicode=True), end="")
    return 0


# ─── instantiate ──────────────────────────────────────────────────


def _parse_slot(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--slot must be KEY=VALUE form, got: {raw!r}")
    key, _, value = raw.partition("=")
    key = key.strip()
    value = value.strip()
    if not key:
        raise argparse.ArgumentTypeError(f"--slot key must be non-empty, got: {raw!r}")
    return key, value


def _serialize_instance(instance, source_path: Path | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pattern_id": instance.pattern_id,
        "category": instance.category,
        "title": instance.title,
        "resolved_formula": instance.resolved_formula,
        "source_template": instance.source_template,
        "slot_fills": dict(instance.slot_fills),
    }
    if source_path is not None:
        out["_source_pattern_path"] = str(source_path)
    return out


def _cmd_pattern_instantiate(args: argparse.Namespace) -> int:
    try:
        lib = _load_library(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"pattern instantiate: {exc}", file=sys.stderr)
        return 1

    try:
        entry = lib.get(args.pattern_id)
    except KeyError as exc:
        print(f"pattern instantiate: {exc}", file=sys.stderr)
        return 1

    slot_fills: dict[str, str] = {}
    for raw in args.slot or []:
        try:
            k, v = _parse_slot(raw)
        except argparse.ArgumentTypeError as exc:
            print(f"pattern instantiate: {exc}", file=sys.stderr)
            return 2
        if k in slot_fills:
            print(
                f"pattern instantiate: slot '{k}' provided multiple times",
                file=sys.stderr,
            )
            return 2
        slot_fills[k] = v

    try:
        instance = instantiate_pattern(entry, slot_fills, title=args.title, validate_formula=not args.no_validate)
    except PatternInstantiationError as exc:
        print(f"pattern instantiate: {exc}", file=sys.stderr)
        return 1

    out = _serialize_instance(instance, source_path=entry.path)

    if args.output:
        target = Path(args.output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = target.suffix.lower()
        if suffix == ".json":
            target.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            yaml = import_yaml()
            if yaml is None:
                print(
                    "pattern instantiate: PyYAML is required for YAML output",
                    file=sys.stderr,
                )
                return 1
            target.write_text(
                yaml.safe_dump(out, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        if not args.quiet:
            print(f"wrote {target}", file=sys.stderr)
    else:
        if args.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            yaml = import_yaml()
            if yaml is None:
                print(json.dumps(out, indent=2, sort_keys=True))
            else:
                print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True), end="")
    return 0


# ─── registration ─────────────────────────────────────────────────


def register_pattern_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("pattern", help="manage and instantiate PropertyPattern templates")
    parser.add_argument(
        "--library",
        help="custom pattern directory (defaults to shipped seed library)",
    )
    pattern_sub = parser.add_subparsers(dest="pattern_cmd")
    pattern_sub.required = True

    list_p = pattern_sub.add_parser("list", help="list all loaded patterns")
    list_p.add_argument(
        "--category",
        choices=["safety", "liveness", "fairness"],
        help="filter by pattern category",
    )
    list_p.add_argument("--json", action="store_true", help="emit JSON")
    list_p.set_defaults(func=_cmd_pattern_list)

    show_p = pattern_sub.add_parser("show", help="show a single pattern's full body")
    show_p.add_argument("pattern_id", help="pattern id (e.g. safety.no_x_amplifies_y)")
    show_p.add_argument("--json", action="store_true", help="emit JSON")
    show_p.set_defaults(func=_cmd_pattern_show)

    inst_p = pattern_sub.add_parser(
        "instantiate",
        help="produce a concrete PatternInstance from a template + slot fills",
    )
    inst_p.add_argument("pattern_id", help="pattern id (e.g. safety.no_x_amplifies_y)")
    inst_p.add_argument(
        "--slot",
        action="append",
        metavar="KEY=VALUE",
        help="slot fill — repeatable, e.g. --slot forbidden_predicate=has(x)",
    )
    inst_p.add_argument("--title", help="custom title for this instance (defaults to pattern title)")
    inst_p.add_argument("--output", help="write to this path (.yaml/.json); print to stdout if omitted")
    inst_p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON to stdout (ignored when --output is set)",
    )
    inst_p.add_argument(
        "--no-validate",
        action="store_true",
        help="skip CTL validation of resolved formula",
    )
    inst_p.add_argument(
        "--quiet",
        action="store_true",
        help="suppress 'wrote X' confirmation when --output is set",
    )
    inst_p.set_defaults(func=_cmd_pattern_instantiate)


__all__ = ["register_pattern_commands"]
