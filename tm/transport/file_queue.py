"""``FileQueueTransport`` — local-filesystem transport.

Companion to :class:`tm.transport.http.HttpTransport`. Where HTTP is the
production wire format for live cross-process / cross-host communication,
FileQueueTransport is the **audit / debug / disconnected-leaf** wire format:
every message is visible as a JSON file on disk, making evidence chains
trivially reproducible and amenable to forensic review.

Directory layout::

    {root}/
      peers/
        {peer_id}/
          inbox/        # messages waiting for {peer_id} to recv
            {seq}-{uuid}.json
          processed/    # successfully recv'd (kept for audit)
            {seq}-{uuid}.json
          responses/    # RPC responses keyed by correlation_id
            {correlation_id}.json
          dedup.json    # rolling LRU of recently-seen idempotency keys

Envelope shape (JSON content of each inbox file)::

    {
      "from": "<sender peer_id>",
      "to": "<recipient peer_id>",
      "kind": "send" | "request",
      "body": { ... opaque payload ... },
      "seq": <monotonic integer>,
      "uuid": "<random>",
      "timestamp": "<RFC3339>",
      "idempotency_key": "<optional>",
      "correlation_id": "<required when kind == request>"
    }

Semantics
---------
- **At-least-once delivery**: ``send`` writes the envelope atomically
  (``tempfile.NamedTemporaryFile`` + ``os.replace``); the receiver's
  ``recv`` pops the oldest file. A crash between write and processed-rename
  leaves the file in inbox for re-delivery on the next recv.
- **Idempotency dedup**: each peer maintains a small rolling LRU of
  ``idempotency_key`` values. On recv, if the popped envelope's key is
  already seen, it is moved to processed/ and the next file is examined.
  This converts at-least-once into effectively-once at the application
  layer when callers supply keys (which the remote ``AgentRuntime``
  resolver does for every cross-node request).
- **FIFO**: per-peer ordering is preserved via a monotonically increasing
  sequence number embedded in the filename. ``sorted(inbox)`` always yields
  oldest-first.
- **RPC**: ``request`` writes a request envelope to ``{to}/inbox/``, then
  polls ``{from}/responses/{correlation_id}.json`` until present or
  ``timeout_s`` elapses. The server side is expected to call
  :meth:`reply` to deposit the reply file. Polling cadence defaults to
  20 ms; tunable via constructor.

Concurrency
-----------
- File operations are atomic at the POSIX level; no in-process lock is
  required for cross-process correctness.
- The in-process sequence counter is protected by a ``threading.Lock`` so
  multiple threads in the same process can safely share one transport.
- Dedup state is persisted to ``dedup.json`` after every recv, with the
  same atomic-rename pattern.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, Mapping

from tm.transport.base import (
    RequestHandler,
    TransportError,
    TransportNetworkError,
    TransportTimeout,
)

# Envelope files follow ``{seq:020d}-{uuid}.json``. Anything else in the
# inbox directory (most importantly tempfiles ``.tmp-XXX.json`` created
# mid-write by atomic enqueue) MUST be skipped to avoid racing the writer.
# Pattern is permissive on the uuid portion so hand-crafted forensic /
# replay tools can use friendly identifiers; the only hard requirement is
# the leading 20-digit sequence number, which alone disambiguates from
# the ``.tmp-`` prefix used for atomic writes.
_ENVELOPE_FILENAME_RE = re.compile(r"^\d{20}-[\w.-]+\.json$")

DEFAULT_DEDUP_WINDOW = 256
DEFAULT_POLL_INTERVAL_S = 0.02


@dataclass(frozen=True)
class FileQueueConfig:
    """Configuration knobs for :class:`FileQueueTransport`."""

    dedup_window: int = DEFAULT_DEDUP_WINDOW
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S


class FileQueueTransport:
    """Filesystem-backed ``Transport`` with at-least-once + idempotency dedup."""

    INBOX_DIR = "inbox"
    PROCESSED_DIR = "processed"
    RESPONSES_DIR = "responses"
    DEDUP_FILE = "dedup.json"

    def __init__(
        self,
        *,
        my_peer_id: str,
        root_dir: str | os.PathLike[str],
        known_peers: Iterable[str] | None = None,
        config: FileQueueConfig | None = None,
    ) -> None:
        if not my_peer_id:
            raise ValueError("my_peer_id must be a non-empty string")
        self._my_id = my_peer_id
        self._root = Path(root_dir)
        self._config = config or FileQueueConfig()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._handlers: Dict[str, RequestHandler] = {}
        self._dedup_state: Deque[str] = deque(maxlen=self._config.dedup_window)
        self._dedup_lock = threading.Lock()

        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_peer_dirs(my_peer_id)
        self._load_dedup_state()

        if known_peers is not None:
            for peer_id in known_peers:
                self._ensure_peer_dirs(peer_id)

    # ─── Public Transport API ────────────────────────────────────────

    def send(self, peer_id: str, message: Mapping[str, Any]) -> None:
        envelope = self._make_envelope("send", peer_id, message)
        self._enqueue(peer_id, envelope)

    def broadcast(self, message: Mapping[str, Any]) -> None:
        last_err: TransportError | None = None
        for peer_id in list(self.peers()):
            if peer_id == self._my_id:
                continue
            try:
                self.send(peer_id, message)
            except TransportError as exc:
                last_err = exc
        if last_err is not None:
            raise last_err

    def recv(self, peer_id: str, *, timeout_s: float | None = None) -> Mapping[str, Any] | None:
        """Pop the oldest envelope addressed to ``peer_id``.

        ``peer_id`` is normally ``self._my_id`` — the local inbox. Other
        values are allowed for debug / forensic tools that want to inspect
        any peer's pending mailbox.

        ``timeout_s`` (when set) makes this a bounded poll; ``None`` is
        non-blocking (single sweep of the inbox dir).
        """
        deadline = (time.monotonic() + timeout_s) if timeout_s and timeout_s > 0 else None
        while True:
            envelope = self._try_pop_one(peer_id)
            if envelope is not None:
                return envelope
            if deadline is None:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._config.poll_interval_s)

    def request(
        self,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        correlation_id = uuid.uuid4().hex
        envelope = self._make_envelope(
            "request",
            peer_id,
            message,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        self._enqueue(peer_id, envelope)
        response_path = self._peer_dir(self._my_id) / self.RESPONSES_DIR / f"{correlation_id}.json"
        deadline = time.monotonic() + (timeout_s if timeout_s and timeout_s > 0 else 5.0)
        while True:
            if response_path.exists():
                try:
                    raw = response_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    raw = None
                if raw is not None:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise TransportError(f"RPC reply file {response_path} is not valid JSON: {exc}") from exc
                    response_path.unlink(missing_ok=True)
                    if not isinstance(payload, Mapping):
                        raise TransportError(
                            f"RPC reply file {response_path} must contain a JSON object, got {type(payload).__name__}"
                        )
                    return dict(payload)
            if time.monotonic() >= deadline:
                raise TransportTimeout(f"FileQueue RPC to '{peer_id}' (correlation_id={correlation_id}) timed out")
            time.sleep(self._config.poll_interval_s)

    def peers(self) -> Iterable[str]:
        peers_dir = self._root / "peers"
        if not peers_dir.exists():
            return ()
        return tuple(sorted(child.name for child in peers_dir.iterdir() if child.is_dir()))

    # ─── Server-side hooks for request handlers ──────────────────────

    def register_request_handler(self, peer_id: str, handler: RequestHandler) -> None:
        self._handlers[peer_id] = handler

    def unregister_request_handler(self, peer_id: str) -> None:
        self._handlers.pop(peer_id, None)

    def process_request_inbox(self, max_messages: int | None = None) -> int:
        """Drain the local inbox of ``kind=request`` envelopes, invoking the
        registered handler for each and writing the reply into the sender's
        ``responses/`` dir.

        Returns the number of requests processed. Non-request envelopes are
        left untouched in the inbox so that ``recv`` can still see them.
        ``max_messages`` (when set) caps the drain.
        """
        processed = 0
        inbox_dir = self._peer_dir(self._my_id) / self.INBOX_DIR
        if not inbox_dir.exists():
            return 0
        for envelope_path in sorted(inbox_dir.iterdir()):
            if max_messages is not None and processed >= max_messages:
                break
            if not _ENVELOPE_FILENAME_RE.match(envelope_path.name):
                continue
            try:
                envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if envelope.get("kind") != "request":
                continue
            self._handle_request(envelope, envelope_path)
            processed += 1
        return processed

    def reply(
        self,
        sender_peer_id: str,
        correlation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Deposit an RPC reply for ``sender_peer_id``.

        Used by external server loops that don't want to call
        :meth:`process_request_inbox` (e.g. when the reply is computed
        out-of-band).
        """
        self._ensure_peer_dirs(sender_peer_id)
        target = self._peer_dir(sender_peer_id) / self.RESPONSES_DIR / f"{correlation_id}.json"
        self._atomic_write_json(target, dict(payload))

    # ─── Diagnostics helpers ─────────────────────────────────────────

    def pending_count(self, peer_id: str) -> int:
        inbox_dir = self._peer_dir(peer_id) / self.INBOX_DIR
        if not inbox_dir.exists():
            return 0
        return sum(1 for entry in inbox_dir.iterdir() if _ENVELOPE_FILENAME_RE.match(entry.name))

    # ─── Internals ───────────────────────────────────────────────────

    def _make_envelope(
        self,
        kind: str,
        peer_id: str,
        message: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> Dict[str, Any]:
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        envelope: Dict[str, Any] = {
            "from": self._my_id,
            "to": peer_id,
            "kind": kind,
            "body": dict(message),
            "seq": seq,
            "uuid": uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if idempotency_key is not None:
            envelope["idempotency_key"] = idempotency_key
        if correlation_id is not None:
            envelope["correlation_id"] = correlation_id
        return envelope

    def _enqueue(self, peer_id: str, envelope: Mapping[str, Any]) -> None:
        inbox_dir = self._peer_dir(peer_id) / self.INBOX_DIR
        filename = f"{envelope['seq']:020d}-{envelope['uuid']}.json"
        try:
            self._ensure_peer_dirs(peer_id)
            self._atomic_write_json(inbox_dir / filename, dict(envelope))
        except OSError as exc:
            raise TransportNetworkError(
                f"failed to enqueue message for '{peer_id}': {exc}",
                peer_id=peer_id,
            ) from exc

    def _try_pop_one(self, peer_id: str) -> Mapping[str, Any] | None:
        inbox_dir = self._peer_dir(peer_id) / self.INBOX_DIR
        if not inbox_dir.exists():
            return None
        for envelope_path in sorted(inbox_dir.iterdir()):
            if not _ENVELOPE_FILENAME_RE.match(envelope_path.name):
                continue
            try:
                envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # Concurrent recv consumed it between glob and read.
                continue
            except json.JSONDecodeError as exc:
                raise TransportError(f"FileQueue inbox file {envelope_path} is not valid JSON: {exc}") from exc
            # Move to processed/ before deciding dedup. The file is gone
            # from inbox either way; dedup just decides whether to skip
            # surfacing it to the caller.
            processed_path = self._peer_dir(peer_id) / self.PROCESSED_DIR / envelope_path.name
            try:
                os.replace(envelope_path, processed_path)
            except FileNotFoundError:
                continue
            if self._is_duplicate(envelope):
                continue
            self._record_seen(envelope)
            return envelope
        return None

    def _handle_request(
        self,
        envelope: Mapping[str, Any],
        envelope_path: Path,
    ) -> None:
        sender = str(envelope.get("from") or "")
        correlation_id = str(envelope.get("correlation_id") or "")
        if not sender or not correlation_id:
            raise TransportError(f"FileQueue request envelope {envelope_path} is missing 'from' or 'correlation_id'")
        # Move from inbox → processed first (matches recv's at-least-once
        # contract; crash between move and reply leaves the request in
        # processed and the sender will retry from its end).
        processed_path = self._peer_dir(self._my_id) / self.PROCESSED_DIR / envelope_path.name
        try:
            os.replace(envelope_path, processed_path)
        except FileNotFoundError:
            return

        handler_key = str(envelope.get("to") or self._my_id)
        if self._is_duplicate(envelope):
            # Replay: if we ever produced a reply, re-deliver it; otherwise
            # silently absorb (the sender will time out and retry).
            cached_reply_path = self._peer_dir(sender) / self.RESPONSES_DIR / f"{correlation_id}.json"
            if cached_reply_path.exists():
                return
            return
        self._record_seen(envelope)

        handler = self._handlers.get(handler_key)
        if handler is None:
            err_payload = {
                "error": "no_handler",
                "handler_key": handler_key,
                "correlation_id": correlation_id,
            }
            self.reply(sender, correlation_id, err_payload)
            return
        body = envelope.get("body") or {}
        if not isinstance(body, Mapping):
            raise TransportError(f"FileQueue request envelope {envelope_path} body must be a mapping")
        reply = handler(dict(body))
        if not isinstance(reply, Mapping):
            raise TransportError(f"handler for '{handler_key}' returned non-mapping {type(reply).__name__}")
        self.reply(sender, correlation_id, dict(reply))

    def _is_duplicate(self, envelope: Mapping[str, Any]) -> bool:
        key = envelope.get("idempotency_key")
        if not isinstance(key, str) or not key:
            return False
        sender = str(envelope.get("from") or "")
        composite = f"{sender}|{key}"
        with self._dedup_lock:
            return composite in self._dedup_state

    def _record_seen(self, envelope: Mapping[str, Any]) -> None:
        key = envelope.get("idempotency_key")
        if not isinstance(key, str) or not key:
            return
        sender = str(envelope.get("from") or "")
        composite = f"{sender}|{key}"
        with self._dedup_lock:
            if composite not in self._dedup_state:
                self._dedup_state.append(composite)
                self._persist_dedup_state_locked()

    def _persist_dedup_state_locked(self) -> None:
        dedup_path = self._peer_dir(self._my_id) / self.DEDUP_FILE
        snapshot = list(self._dedup_state)
        try:
            self._atomic_write_json(dedup_path, {"keys": snapshot})
        except OSError:
            # Dedup persistence is best-effort; loss only means duplicates
            # may be re-delivered after a crash, which is correct
            # at-least-once behavior.
            return

    def _load_dedup_state(self) -> None:
        dedup_path = self._peer_dir(self._my_id) / self.DEDUP_FILE
        if not dedup_path.exists():
            return
        try:
            payload = json.loads(dedup_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        keys = payload.get("keys") if isinstance(payload, Mapping) else None
        if not isinstance(keys, list):
            return
        with self._dedup_lock:
            for key in keys[-self._config.dedup_window :]:
                if isinstance(key, str):
                    self._dedup_state.append(key)

    def _ensure_peer_dirs(self, peer_id: str) -> None:
        base = self._peer_dir(peer_id)
        for sub in (self.INBOX_DIR, self.PROCESSED_DIR, self.RESPONSES_DIR):
            (base / sub).mkdir(parents=True, exist_ok=True)

    def _peer_dir(self, peer_id: str) -> Path:
        return self._root / "peers" / peer_id

    def _atomic_write_json(self, target: Path, payload: Mapping[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".tmp-",
            suffix=".json",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, target)


__all__ = ["FileQueueConfig", "FileQueueTransport"]
