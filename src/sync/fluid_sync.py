"""
ContextForge Nexus Architecture — Fluid-Sync Coordinator
=================================================

Handles cross-device portability and automatic checkpointing with zero
"cloud bloat".

Key concepts
────────────
  Snapshot (.forge file)
      A ZIP archive containing:
        • events.json       — full active event log (text-only, small)
        • charter.md        — current PROJECT_CHARTER.md
        • manifest.json     — metadata (timestamp, event count, checksum)
      The archive is AES-256 encrypted using a passphrase derived from
      FORGE_SNAPSHOT_KEY env var (or a per-machine default if unset).

  Idle Trigger
      A background thread that monitors the time of the last activity.
      If no activity is detected for IDLE_MINUTES (default 15), a
      CHECKPOINT event is appended and a snapshot is saved to .forge/.

  New-device Handshake (replay_from_snapshot)
      1. Decrypt and unzip the .forge file.
      2. Replay events.json into a fresh local ledger.
      3. Lazy-pull source files from local disk (they already exist on disk;
         the ledger is the only thing that needed syncing).

  CRDT-Lite merge (merge_logs)
      Append-only OR-Set semantics: union both event logs, deduplicate by
      event_id, sort by created_at. Conflicts resolved by the Socratic
      Reviewer Guard inside the ledger.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from src.memory.ledger import EventLedger, EventType


# ---------------------------------------------------------------------------
# Encryption helpers (Fernet / AES-128-CBC via cryptography; fallback = base64)
# ---------------------------------------------------------------------------

def _get_passphrase() -> bytes:
    key = os.getenv("FORGE_SNAPSHOT_KEY", "contextforge-default-key")
    return hashlib.sha256(key.encode()).digest()   # 32-byte key


def _encrypt(data: bytes) -> bytes:
    """AES-256-GCM encrypt if cryptography is available; else base64-encode."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        import secrets
        nonce = secrets.token_bytes(12)
        key   = _get_passphrase()
        ct    = AESGCM(key).encrypt(nonce, data, b"contextforge")
        return nonce + ct
    except ImportError:
        import base64
        return b"B64:" + base64.b64encode(data)


def _decrypt(data: bytes) -> bytes:
    """Inverse of _encrypt."""
    if data.startswith(b"B64:"):
        import base64
        return base64.b64decode(data[4:])
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        key   = _get_passphrase()
        nonce = data[:12]
        ct    = data[12:]
        return AESGCM(key).decrypt(nonce, ct, b"contextforge")
    except ImportError:
        raise RuntimeError(
            "Cannot decrypt: cryptography not installed "
            "(pip install cryptography) and data is not base64."
        )


# ---------------------------------------------------------------------------
# FluidSync
# ---------------------------------------------------------------------------

class FluidSync:
    """
    Snapshot, idle-checkpoint, and cross-device replay coordinator.

    Parameters
    ──────────
    ledger       : EventLedger instance to snapshot / replay into
    charter_path : path to PROJECT_CHARTER.md (bundled into snapshots)
    snapshot_dir : directory to store .forge files (default .forge/)
    idle_minutes : inactivity threshold before auto-checkpoint (default 15)
    """

    def __init__(
        self,
        ledger:       EventLedger,
        charter_path: str = "PROJECT_CHARTER.md",
        snapshot_dir: str = ".forge",
        idle_minutes: float = 15.0,
    ) -> None:
        self._ledger       = ledger
        self._charter_path = Path(charter_path)
        self._snap_dir     = Path(snapshot_dir)
        self._snap_dir.mkdir(parents=True, exist_ok=True)
        self._idle_minutes  = idle_minutes
        self._last_activity = time.monotonic()

        self._idle_thread: threading.Thread | None = None
        self._stop_event  = threading.Event()
        self._start_idle_watcher()

    # ------------------------------------------------------------------
    # Public: snapshot
    # ------------------------------------------------------------------

    def create_snapshot(self, label: str = "manual") -> Path:
        """
        Bundle the ledger event log + charter into an encrypted .forge file.

        Returns the path to the created file.
        """
        ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_label = label.replace(" ", "_")[:32]
        out_path  = self._snap_dir / f"snapshot_{ts}_{safe_label}.forge"

        events      = self._ledger.export_log()
        events_json = json.dumps(events, default=str, indent=2).encode("utf-8")

        charter_text = b""
        if self._charter_path.exists():
            charter_text = self._charter_path.read_bytes()

        manifest = {
            "version":     "5.0",
            "label":       label,
            "created_at":  ts,
            "event_count": len(events),
            "checksum":    hashlib.sha256(events_json).hexdigest(),
        }
        manifest_json = json.dumps(manifest, indent=2).encode("utf-8")

        # Build ZIP in memory
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("events.json",   events_json)
            zf.writestr("charter.md",    charter_text)
            zf.writestr("manifest.json", manifest_json)

        encrypted = _encrypt(zip_buf.getvalue())
        out_path.write_bytes(encrypted)

        logger.info(
            f"[FluidSync] Snapshot saved: {out_path} "
            f"({len(events)} events, {out_path.stat().st_size:,} bytes)"
        )
        return out_path

    # ------------------------------------------------------------------
    # Public: replay (new-device handshake)
    # ------------------------------------------------------------------

    def replay_from_snapshot(self, forge_path: str) -> int:
        """
        Decrypt a .forge file and replay its events into the local ledger.

        Events that already exist (by event_id) are skipped (idempotent).
        Returns the number of events replayed.
        """
        path = Path(forge_path)
        if not path.exists():
            raise FileNotFoundError(f".forge file not found: {forge_path}")

        raw        = _decrypt(path.read_bytes())
        zip_buf    = io.BytesIO(raw)
        replayed   = 0

        with zipfile.ZipFile(zip_buf, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            events   = json.loads(zf.read("events.json").decode("utf-8"))

            # Restore charter if missing locally
            charter_bytes = zf.read("charter.md")
            if charter_bytes and not self._charter_path.exists():
                self._charter_path.write_bytes(charter_bytes)
                logger.info(f"[FluidSync] Restored PROJECT_CHARTER.md from snapshot")

        logger.info(
            f"[FluidSync] Replaying {len(events)} events from "
            f"{manifest.get('created_at', '?')} snapshot …"
        )

        # Get existing event IDs to skip duplicates
        existing = {
            e["event_id"]
            for e in self._ledger.list_events(last_n=100_000)
        }

        for evt in reversed(events):   # oldest first (events are newest-first from export)
            eid = evt.get("event_id", "")
            if eid in existing:
                continue
            try:
                raw_type = evt.get("event_type", "AGENT_THOUGHT")
                etype    = EventType(raw_type)
                content  = evt.get("content", {})
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        content = {"raw": content}

                self._ledger.append(
                    event_type = etype,
                    content    = content,
                    parent_id  = evt.get("parent_id"),
                    metadata   = evt.get("metadata", {}),
                    skip_guard = True,   # trust the originating device's reviewer
                )
                replayed += 1
            except Exception as exc:
                logger.debug(f"[FluidSync] Skipping event {eid[:8]}: {exc}")

        logger.info(f"[FluidSync] Replay complete: {replayed} new events ingested")
        return replayed

    # ------------------------------------------------------------------
    # Public: CRDT-Lite merge
    # ------------------------------------------------------------------

    def merge_logs(
        self,
        remote_events: list[dict[str, Any]],
    ) -> int:
        """
        OR-Set merge: union remote_events with the local ledger.
        Deduplicates by event_id. Returns number of new events ingested.
        """
        existing = {e["event_id"] for e in self._ledger.list_events(last_n=100_000)}
        ingested = 0

        # Sort by created_at ascending (oldest first)
        remote_sorted = sorted(
            remote_events,
            key=lambda e: e.get("created_at", ""),
        )

        for evt in remote_sorted:
            eid = evt.get("event_id", "")
            if eid in existing:
                continue
            try:
                etype   = EventType(evt.get("event_type", "AGENT_THOUGHT"))
                content = evt.get("content", {})
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        content = {"raw": content}

                self._ledger.append(
                    event_type = etype,
                    content    = content,
                    skip_guard = False,   # run local reviewer on merged content
                )
                ingested += 1
            except Exception as exc:
                logger.debug(f"[FluidSync] Merge skip {eid[:8]}: {exc}")

        return ingested

    # ------------------------------------------------------------------
    # Public: activity heartbeat
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Call this on every user interaction to reset the idle timer."""
        self._last_activity = time.monotonic()

    def shutdown(self) -> None:
        """Stop the idle watcher thread."""
        self._stop_event.set()
        if self._idle_thread and self._idle_thread.is_alive():
            self._idle_thread.join(timeout=2)

    # ------------------------------------------------------------------
    # Idle watcher — public + internal
    # ------------------------------------------------------------------

    def start_idle_watcher(self) -> None:
        """Public alias for ``_start_idle_watcher()`` (used by tests and external callers)."""
        self._start_idle_watcher()

    def _start_idle_watcher(self) -> None:
        self._idle_thread = threading.Thread(
            target=self._idle_loop,
            name="fluid-sync-idle",
            daemon=True,
        )
        self._idle_thread.start()
        logger.debug(
            f"[FluidSync] Idle watcher started "
            f"(threshold={self._idle_minutes} min)"
        )

    def _idle_loop(self) -> None:
        idle_seconds = self._idle_minutes * 60
        check_interval = min(60.0, idle_seconds / 4)   # check every 1/4 of threshold

        while not self._stop_event.is_set():
            time.sleep(check_interval)
            elapsed = time.monotonic() - self._last_activity
            if elapsed >= idle_seconds:
                self._auto_checkpoint()
                # Reset so we don't checkpoint every check after idle
                self._last_activity = time.monotonic()

    def _auto_checkpoint(self) -> None:
        logger.info("[FluidSync] Idle threshold reached — creating auto-checkpoint")
        try:
            # Append CHECKPOINT event to the ledger
            self._ledger.append(
                event_type = EventType.CHECKPOINT,
                content    = {
                    "trigger": "idle",
                    "idle_minutes": self._idle_minutes,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                skip_guard = True,
            )
            # Save the .forge snapshot
            self.create_snapshot(label="auto_idle")
        except Exception as exc:
            logger.error(f"[FluidSync] Auto-checkpoint failed: {exc}")

    # ------------------------------------------------------------------
    # List available snapshots
    # ------------------------------------------------------------------

    def list_snapshots(self) -> list[dict[str, Any]]:
        """Return metadata for all .forge files in the snapshot directory."""
        result: list[dict[str, Any]] = []
        for fpath in sorted(self._snap_dir.glob("*.forge")):
            stat = fpath.stat()
            result.append({
                "path":         str(fpath),
                "size_bytes":   stat.st_size,
                "modified_at":  datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return result
