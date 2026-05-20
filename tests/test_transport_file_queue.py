"""Tests for :class:`tm.transport.file_queue.FileQueueTransport` — Phase 6 Stage 6-2.1d.

Covers the at-least-once + idempotency-dedup contract and the RPC
poll/reply loop. Every test uses ``tmp_path`` so the filesystem is real
but isolated; nothing escapes the per-test directory.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tm.transport import Transport, TransportError, TransportNetworkError, TransportTimeout
from tm.transport.file_queue import FileQueueConfig, FileQueueTransport


def _pair(tmp_path: Path) -> tuple[FileQueueTransport, FileQueueTransport]:
    """Build a (sender, receiver) pair sharing the same root."""
    sender = FileQueueTransport(
        my_peer_id="sender",
        root_dir=tmp_path,
        known_peers=["receiver"],
        config=FileQueueConfig(dedup_window=128, poll_interval_s=0.005),
    )
    receiver = FileQueueTransport(
        my_peer_id="receiver",
        root_dir=tmp_path,
        known_peers=["sender"],
        config=FileQueueConfig(dedup_window=128, poll_interval_s=0.005),
    )
    return sender, receiver


# ─── Construction & layout ────────────────────────────────────────


class TestConstruction:
    def test_empty_peer_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            FileQueueTransport(my_peer_id="", root_dir=tmp_path)

    def test_directory_layout_created(self, tmp_path: Path) -> None:
        FileQueueTransport(my_peer_id="me", root_dir=tmp_path, known_peers=["peer.a"])
        for peer in ("me", "peer.a"):
            base = tmp_path / "peers" / peer
            assert (base / "inbox").is_dir()
            assert (base / "processed").is_dir()
            assert (base / "responses").is_dir()

    def test_satisfies_transport_protocol(self, tmp_path: Path) -> None:
        t = FileQueueTransport(my_peer_id="me", root_dir=tmp_path)
        assert isinstance(t, Transport)

    def test_peers_lists_seen_peers(self, tmp_path: Path) -> None:
        t = FileQueueTransport(my_peer_id="me", root_dir=tmp_path, known_peers=["a", "b", "c"])
        assert set(t.peers()) == {"me", "a", "b", "c"}


# ─── send / recv FIFO + envelope shape ────────────────────────────


class TestSendRecv:
    def test_send_then_recv_roundtrip(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        sender.send("receiver", {"msg": 1})
        envelope = receiver.recv("receiver")
        assert envelope is not None
        assert envelope["from"] == "sender"
        assert envelope["to"] == "receiver"
        assert envelope["kind"] == "send"
        assert envelope["body"] == {"msg": 1}
        assert isinstance(envelope["seq"], int) and envelope["seq"] >= 1
        assert isinstance(envelope["uuid"], str) and len(envelope["uuid"]) > 0

    def test_recv_empty_returns_none(self, tmp_path: Path) -> None:
        _, receiver = _pair(tmp_path)
        assert receiver.recv("receiver") is None

    def test_recv_blocks_until_timeout_then_returns_none(self, tmp_path: Path) -> None:
        _, receiver = _pair(tmp_path)
        t0 = time.monotonic()
        assert receiver.recv("receiver", timeout_s=0.05) is None
        elapsed = time.monotonic() - t0
        assert 0.04 <= elapsed < 0.5

    def test_recv_picks_up_message_during_poll(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)

        def producer() -> None:
            time.sleep(0.02)
            sender.send("receiver", {"msg": "arrived"})

        threading.Thread(target=producer).start()
        envelope = receiver.recv("receiver", timeout_s=1.0)
        assert envelope is not None
        assert envelope["body"] == {"msg": "arrived"}

    def test_fifo_per_peer(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        for i in range(10):
            sender.send("receiver", {"i": i})
        received = []
        for _ in range(10):
            r = receiver.recv("receiver")
            assert r is not None
            received.append(r["body"]["i"])
        assert received == list(range(10))

    def test_send_moves_envelope_to_processed_after_recv(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        sender.send("receiver", {"msg": 1})
        assert receiver.pending_count("receiver") == 1
        receiver.recv("receiver")
        assert receiver.pending_count("receiver") == 0
        processed = list((tmp_path / "peers" / "receiver" / "processed").glob("*.json"))
        assert len(processed) == 1

    def test_send_envelope_is_independent_of_caller_payload(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        payload = {"box": [1, 2]}
        sender.send("receiver", payload)
        payload["box"].append(3)
        envelope = receiver.recv("receiver")
        assert envelope["body"] == {"box": [1, 2]}


# ─── Broadcast ────────────────────────────────────────────────────


class TestBroadcast:
    def test_broadcast_reaches_known_peers_skips_self(self, tmp_path: Path) -> None:
        # Three transports sharing the root: A broadcasts, B + C receive.
        a = FileQueueTransport(my_peer_id="a", root_dir=tmp_path, known_peers=["b", "c"])
        b = FileQueueTransport(my_peer_id="b", root_dir=tmp_path)
        c = FileQueueTransport(my_peer_id="c", root_dir=tmp_path)
        a.broadcast({"hello": "world"})
        env_b = b.recv("b")
        env_c = c.recv("c")
        assert env_b["body"] == {"hello": "world"}
        assert env_c["body"] == {"hello": "world"}
        # 'a' did not receive its own broadcast.
        assert a.pending_count("a") == 0


# ─── Idempotency-keyed dedup ──────────────────────────────────────


class TestIdempotencyDedup:
    def test_duplicate_idempotency_key_skipped_on_recv(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        # Manually craft two envelopes with the same key by hand-writing
        # files (simulates at-least-once redelivery).
        for i in range(2):
            envelope = {
                "from": "sender",
                "to": "receiver",
                "kind": "send",
                "body": {"i": i},
                "seq": i + 1,
                "uuid": f"u-{i}",
                "timestamp": "2026-05-19T00:00:00Z",
                "idempotency_key": "dup-1",
            }
            path = tmp_path / "peers" / "receiver" / "inbox" / f"{i + 1:020d}-u-{i}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(envelope), encoding="utf-8")

        first = receiver.recv("receiver")
        second = receiver.recv("receiver")
        assert first is not None
        assert first["body"] == {"i": 0}
        # The duplicate keyed envelope was popped + moved to processed but
        # silently absorbed; ``recv`` returns ``None`` (no more deliverable
        # envelopes).
        assert second is None
        # Both files ended up in processed/.
        processed = list((tmp_path / "peers" / "receiver" / "processed").glob("*.json"))
        assert len(processed) == 2

    def test_dedup_state_persists_across_reopen(self, tmp_path: Path) -> None:
        sender = FileQueueTransport(my_peer_id="sender", root_dir=tmp_path)
        receiver = FileQueueTransport(my_peer_id="receiver", root_dir=tmp_path)
        sender.send("receiver", {"i": 0})
        # Hand-promote the envelope to have a dedup key.
        inbox_files = sorted((tmp_path / "peers" / "receiver" / "inbox").glob("*.json"))
        assert len(inbox_files) == 1
        envelope = json.loads(inbox_files[0].read_text(encoding="utf-8"))
        envelope["idempotency_key"] = "persist-key"
        inbox_files[0].write_text(json.dumps(envelope), encoding="utf-8")
        envelope1 = receiver.recv("receiver")
        assert envelope1 is not None

        # Reopen the receiver; its dedup state should reload from disk.
        receiver2 = FileQueueTransport(my_peer_id="receiver", root_dir=tmp_path)
        replay_envelope = {
            **envelope,
            "uuid": "u-replay",
            "seq": 999,
        }
        replay_path = tmp_path / "peers" / "receiver" / "inbox" / "00000000000000000999-u-replay.json"
        replay_path.write_text(json.dumps(replay_envelope), encoding="utf-8")
        # The replayed message has the same idempotency_key → must be absorbed.
        assert receiver2.recv("receiver") is None
        del sender

    def test_dedup_window_evicts_oldest(self, tmp_path: Path) -> None:
        # Tiny window forces eviction.
        receiver = FileQueueTransport(
            my_peer_id="receiver",
            root_dir=tmp_path,
            config=FileQueueConfig(dedup_window=2, poll_interval_s=0.005),
        )
        # Inject 4 unique keys, then replay the first one — it should no
        # longer be deduped because the window only holds 2 entries.
        for i in range(4):
            envelope = {
                "from": "sender",
                "to": "receiver",
                "kind": "send",
                "body": {"i": i},
                "seq": i + 1,
                "uuid": f"u-{i}",
                "timestamp": "2026-05-19T00:00:00Z",
                "idempotency_key": f"key-{i}",
            }
            path = tmp_path / "peers" / "receiver" / "inbox" / f"{i + 1:020d}-u-{i}.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
        for _ in range(4):
            receiver.recv("receiver")

        replay = {
            "from": "sender",
            "to": "receiver",
            "kind": "send",
            "body": {"replay": True},
            "seq": 99,
            "uuid": "u-replay",
            "timestamp": "2026-05-19T00:00:00Z",
            "idempotency_key": "key-0",
        }
        replay_path = tmp_path / "peers" / "receiver" / "inbox" / "00000000000000000099-u-replay.json"
        replay_path.write_text(json.dumps(replay), encoding="utf-8")
        # key-0 evicted (window=2 holds key-2, key-3) → replay delivers.
        received = receiver.recv("receiver")
        assert received is not None
        assert received["body"] == {"replay": True}

    def test_missing_idempotency_key_never_deduped(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        for _ in range(3):
            sender.send("receiver", {"msg": "no_key"})
        for _ in range(3):
            assert receiver.recv("receiver") is not None


# ─── RPC (request/response) ───────────────────────────────────────


class TestRequestResponse:
    def test_request_completes_after_handler_runs(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)
        receiver.register_request_handler("receiver", lambda msg: {"echo": msg["op"]})

        def server_loop() -> None:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if receiver.process_request_inbox(max_messages=1) > 0:
                    return
                time.sleep(0.005)

        threading.Thread(target=server_loop).start()
        reply = sender.request("receiver", {"op": "ping"}, timeout_s=1.0)
        assert reply == {"echo": "ping"}

    def test_request_without_handler_returns_no_handler_error(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)

        def server_loop() -> None:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if receiver.process_request_inbox(max_messages=1) > 0:
                    return
                time.sleep(0.005)

        threading.Thread(target=server_loop).start()
        reply = sender.request("receiver", {"op": "ping"}, timeout_s=1.0)
        assert reply.get("error") == "no_handler"

    def test_request_times_out_when_no_server(self, tmp_path: Path) -> None:
        sender, _ = _pair(tmp_path)
        with pytest.raises(TransportTimeout):
            sender.request("receiver", {"op": "ping"}, timeout_s=0.05)

    def test_explicit_reply_method_delivers_response(self, tmp_path: Path) -> None:
        sender, receiver = _pair(tmp_path)

        def server_loop() -> None:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                env = receiver.recv("receiver", timeout_s=0.05)
                if env and env.get("kind") == "request":
                    receiver.reply(
                        env["from"],
                        env["correlation_id"],
                        {"reply": "ok", "for": env["body"]["op"]},
                    )
                    return

        threading.Thread(target=server_loop).start()
        reply = sender.request("receiver", {"op": "explicit"}, timeout_s=1.0)
        assert reply == {"reply": "ok", "for": "explicit"}


# ─── Atomic write properties ──────────────────────────────────────


class TestAtomicWrite:
    def test_no_partial_files_in_inbox(self, tmp_path: Path) -> None:
        sender, _ = _pair(tmp_path)
        sender.send("receiver", {"large": "x" * 10_000})
        inbox_files = sorted((tmp_path / "peers" / "receiver" / "inbox").glob("*.json"))
        # No leftover temp files (".tmp-...")
        tmp_files = sorted((tmp_path / "peers" / "receiver" / "inbox").glob(".tmp-*"))
        assert tmp_files == []
        assert len(inbox_files) == 1
        # File parses as JSON in one shot.
        payload = json.loads(inbox_files[0].read_text(encoding="utf-8"))
        assert payload["body"]["large"] == "x" * 10_000


# ─── Error surfaces ───────────────────────────────────────────────


class TestErrorSurfaces:
    def test_unparseable_inbox_file_raises_transport_error(self, tmp_path: Path) -> None:
        _, receiver = _pair(tmp_path)
        bad_path = tmp_path / "peers" / "receiver" / "inbox" / "00000000000000000001-u-bad.json"
        bad_path.write_text("{not: json}", encoding="utf-8")
        with pytest.raises(TransportError):
            receiver.recv("receiver")

    def test_send_to_unwritable_root_raises_network_error(self, tmp_path: Path) -> None:
        sender = FileQueueTransport(my_peer_id="sender", root_dir=tmp_path)
        # Replace the receiver's inbox with a non-directory file → write fails.
        receiver_inbox = tmp_path / "peers" / "receiver" / "inbox"
        receiver_inbox.parent.mkdir(parents=True, exist_ok=True)
        receiver_inbox.write_text("not a directory", encoding="utf-8")
        with pytest.raises(TransportNetworkError) as excinfo:
            sender.send("receiver", {"msg": 1})
        assert excinfo.value.peer_id == "receiver"
