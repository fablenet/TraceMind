"""Canonical turn-journal hash chain + replay determinism (Stage 7-2.6).

Every transition appends a turn whose ``turn_hash`` canonically commits to the
turn content *and* its predecessor's hash, making the journal a tamper-evident
chain. The same action sequence replays to byte-identical hashes (determinism),
and any tamper / reorder / drop is caught by :func:`verify_journal`. The chain
head is the Phase-8 zero-trust signing target (signature itself is out of scope).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tm.intent.design_loop import DesignStep
from tm.intent.session import (
    GateFacts,
    advance,
    new_session,
    revert,
    turn_content_hash,
    verify_journal,
)
from tm.intent.session_store import SessionStore


def _drive() -> "object":
    s = new_session("session.j", "intent.j")
    s = advance(s, GateFacts(), role="agent")  # check_5w1h
    s = advance(s, GateFacts(), role="agent")  # propose
    s = advance(s, GateFacts(), role="agent")  # refine
    s = revert(s, DesignStep.CHECK_5W1H, role="human", reason="goal changed")
    return s


# ─── hashing + integrity ────────────────────────────────────────────


def test_every_turn_gets_a_hash() -> None:
    s = _drive()
    assert all(t.turn_hash for t in s.turns)
    assert verify_journal(s) == []


def test_chain_links_to_predecessor() -> None:
    s = _drive()
    prev = ""
    for turn in s.turns:
        assert turn.turn_hash == turn_content_hash(turn, prev)
        prev = turn.turn_hash


def test_head_is_phase8_signing_target_nonempty() -> None:
    s = _drive()
    assert isinstance(s.turns[-1].turn_hash, str) and len(s.turns[-1].turn_hash) == 64


# ─── replay determinism ─────────────────────────────────────────────


def test_same_sequence_replays_identical_hashes() -> None:
    a = _drive()
    b = _drive()
    assert [t.turn_hash for t in a.turns] == [t.turn_hash for t in b.turns]


def test_different_first_action_diverges_chain() -> None:
    # chain sensitivity: a different early turn changes every downstream hash
    base = new_session("session.j", "intent.j")
    a = advance(base, GateFacts(), role="agent")  # check_5w1h (agent)
    b = revert(advance(advance(base, GateFacts(), role="agent"), GateFacts(), role="agent"), DesignStep.DRAFT, role="human", reason="x")
    # b's first two turns differ in content from a's single turn → hashes differ
    assert a.turns[0].turn_hash != b.turns[-1].turn_hash


# ─── tamper / reorder / drop detection ──────────────────────────────


def test_tampered_content_is_detected() -> None:
    s = _drive()
    tampered_turns = list(s.turns)
    tampered_turns[1] = replace(tampered_turns[1], output_ref="HACKED")  # keep stale hash
    tampered = replace(s, turns=tampered_turns)
    issues = verify_journal(tampered)
    assert any("turns[1]" in i and "mismatch" in i for i in issues)


def test_dropped_turn_is_detected() -> None:
    s = _drive()
    dropped = replace(s, turns=[s.turns[0], *s.turns[2:]])
    issues = verify_journal(dropped)
    assert issues  # the turn after the gap no longer chains to its recorded predecessor


def test_missing_hash_is_detected() -> None:
    s = _drive()
    broken = replace(s, turns=[replace(s.turns[0], turn_hash=None), *s.turns[1:]])
    issues = verify_journal(broken)
    assert any("missing turn_hash" in i for i in issues)


# ─── persistence round-trip keeps the chain intact ─────────────────


def test_store_roundtrip_preserves_journal(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    s = _drive()
    store.save(s)
    loaded = store.load(s.session_id)
    assert [t.turn_hash for t in loaded.turns] == [t.turn_hash for t in s.turns]
    assert verify_journal(loaded) == []
