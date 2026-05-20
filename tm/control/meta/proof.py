"""ProofReport — structured evidence that a MAPE-K cycle achieved its intent.

A ProofReport captures:

- pre/post environment snapshots (what changed)
- execution report (what was done)
- kripke verification verdict (formal properties)
- evidence chain (complete audit trail)
- overall verdict (pass / fail / inconclusive)

Domain-neutral: the ``intent_id`` is supplied by the caller. The generator
makes no assumptions about which intents or KPIs the cycle was targeting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass
class KripkeVerdict:
    """Result of formal property verification."""

    verified: bool
    properties_checked: int
    properties_passed: int
    failed_properties: list[str] = field(default_factory=list)
    counterexamples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if self.properties_checked == 0:
            return Verdict.INCONCLUSIVE
        if not self.verified:
            return Verdict.FAIL
        return Verdict.PASS


@dataclass
class EvidenceEntry:
    """Single entry in the evidence chain."""

    source: str
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    peer_node_id: str | None = None
    peer_chain_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        # Mirror optional cross-node fields into data for artifact serialization.
        if self.peer_node_id is not None:
            self.data.setdefault("peer_node_id", self.peer_node_id)
        if self.peer_chain_ref is not None:
            self.data.setdefault("peer_chain_ref", self.peer_chain_ref)


@dataclass
class ProofReport:
    """Complete proof that a control cycle achieved its intent."""

    report_id: str
    intent_id: str
    cycle_id: str

    pre_snapshot: dict[str, Any]
    post_snapshot: dict[str, Any]
    execution_summary: dict[str, Any]

    kripke_verdict: KripkeVerdict
    evidence_chain: list[EvidenceEntry]
    policy_decisions: list[dict[str, Any]]

    overall_verdict: Verdict
    verdict_reason: str

    created_at: str = ""
    report_hash: str = ""
    peer_node_id: str | None = None
    peer_chain_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.report_hash:
            self.report_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        peer_refs = sorted(
            entry.peer_chain_ref or entry.data.get("peer_chain_ref") or ""
            for entry in self.evidence_chain
            if entry.event_type == "peer_proof_report" and (entry.peer_chain_ref or entry.data.get("peer_chain_ref"))
        )
        payload = {
            "report_id": self.report_id,
            "intent_id": self.intent_id,
            "cycle_id": self.cycle_id,
            "overall_verdict": self.overall_verdict.value,
            "kripke_verified": self.kripke_verdict.verified,
            "evidence_count": len(self.evidence_chain),
            "peer_chain_refs": peer_refs,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def recompute_hash(self) -> None:
        """Recompute ``report_hash`` after mutating evidence (cross-node attach)."""
        self.report_hash = self._compute_hash()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall_verdict"] = self.overall_verdict.value
        d["kripke_verdict"]["verdict"] = self.kripke_verdict.verdict.value
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProofReport":
        kv_raw = data.get("kripke_verdict", {})
        kripke = KripkeVerdict(
            verified=bool(kv_raw.get("verified", False)),
            properties_checked=int(kv_raw.get("properties_checked", 0)),
            properties_passed=int(kv_raw.get("properties_passed", 0)),
            failed_properties=list(kv_raw.get("failed_properties", [])),
            counterexamples=list(kv_raw.get("counterexamples", [])),
        )
        evidence = [
            EvidenceEntry(
                source=str(e.get("source", "")),
                event_type=str(e.get("event_type", "")),
                data=dict(e.get("data", {})),
                timestamp=str(e.get("timestamp", "")),
                peer_node_id=(str(e.get("peer_node_id")) if e.get("peer_node_id") is not None else None),
                peer_chain_ref=(str(e.get("peer_chain_ref")) if e.get("peer_chain_ref") is not None else None),
            )
            for e in data.get("evidence_chain", [])
        ]
        pd = [dict(p) for p in data.get("policy_decisions", [])]
        verdict_str = str(data.get("overall_verdict", "inconclusive"))
        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.INCONCLUSIVE

        return cls(
            report_id=str(data.get("report_id", "")),
            intent_id=str(data.get("intent_id", "")),
            cycle_id=str(data.get("cycle_id", "")),
            pre_snapshot=dict(data.get("pre_snapshot", {})),
            post_snapshot=dict(data.get("post_snapshot", {})),
            execution_summary=dict(data.get("execution_summary", {})),
            kripke_verdict=kripke,
            evidence_chain=evidence,
            policy_decisions=pd,
            overall_verdict=verdict,
            verdict_reason=str(data.get("verdict_reason", "")),
            created_at=str(data.get("created_at", "")),
            report_hash=str(data.get("report_hash", "")),
            peer_node_id=(str(data.get("peer_node_id")) if data.get("peer_node_id") is not None else None),
            peer_chain_ref=(str(data.get("peer_chain_ref")) if data.get("peer_chain_ref") is not None else None),
        )


class ProofReportGenerator:
    """Generates a ProofReport from ControllerCycle results.

    Usage::

        gen = ProofReportGenerator(intent_id="my.intent.id")
        report = gen.generate(
            cycle_result=result,
            pre_snapshot=old_snap,
            kripke_verdict=kv,
        )
    """

    def __init__(self, intent_id: str = ""):
        self.intent_id = intent_id

    def generate(
        self,
        cycle_result: Any,
        pre_snapshot: Mapping[str, Any] | None = None,
        kripke_verdict: KripkeVerdict | None = None,
        extra_evidence: Sequence[EvidenceEntry] | None = None,
    ) -> ProofReport:
        bundle_id = str(getattr(cycle_result, "bundle_artifact_id", "unknown"))
        post_snapshot = self._extract_snapshot(cycle_result)
        exec_summary = self._extract_execution_summary(cycle_result)
        policy_decisions = self._extract_policy_decisions(cycle_result)
        evidence = self._build_evidence_chain(cycle_result, extra_evidence)

        kv = kripke_verdict or KripkeVerdict(
            verified=False,
            properties_checked=0,
            properties_passed=0,
        )

        verdict, reason = self._determine_verdict(exec_summary, kv, policy_decisions)

        return ProofReport(
            report_id=f"proof-{bundle_id}",
            intent_id=self.intent_id,
            cycle_id=bundle_id,
            pre_snapshot=dict(pre_snapshot) if pre_snapshot else {},
            post_snapshot=post_snapshot,
            execution_summary=exec_summary,
            kripke_verdict=kv,
            evidence_chain=evidence,
            policy_decisions=policy_decisions,
            overall_verdict=verdict,
            verdict_reason=reason,
        )

    def _extract_snapshot(self, result: Any) -> dict[str, Any]:
        snap = getattr(result, "env_snapshot", None)
        if snap is None:
            return {}
        body = getattr(snap, "body", None)
        if body is None:
            return {}
        from dataclasses import asdict as _asdict

        try:
            return _asdict(body)
        except TypeError:
            return {}

    def _extract_execution_summary(self, result: Any) -> dict[str, Any]:
        report = getattr(result, "execution_report", None)
        if report is None:
            return {"status": "no_report"}
        body = getattr(report, "body", None)
        if body is None:
            return {"status": "no_body"}
        return {
            "report_id": getattr(body, "report_id", ""),
            "status": getattr(body, "status", ""),
            "artifact_refs": dict(getattr(body, "artifact_refs", {})),
            "errors": list(getattr(body, "errors", [])),
        }

    def _extract_policy_decisions(self, result: Any) -> list[dict[str, Any]]:
        decisions = getattr(result, "policy_decisions", [])
        out: list[dict[str, Any]] = []
        for d in decisions:
            if isinstance(d, Mapping):
                out.append(dict(d))
            elif hasattr(d, "effect_ref"):
                out.append(
                    {
                        "effect_ref": getattr(d, "effect_ref", ""),
                        "allowed": getattr(d, "allowed", False),
                        "reason": getattr(d, "reason", ""),
                    }
                )
        return out

    def _build_evidence_chain(
        self,
        result: Any,
        extra: Sequence[EvidenceEntry] | None,
    ) -> list[EvidenceEntry]:
        chain: list[EvidenceEntry] = []

        chain.append(
            EvidenceEntry(
                source="controller_cycle",
                event_type="cycle_completed",
                data={
                    "bundle": str(getattr(result, "bundle_artifact_id", "")),
                    "start": str(getattr(result, "start_time", "")),
                    "end": str(getattr(result, "end_time", "")),
                },
            )
        )

        exec_summary = self._extract_execution_summary(result)
        if exec_summary.get("status"):
            chain.append(
                EvidenceEntry(
                    source="execution_report",
                    event_type="execution_status",
                    data={"status": exec_summary["status"]},
                )
            )

        if extra:
            chain.extend(extra)

        return chain

    def _determine_verdict(
        self,
        exec_summary: dict[str, Any],
        kripke: KripkeVerdict,
        policy_decisions: list[dict[str, Any]],
    ) -> tuple[Verdict, str]:
        reasons: list[str] = []
        has_failure = False

        exec_status = exec_summary.get("status", "")
        if exec_status == "succeeded":
            reasons.append("execution succeeded")
        elif exec_status == "failed":
            reasons.append("execution failed")
            has_failure = True
        elif exec_status == "partial":
            reasons.append("execution partially succeeded")
        else:
            reasons.append(f"execution status: {exec_status}")

        if kripke.properties_checked > 0:
            if kripke.verified:
                reasons.append(f"all {kripke.properties_checked} properties verified")
            else:
                failed = ", ".join(kripke.failed_properties) or "unknown"
                reasons.append(f"kripke failed: {failed}")
                has_failure = True
        else:
            reasons.append("no kripke properties checked")

        denied = [d for d in policy_decisions if not d.get("allowed", True)]
        if denied:
            reasons.append(f"{len(denied)} policy decisions denied")
            has_failure = True
        elif policy_decisions:
            reasons.append("all policy decisions allowed")

        if has_failure:
            return Verdict.FAIL, "; ".join(reasons)

        if not kripke.properties_checked and not policy_decisions:
            return Verdict.INCONCLUSIVE, "; ".join(reasons)

        return Verdict.PASS, "; ".join(reasons)


# ─── Snapshot Diff ─────────────────────────────────────────────────


@dataclass
class SnapshotDiffEntry:
    """One changed metric between pre and post snapshot."""

    key: str
    pre_value: Any
    post_value: Any
    delta: float | None = None


@dataclass
class SnapshotDiff:
    """Structured diff between pre- and post-cycle snapshots."""

    changed: list[SnapshotDiffEntry]
    added: list[str]
    removed: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": [
                {
                    "key": e.key,
                    "pre": e.pre_value,
                    "post": e.post_value,
                    "delta": e.delta,
                }
                for e in self.changed
            ],
            "added": self.added,
            "removed": self.removed,
        }


def diff_snapshots(
    pre: Mapping[str, Any],
    post: Mapping[str, Any],
) -> SnapshotDiff:
    """Compare pre and post env snapshots, returning structured diff."""
    pre_metrics = _extract_metrics(pre)
    post_metrics = _extract_metrics(post)

    all_keys = set(pre_metrics.keys()) | set(post_metrics.keys())
    changed: list[SnapshotDiffEntry] = []
    added: list[str] = []
    removed: list[str] = []

    for key in sorted(all_keys):
        if key not in pre_metrics:
            added.append(key)
        elif key not in post_metrics:
            removed.append(key)
        elif pre_metrics[key] != post_metrics[key]:
            pre_val = pre_metrics[key]
            post_val = post_metrics[key]
            delta = None
            try:
                delta = float(post_val) - float(pre_val)
            except (ValueError, TypeError):
                pass
            changed.append(
                SnapshotDiffEntry(
                    key=key,
                    pre_value=pre_val,
                    post_value=post_val,
                    delta=delta,
                )
            )

    return SnapshotDiff(changed=changed, added=added, removed=removed)


def _extract_metrics(snap: Mapping[str, Any]) -> dict[str, Any]:
    env = snap.get("environment", {})
    if isinstance(env, Mapping):
        m = env.get("metrics", {})
        if isinstance(m, Mapping):
            return dict(m)
    return {}


# ─── Hash Chain ────────────────────────────────────────────────────


def build_hash_chain(report: ProofReport) -> list[dict[str, str]]:
    """Build an ordered hash chain linking all proof artifacts.

    Each entry hashes the previous entry's hash + the current artifact,
    forming a tamper-evident chain.
    """
    chain: list[dict[str, str]] = []
    prev_hash = ""

    items = [
        ("pre_snapshot", json.dumps(report.pre_snapshot, sort_keys=True)),
        ("execution_summary", json.dumps(report.execution_summary, sort_keys=True)),
        ("post_snapshot", json.dumps(report.post_snapshot, sort_keys=True)),
        ("kripke_verdict", json.dumps(asdict(report.kripke_verdict), sort_keys=True)),
        (
            "evidence_chain",
            json.dumps([asdict(e) for e in report.evidence_chain], sort_keys=True),
        ),
        ("overall_verdict", report.overall_verdict.value),
    ]

    for name, payload in items:
        content = f"{prev_hash}:{payload}"
        entry_hash = hashlib.sha256(content.encode()).hexdigest()
        chain.append({"artifact": name, "hash": entry_hash, "prev_hash": prev_hash})
        prev_hash = entry_hash

    return chain


def verify_hash_chain(chain: Sequence[dict[str, str]], report: ProofReport) -> bool:
    """Verify that a hash chain is internally consistent and matches the report."""
    expected = build_hash_chain(report)
    if len(chain) != len(expected):
        return False
    return all(c["hash"] == e["hash"] for c, e in zip(chain, expected))


# ─── Cross-node peer chain ─────────────────────────────────────────


def attach_peer_proofs(
    center_proof: ProofReport,
    peer_reports: Sequence[tuple[str, ProofReport]],
) -> ProofReport:
    """Attach leaf proof hashes as peer evidence entries on a center proof.

    Each leaf contributes one ``peer_proof_report`` evidence entry whose
    ``peer_chain_ref`` is the leaf's ``report_hash``. The center hash is
    recomputed so tampering any leaf proof invalidates verification.
    """
    for peer_id, peer_proof in peer_reports:
        peer_hash = peer_proof.report_hash or peer_proof._compute_hash()
        center_proof.evidence_chain.append(
            EvidenceEntry(
                source=f"peer:{peer_id}",
                event_type="peer_proof_report",
                data={
                    "peer_report_id": peer_proof.report_id,
                    "peer_cycle_id": peer_proof.cycle_id,
                    "peer_overall_verdict": peer_proof.overall_verdict.value,
                },
                peer_node_id=peer_id,
                peer_chain_ref=peer_hash,
            )
        )
    center_proof.recompute_hash()
    return center_proof


def verify_peer_chain(
    center_proof: ProofReport,
    leaf_proofs: Mapping[str, ProofReport],
) -> tuple[bool, list[str]]:
    """Verify center peer evidence refs match the supplied leaf proofs."""
    errors: list[str] = []
    peer_entries = [e for e in center_proof.evidence_chain if e.event_type == "peer_proof_report"]

    refs_by_peer: dict[str, str] = {}
    for entry in peer_entries:
        peer_id = entry.peer_node_id or str(entry.data.get("peer_node_id") or "")
        ref = entry.peer_chain_ref or str(entry.data.get("peer_chain_ref") or "")
        if not peer_id:
            errors.append("peer evidence entry missing peer_node_id")
            continue
        refs_by_peer[peer_id] = ref

    if len(refs_by_peer) != len(leaf_proofs):
        errors.append(f"expected {len(leaf_proofs)} peer evidence entries, got {len(refs_by_peer)}")

    for peer_id, leaf_proof in sorted(leaf_proofs.items()):
        expected_hash = leaf_proof.report_hash or leaf_proof._compute_hash()
        attached_ref = refs_by_peer.get(peer_id)
        if attached_ref is None:
            errors.append(f"missing peer evidence for '{peer_id}'")
            continue
        if attached_ref != expected_hash:
            errors.append(
                f"peer chain ref mismatch for '{peer_id}': "
                f"center has '{attached_ref}', leaf hash is '{expected_hash}'"
            )

    recomputed = center_proof._compute_hash()
    if center_proof.report_hash and center_proof.report_hash != recomputed:
        errors.append(f"center report_hash '{center_proof.report_hash}' " f"does not match recomputed '{recomputed}'")

    return len(errors) == 0, errors


__all__ = [
    "EvidenceEntry",
    "KripkeVerdict",
    "ProofReport",
    "ProofReportGenerator",
    "SnapshotDiff",
    "SnapshotDiffEntry",
    "Verdict",
    "attach_peer_proofs",
    "build_hash_chain",
    "diff_snapshots",
    "verify_hash_chain",
    "verify_peer_chain",
]
