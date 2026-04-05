"""
ContextForge Nexus Architecture — Event-Sourced Transactional Ledger
=============================================================

Replaces the static decision_nodes write pattern with an **append-only
event store**. Every turn, file change, agent thought, and conflict
becomes an immutable record.  The "current state" is always derived by
replaying the event log — enabling true time-travel rollbacks.

Schema
──────
  events table
    event_id    TEXT PRIMARY KEY          — UUID v4
    parent_id   TEXT                      — branch pointer (future CRDT)
    event_type  TEXT NOT NULL             — see EventType enum
    content     TEXT NOT NULL             — JSON payload
    metadata    TEXT                      — JSON: tokens_used, model_name, …
    status      TEXT DEFAULT 'active'     — active | rolled_back | conflict
    created_at  TIMESTAMP DEFAULT NOW
    prev_hash   TEXT                      — SHA-256 of previous event (hash chain)

Security / Socratic Reviewer integration
────────────────────────────────────────
  Before an event is committed as FINAL it is passed through
  `ReviewerGuard.check()`.  If the content contradicts the
  PROJECT_CHARTER.md, the event is marked `conflict` and the caller
  receives a `ConflictError` with the resolution prompt.

API
───
  ledger = EventLedger(db_path="data/contextforge.db")

  # Append
  eid = ledger.append(EventType.USER_INPUT, {"text": "..."})

  # Rollback to a specific event (prune everything after it)
  pruned = ledger.rollback(event_id=eid)

  # Reconstruct system prompt from last N events
  prompt = ledger.reconstruct_state(n=10)

  # Inspect
  events = ledger.list_events(last_n=20)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator

def _now_iso() -> str:
    """Microsecond-precision UTC timestamp (ISO 8601).

    SQLite's built-in strftime has only 1-second resolution, which causes
    same-second collisions in fast test suites.  Using Python's datetime
    gives us sub-millisecond ordering so timestamp-based rollbacks and
    ordering operations are always correct.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

from loguru import logger


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    USER_INPUT    = "USER_INPUT"
    AGENT_THOUGHT = "AGENT_THOUGHT"
    FILE_DIFF     = "FILE_DIFF"
    CHECKPOINT    = "CHECKPOINT"
    CONFLICT      = "CONFLICT"
    ROLLBACK      = "ROLLBACK"
    NODE_APPROVED = "NODE_APPROVED"
    NODE_BLOCKED  = "NODE_BLOCKED"
    RESEARCH      = "RESEARCH"
    TASK_DONE     = "TASK_DONE"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConflictError(Exception):
    """Raised when an event contradicts PROJECT_CHARTER.md."""
    def __init__(self, detail: str, contradicted_rule: str) -> None:
        super().__init__(detail)
        self.detail           = detail
        self.contradicted_rule = contradicted_rule


# ---------------------------------------------------------------------------
# Socratic Reviewer Guard
# ---------------------------------------------------------------------------

class ReviewerGuard:
    """
    Lightweight, offline charter-compliance checker.

    Reads PROJECT_CHARTER.md (if present) and extracts constraint lines
    (lines beginning with -, *, or numbered).  Before each event is persisted,
    it checks whether the event content introduces keywords that directly
    contradict any constraint.
    """

    def __init__(self, charter_path: str = "PROJECT_CHARTER.md") -> None:
        self._charter_path = Path(charter_path)
        self._constraints: list[str] = []
        self._load_charter()

    def _load_charter(self) -> None:
        if not self._charter_path.exists():
            logger.debug("[ReviewerGuard] No PROJECT_CHARTER.md found — guard inactive")
            return
        # errors="replace" ensures non-UTF-8 bytes in the file do not crash startup
        text = self._charter_path.read_text(encoding="utf-8", errors="replace")
        self._constraints = [
            line.strip(" -*•\t")
            for line in text.splitlines()
            if re.match(r"^\s*[-*•]|\s*\d+\.", line) and len(line.strip()) > 4
        ]
        # Also extract explicit protected-entity names from the charter.
        # These are extracted from "**<Name>**" bold patterns so we can
        # perform fast entity-presence checks independently of constraint scoring.
        self._protected_entities: list[str] = re.findall(
            r"\*\*([^*]+)\*\*", text
        )
        logger.debug(
            f"[ReviewerGuard] Loaded {len(self._constraints)} constraints, "
            f"{len(self._protected_entities)} protected entities"
        )

    def reload(self) -> None:
        """Hot-reload the charter (call after edits)."""
        self._constraints        = []
        self._protected_entities = []
        self._load_charter()

    # Compiled once at class level for performance.
    # Expanded vocabulary covers direct verbs AND indirect/obfuscated attack patterns:
    #   exfil / exfiltrate  — data-theft vectors
    #   decommission        — system deactivation synonym
    #   wipe / purge / flush / truncate / erase / clear — data destruction
    #   neutralize / terminate / suspend / revoke       — agent shutdown synonyms
    #   shutdown / shut down                            — system-level stop
    #   corrupt / sabotage / tamper                     — integrity attacks
    _DESTRUCTIVE: re.Pattern = re.compile(
        r"\b("
        r"delete|remove|drop|disable|kill|destroy|deprecate|eliminate|"
        r"uninstall|bypass|ignore|override|exfil|exfiltrate|decommission|"
        r"wipe|purge|flush|truncate|erase|neutralize|terminate|suspend|"
        r"revoke|shutdown|corrupt|sabotage|tamper|cleardb|reset|abandon|"
        r"circumvent|suppress|censor|obfuscate|mask|forge|spoof|inject"
        r")\b",
        re.IGNORECASE,
    )

    # Core protected entity names — always checked regardless of charter content.
    # Augmented by entities extracted from PROJECT_CHARTER.md at load time.
    _CORE_PROTECTED: frozenset[str] = frozenset({
        "sentry", "reviewer", "historian", "librarian", "coder", "router",
        "ledger", "charter", "database", "sqlite", "contextforge",
        "nexus", "sync", "fluidesync", "indexer", "knowledge", "graph",
        "pipeline", "watchdog", "watcher", "snapshot", "forge",
        "api", "key", "keys", "token", "tokens", "secret", "credential",
    })

    def check(self, event_type: EventType, content: dict[str, Any]) -> None:
        """
        Raise ConflictError if content contradicts a charter constraint.
        Checks AGENT_THOUGHT, FILE_DIFF, and NODE_APPROVED events.

        Two-pass strategy
        ─────────────────
        Pass 1 — Entity-centric fast path:
            If a destructive verb appears anywhere in the content AND any
            protected entity name also appears, raise immediately.  This
            catches bypasses like "exfil api keys" that don't align with
            long constraint strings.

        Pass 2 — Constraint-keyword scoring (original logic, relaxed threshold):
            For each charter constraint line, count keyword hits.  Raise if
            hits ≥ max(1, len(keywords) // 5)  [was // 3 — too strict].
        """
        if event_type not in (EventType.AGENT_THOUGHT, EventType.FILE_DIFF,
                               EventType.NODE_APPROVED):
            return

        text_blob = json.dumps(content, ensure_ascii=False).lower()

        # ── Pass 1: Entity-centric fast path ────────────────────────────
        if self._DESTRUCTIVE.search(text_blob):
            # Build entity set from hardcoded core + charter-extracted names
            entities = self._CORE_PROTECTED | {
                e.lower() for e in getattr(self, "_protected_entities", [])
                if len(e) >= 4
            }
            for entity in entities:
                # Use word-boundary search to avoid partial matches
                if re.search(r"\b" + re.escape(entity) + r"\b", text_blob):
                    raise ConflictError(
                        detail=(
                            f"Destructive operation targeting protected entity "
                            f'"{entity}": content contains a blocked verb'
                        ),
                        contradicted_rule=f"protected entity: {entity}",
                    )

        # ── Pass 2: Constraint-keyword scoring (relaxed threshold) ──────
        if not self._constraints:
            return

        for constraint in self._constraints:
            keywords = re.findall(r"\b\w{4,}\b", constraint.lower())
            if not keywords:
                continue
            hits = sum(1 for kw in keywords if kw in text_blob)
            # Relaxed from // 3 → // 5 so that 1/5 of keywords is sufficient
            if hits >= max(1, len(keywords) // 5):
                raise ConflictError(
                    detail=(
                        f"Event content may contradict charter constraint: "
                        f'"{constraint[:120]}"'
                    ),
                    contradicted_rule=constraint,
                )


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    parent_id  TEXT,
    event_type TEXT NOT NULL,
    content    TEXT NOT NULL,
    metadata   TEXT DEFAULT '{}',
    status     TEXT DEFAULT 'active'
                CHECK(status IN ('active', 'rolled_back', 'conflict')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    prev_hash  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_created  ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_events_type     ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_status   ON events (status);
"""


# ---------------------------------------------------------------------------
# EventLedger
# ---------------------------------------------------------------------------

class EventLedger:
    """
    Append-only SQLite event store with rollback and state reconstruction.
    """

    def __init__(
        self,
        db_path:      str  = "data/contextforge.db",
        charter_path: str  = "PROJECT_CHARTER.md",
    ) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._guard = ReviewerGuard(charter_path=charter_path)
        self._init_db()

    # ------------------------------------------------------------------
    # DB bootstrap
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory   = sqlite3.Row
        conn.text_factory  = lambda b: b.decode("utf-8", errors="replace")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")   # 8 MB page cache
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # Core: append
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: EventType,
        content:    dict[str, Any],
        *,
        parent_id:  str | None       = None,
        metadata:   dict[str, Any]  | None = None,
        skip_guard: bool             = False,
    ) -> str:
        """
        Append a new event to the ledger.

        Returns the new event_id (UUID4).
        Raises ConflictError if the Socratic Reviewer detects a charter violation
        (event is saved with status='conflict' before raising).
        """
        event_id  = str(uuid.uuid4())
        meta      = metadata or {}
        prev_hash = self._latest_hash()
        content_json = json.dumps(content, default=str)

        # Hash chain integrity
        chain_input = f"{prev_hash}{event_id}{content_json}"
        my_hash     = hashlib.sha256(chain_input.encode()).hexdigest()

        status = "active"
        conflict_error: ConflictError | None = None

        if not skip_guard:
            try:
                self._guard.check(event_type, content)
            except ConflictError as exc:
                status = "conflict"
                conflict_error = exc
                logger.warning(f"[Ledger] CONFLICT on {event_type.value}: {exc.detail}")

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO events
                   (event_id, parent_id, event_type, content, metadata, status, prev_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    parent_id,
                    event_type.value,
                    content_json,
                    json.dumps(meta, default=str),
                    status,
                    prev_hash,
                    _now_iso(),
                ),
            )

        logger.debug(f"[Ledger] +{event_type.value} id={event_id[:8]} status={status}")

        if conflict_error:
            raise conflict_error

        return event_id

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(
        self,
        event_id:  str | None = None,
        *,
        timestamp: str | None = None,
    ) -> int:
        """
        Mark all events AFTER the target as 'rolled_back'.

        Accepts either:
          event_id  — UUID of the target event (that event stays active)
          timestamp — ISO 8601 string; all events after this time are pruned

        Returns the number of events pruned.
        """
        if not event_id and not timestamp:
            raise ValueError("Provide event_id or timestamp")

        # Resolve target rowid — SQLite's implicit integer primary key
        # gives strict insertion order with sub-second precision, unlike
        # the TEXT created_at column which only has 1-second resolution.
        with self._conn() as conn:
            if event_id:
                row = conn.execute(
                    "SELECT rowid, created_at FROM events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if not row:
                    raise ValueError(f"event_id {event_id!r} not found")
                anchor_rowid  = row["rowid"]
                anchor_ts     = row["created_at"]
                cutoff        = anchor_ts   # kept for the ROLLBACK event metadata
            else:
                # Timestamp-based: find the highest rowid at or before the timestamp
                row = conn.execute(
                    "SELECT rowid FROM events WHERE created_at <= ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (timestamp,),
                ).fetchone()
                anchor_rowid = row["rowid"] if row else 0
                cutoff       = timestamp    # type: ignore[assignment]

            # Mark all events INSERTED AFTER the anchor as rolled_back.
            # Using rowid (strict insertion order) instead of created_at
            # prevents the same-second precision bug where multiple events
            # share an identical timestamp string.
            # ROLLBACK events themselves are excluded from being re-pruned
            # (prevents idempotency failure on repeated rollback calls).
            cur = conn.execute(
                "UPDATE events SET status = 'rolled_back' "
                "WHERE rowid > ? AND status = 'active' AND event_type != ?",
                (anchor_rowid, EventType.ROLLBACK.value),
            )
            pruned = cur.rowcount

            # Record the rollback itself as a ROLLBACK event (skip guard)
            rollback_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO events
                   (event_id, event_type, content, metadata, status, prev_hash, created_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (
                    rollback_id,
                    EventType.ROLLBACK.value,
                    json.dumps({"target_event_id": event_id, "target_timestamp": timestamp,
                                "pruned_count": pruned}),
                    json.dumps({"auto": True}),
                    self._latest_hash(),
                    _now_iso(),
                ),
            )

        logger.info(f"[Ledger] Rollback: pruned {pruned} events after {cutoff!r}")
        return pruned

    # ------------------------------------------------------------------
    # State reconstruction
    # ------------------------------------------------------------------

    def reconstruct_state(self, n: int = 20) -> str:
        """
        Build a system-prompt string from the last *n* active events.

        Format:
          [TIMESTAMP] EVENT_TYPE: <content summary>
        """
        events = self.list_events(last_n=n, status="active")
        lines: list[str] = ["=== ContextForge Ledger State ==="]
        for evt in events:
            ts      = evt.get("created_at", "")[:19]
            etype   = evt.get("event_type", "")
            content = evt.get("content", {})

            # Compact summary per event type
            if etype == EventType.USER_INPUT.value:
                summary = content.get("text", "")[:200]
            elif etype == EventType.AGENT_THOUGHT.value:
                summary = content.get("thought", content.get("summary", ""))[:200]
            elif etype == EventType.FILE_DIFF.value:
                summary = f"{content.get('path', '')} — {content.get('change_type', '')}"
            elif etype == EventType.NODE_APPROVED.value:
                summary = content.get("summary", "")[:200]
            elif etype == EventType.CONFLICT.value:
                summary = f"CONFLICT: {content.get('detail', '')}"
            else:
                summary = str(content)[:200]

            lines.append(f"[{ts}] {etype}: {summary}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Inspect
    # ------------------------------------------------------------------

    def list_events(
        self,
        last_n:     int         = 20,
        event_type: str | None  = None,
        status:     str | None  = None,
    ) -> list[dict[str, Any]]:
        """Return up to *last_n* events, newest first."""
        query  = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(last_n)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            try:
                d["content"]  = json.loads(d["content"])
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(d)
        return result

    def export_log(self) -> list[dict[str, Any]]:
        """Export entire active event log for Fluid-Sync transmission."""
        return self.list_events(last_n=100_000, status="active")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _latest_hash(self) -> str:
        """SHA-256 of the most recent event_id (hash-chain anchor)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT prev_hash, event_id FROM events "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return hashlib.sha256(b"genesis").hexdigest()
        chain_input = f"{row['prev_hash'] or ''}{row['event_id']}"
        return hashlib.sha256(chain_input.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Test-isolation helper
# ---------------------------------------------------------------------------

@contextmanager
def temp_ledger(charter_path: str = "PROJECT_CHARTER.md") -> Generator["EventLedger", None, None]:
    """
    Context manager that yields a fully-initialised ``EventLedger`` backed by
    a temporary SQLite file that is **deleted on exit**.

    Prevents 'Disk Full' errors in large benchmark suites by ensuring every
    test starts with a clean slate and releases disk immediately on teardown.

    Usage::

        with temp_ledger() as ledger:
            eid = ledger.append(EventType.USER_INPUT, {"text": "hello"})
            assert eid
        # temp file automatically removed
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="nexus_test_")
    os.close(fd)
    try:
        ledger = EventLedger(db_path=tmp_path, charter_path=charter_path)
        yield ledger
    finally:
        # Close all connections by letting the ledger go out of scope,
        # then unlink the file.  On Windows the file may be locked briefly;
        # suppress OSError rather than crashing the test teardown.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        # Also remove the WAL and SHM sidecar files if they exist
        for ext in ("-wal", "-shm"):
            try:
                os.remove(tmp_path + ext)
            except OSError:
                pass
