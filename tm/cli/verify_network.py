"""CLI handler for ``tm verify network`` — Phase 6 Stage 6-4.4."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tm.verify.network import (
    load_agent_bundle_body,
    load_agent_network_body,
    load_formulas,
    network_verify,
)


def run_verify_network(args) -> int:
    network_path = Path(args.agent_network)
    if not network_path.exists():
        print(f"verify network: file not found: {network_path}", file=sys.stderr)
        return 1

    try:
        network = load_agent_network_body(network_path)
    except (ValueError, TypeError, KeyError) as exc:
        print(f"verify network: failed to load AgentNetwork: {exc}", file=sys.stderr)
        return 1

    bundle_paths: dict[str, Path] = {}
    for entry in args.bundle or []:
        if "=" not in entry:
            print(f"verify network: bundle must be ref=path, got '{entry}'", file=sys.stderr)
            return 1
        ref, raw_path = entry.split("=", 1)
        bundle_paths[ref.strip()] = Path(raw_path.strip())

    required = [network.center_bundle_ref, *network.leaf_bundle_refs]
    missing = [ref for ref in required if ref not in bundle_paths]
    if missing:
        print(f"verify network: missing bundle paths for: {', '.join(missing)}", file=sys.stderr)
        return 1

    bundles = {}
    for ref, path in bundle_paths.items():
        if not path.exists():
            print(f"verify network: bundle not found: {path}", file=sys.stderr)
            return 1
        try:
            bundles[ref] = load_agent_bundle_body(path)
        except (ValueError, TypeError, KeyError) as exc:
            print(f"verify network: failed to load bundle '{ref}': {exc}", file=sys.stderr)
            return 1

    formulas_path = Path(args.formulas) if args.formulas else None
    try:
        formulas = load_formulas(formulas_path)
    except ValueError as exc:
        print(f"verify network: {exc}", file=sys.stderr)
        return 1
    if not formulas:
        print("verify network: no formulas provided (--formulas required)", file=sys.stderr)
        return 1

    mode = getattr(args, "mode", "monolithic")
    try:
        report = network_verify(
            network,
            bundles,
            formulas,
            mode=mode,
            max_depth=int(args.max_depth),
            hash_mode=args.hash_mode,
        )
    except (ValueError, KeyError) as exc:
        print(f"verify network: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        header = (
            f"network={report.network_id} mode={report.mode} "
            f"verified={report.verified} states={report.state_count} "
            f"deadlocks={report.deadlock_count}"
        )
        if report.mode == "compositional" and report.monolithic_state_count is not None:
            header += (
                f" (compositional={report.compositional_state_count} "
                f"vs monolithic={report.monolithic_state_count})"
            )
        print(header)
        for verdict in report.verdicts:
            status = "OK" if verdict.satisfied else "FAIL"
            print(f"  {status}: {verdict.formula}")
            if not verdict.satisfied and verdict.counterexample:
                print(f"    counterexample_steps={len(verdict.counterexample)}")
        for fb in report.fallbacks:
            print(f"  fallback[{fb['trigger']}]: {fb['formula']}")

    return 0 if report.verified else 1


__all__ = ["run_verify_network"]
