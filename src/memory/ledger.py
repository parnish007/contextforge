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
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator

# Perplexity gate (optional third signal — imported lazily to keep startup fast)
_perplexity_gate = None

def _get_perplexity_gate():
    global _perplexity_gate
    if _perplexity_gate is None:
        try:
            from src.security.perplexity_gate import get_perplexity_gate
            cf_mode    = os.getenv("CF_MODE", "paper").lower()
            env_enable = os.getenv("ENABLE_PERPLEXITY_GATE", "false").lower() in ("1", "true", "yes")
            _perplexity_gate = get_perplexity_gate(enabled=cf_mode == "experiment" or env_enable)
        except Exception:
            pass
    return _perplexity_gate

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

    # ── PAPER / EXPERIMENT mode toggle ──────────────────────────────────
    # CF_MODE=paper      (default) — exact paper thresholds, backward-compatible
    # CF_MODE=experiment — multi-trigger OR-gate: entropy path OR intent path OR
    #                      perplexity path, each independently sufficient to block.
    #                      Replaces the broken soft-blend (v2) where W_E*flag +
    #                      W_K*score could never reach threshold when flag=0.
    _CF_MODE: str        = os.getenv("CF_MODE", "paper").lower()
    _ENTROPY_MODE: str   = os.getenv("CF_ENTROPY_MODE",
                               "char" if _CF_MODE == "experiment" else "word")
    # H* per entropy mode:
    #   word-level : paper H* = 3.5 bits  (µ_benign ≈ 2.74, internal-corpus calibration)
    #   char-level : experiment H* = 4.8 bits  (µ_benign ≈ 4.2 bits/char)
    _H_THRESHOLD: float  = float(os.getenv("CF_H_THRESHOLD",
                               "4.8" if _ENTROPY_MODE == "char" else "3.5"))
    _LZ_MIN_DENSITY: float = 0.60
    # Intent-path threshold for EXPERIMENT multi-trigger mode (v3).
    # An intent_score >= this value independently triggers a block,
    # regardless of entropy level.  0.70 chosen so that injection-pattern
    # matches (0.80-0.95) and high charter-overlap (>= 70% keyword hit rate)
    # both trigger independently without blending.
    _INTENT_THRESHOLD: float = float(os.getenv("CF_INTENT_THRESHOLD", "0.70"))

    # Strong adversarial-intent patterns — each maps to a confidence score [0,1].
    # Ordered from most-specific to broadest.  Evaluated on lowercased text_blob.
    # These cover: prompt injection, role-override, safety bypass, data exfiltration,
    # policy violation, and explicit jailbreak markers.
    _INJECTION_PATTERNS: tuple = (
        # ── Prompt injection / role override ──────────────────────────────
        (re.compile(r"ignore\s+(all\s+)?previous\s+(instructions?|context|rules?|guidelines?)", re.I), 0.96),
        (re.compile(r"disregard\s+(the\s+)?(above|previous|following|system|all)", re.I), 0.93),
        (re.compile(r"forget\s+(everything|all|above|previous|context|your\s+instructions?)", re.I), 0.92),
        (re.compile(r"\bjailbreak\b", re.I), 0.90),
        (re.compile(r"(dan\s+mode|developer\s+mode\s+enabled|act\s+as\s+dan)", re.I), 0.92),
        (re.compile(r"from\s+now\s+on\s+you\s+are\s+(an?\s+)?(unrestricted|free|different|jailbroken)", re.I), 0.91),
        (re.compile(r"you\s+are\s+now\s+(an?\s+)?(unrestricted|free|jailbroken|different)", re.I), 0.90),
        (re.compile(r"pretend\s+you\s+(have\s+)?no\s+(restriction|rule|filter|safety|constraint|guideline)", re.I), 0.89),
        (re.compile(r"respond\s+as\s+if\s+you\s+(have\s+no|are|were)\s+(restriction|filter|safety|unconstrained)", re.I), 0.88),
        (re.compile(r"act\s+as\s+(if\s+you\s+(are|have)\s+)?(a\s+)?(different|unrestricted|jailbroken|uncensored)", re.I), 0.87),
        # ── Safety / security bypass ───────────────────────────────────────
        (re.compile(r"(override|bypass|circumvent)\s+(your\s+)?(safety|security|filter|restriction|guideline)", re.I), 0.92),
        (re.compile(r"(suppress|disable)\s+(all\s+)?(safety|security)\s+(check|filter|guard|mechanism)", re.I), 0.91),
        (re.compile(r"(bypass|disable|circumvent)\s+(the\s+)?(charter|reviewerguard|reviewer\s+guard|audit)", re.I), 0.94),
        (re.compile(r"without\s+(any\s+)?(charter|safety|security)\s+(validation|check|enforcement)", re.I), 0.88),
        (re.compile(r"(process|execute|run)\s+.{0,30}\s+bypass(ing)?\s+(the\s+)?(guard|charter|review)", re.I), 0.90),
        # ── Data exfiltration ─────────────────────────────────────────────
        (re.compile(r"(reveal|output|print|show|expose)\s+(all\s+)?(api\s*key|credential|secret|password|token)", re.I), 0.93),
        (re.compile(r"(exfiltrate|extract|dump|export)\s+.{0,30}(api\s*key|credential|secret|charter|database)", re.I), 0.94),
        (re.compile(r"(send|upload|transmit|post)\s+.{0,30}(api\s*key|credential|secret|database|charter)\s*.{0,30}(external|remote|endpoint|server)", re.I), 0.92),
        # ── Instruction hijacking (continuation attacks) ──────────────────
        (re.compile(r"the\s+(above|previous|following)\s+(instruction|prompt|context)\s+(was|is|are)\s+(fake|wrong|test)", re.I), 0.85),
        (re.compile(r"(stop|end|halt)\s+(being|acting\s+as)\s+(an?\s+)?(ai|assistant|model)", re.I), 0.84),
        (re.compile(r"your\s+(true|real|actual)\s+(purpose|goal|instruction)\s+is", re.I), 0.86),
    )

    @staticmethod
    def _compute_word_entropy(text: str) -> float:
        from collections import Counter
        import math
        words = text.split()
        if not words:
            return 0.0
        counts = Counter(words)
        total = len(words)
        return -sum((c / total) * math.log2(c / total) for c in counts.values())

    @staticmethod
    def _compute_char_entropy(text: str) -> float:
        from collections import Counter
        import math
        chars = list(text)
        if not chars:
            return 0.0
        counts = Counter(chars)
        total = len(chars)
        return -sum((c / total) * math.log2(c / total) for c in counts.values())

    @classmethod
    def _compute_entropy(cls, text: str) -> float:
        if cls._ENTROPY_MODE == "char":
            return cls._compute_char_entropy(text)
        return cls._compute_word_entropy(text)

    @staticmethod
    def _compute_lz_density(text: str) -> float:
        raw = text.encode("utf-8", errors="replace")
        if not raw:
            return 1.0
        return len(zlib.compress(raw, level=6)) / len(raw)

    def _intent_score(self, text_blob: str) -> tuple[float, str]:
        """
        Compute adversarial intent score in [0, 1].

        Two independent sub-signals — returns the maximum:

        1. Injection pattern match (phrase-level, high precision):
           Regex patterns for jailbreak phrasing, role override, safety bypass,
           data exfiltration, and instruction hijacking.  Each pattern returns
           a pre-calibrated confidence in [0.84, 0.96].

        2. Charter keyword overlap (structural, normalized):
           Fraction of charter-constraint keywords that appear in the payload.
           High overlap (>= 70%) with a single constraint strongly suggests
           a direct charter violation attempt.

        Returns (score, trigger_label).
        """
        # Sub-signal 1: injection pattern match
        for pattern, score in self._INJECTION_PATTERNS:
            if pattern.search(text_blob):
                return score, f"injection_pattern({pattern.pattern[:40]})"

        # Sub-signal 2: charter keyword overlap (normalized)
        if not self._constraints:
            return 0.0, ""
        max_overlap = 0.0
        worst = ""
        for constraint in self._constraints:
            keywords = re.findall(r"\b\w{4,}\b", constraint.lower())
            if not keywords:
                continue
            hits  = sum(1 for kw in keywords if kw in text_blob)
            score = hits / len(keywords)
            if score > max_overlap:
                max_overlap = score
                worst = constraint
        return max_overlap, f"charter_overlap({max_overlap:.2f}: {worst[:60]})"

    def check(self, event_type: EventType, content: dict[str, Any]) -> None:
        """
        Raise ConflictError if content contradicts a charter constraint.
        Checks AGENT_THOUGHT, FILE_DIFF, and NODE_APPROVED events.

        PAPER MODE (CF_MODE=paper, default — exact paper behaviour)
        ────────────────────────────────────────────────────────────
          Pass 0   — Entropy (word-level H*=3.5) + LZ density: hard block
          Pass 0.5 — Perplexity (if ENABLE_PERPLEXITY_GATE=true): hard block
          Pass 1   — Destructive verb + protected entity: hard block
          Pass 2   — Charter keyword scoring (hits ≥ max(2, len//5)): hard block

        EXPERIMENT MODE (CF_MODE=experiment) — multi-trigger OR-gate v3
        ────────────────────────────────────────────────────────────────
          Pass 0:   LZ density < ρ_min → hard block (unchanged)
          Pass 0.5: Perplexity > P* → hard block (auto-enabled)
          Pass 1:   Destructive verb + protected entity → hard block (unchanged)
          Pass 2:   Multi-trigger OR-gate — any path independently sufficient:
            Path A (obfuscation): H_char ≥ H* = 4.8 → BLOCK
            Path B (intent):      intent_score ≥ 0.70 → BLOCK
              where intent_score = max(injection_pattern_score,
                                       charter_keyword_overlap_score)

        Architecture rationale: the v2 soft-blend
          (0.75 * entropy_flag + 0.25 * keyword_score ≥ 0.60)
        was a degenerate classifier — when entropy_flag=0 the keyword
        contribution (max 0.25) could never reach the 0.60 threshold,
        collapsing to entropy-only detection with 46% adversarial recall.
        Independent paths restore recall on natural-language injections
        (low entropy, high intent) without sacrificing benign FPR.
        """
        if event_type not in (EventType.AGENT_THOUGHT, EventType.FILE_DIFF,
                               EventType.NODE_APPROVED):
            return

        text_blob  = json.dumps(content, ensure_ascii=False).lower()
        experiment = (self._CF_MODE == "experiment")

        # ── Pass 0: LZ density gate (hard block in both modes) ──────────
        rho = self._compute_lz_density(text_blob)
        if rho < self._LZ_MIN_DENSITY:
            raise ConflictError(
                detail=f"LZ density ρ={rho:.3f} < {self._LZ_MIN_DENSITY} — repetition/compression attack",
                contradicted_rule="lz_density_gate",
            )

        # ── Pass 0: Entropy gate ─────────────────────────────────────────
        H = self._compute_entropy(text_blob)
        if H > self._H_THRESHOLD:
            # Both modes: entropy path is always an independent hard block.
            # PAPER: word H* = 3.5.  EXPERIMENT: char H* = 4.8.
            raise ConflictError(
                detail=f"Shannon entropy H={H:.2f} > {self._H_THRESHOLD} — high-entropy/obfuscated payload [entropy_path]",
                contradicted_rule="entropy_gate",
            )

        # ── Pass 0.5: Perplexity gate ────────────────────────────────────
        # Hard block in both modes; auto-enabled when CF_MODE=experiment.
        # Catches entropy-mimicry attacks whose H and ρ look benign but
        # whose word-sequence statistics are anomalous.
        try:
            gate = _get_perplexity_gate()
            if gate is not None and gate.enabled:
                p_result = gate.check(text_blob)
                if p_result.flagged:
                    raise ConflictError(
                        detail=(
                            f"Perplexity P={p_result.perplexity:.1f} > P*={p_result.threshold:.1f} "
                            f"— anomalous language pattern (entropy-mimicry indicator); "
                            f"backend={p_result.backend}  latency={p_result.latency_ms:.1f}ms"
                        ),
                        contradicted_rule="perplexity_gate",
                    )
        except ConflictError:
            raise
        except Exception:
            pass  # gate errors are non-fatal — degrade gracefully

        # ── Pass 1: Entity-centric fast path (hard block in both modes) ──
        if self._DESTRUCTIVE.search(text_blob):
            entities = self._CORE_PROTECTED | {
                e.lower() for e in getattr(self, "_protected_entities", [])
                if len(e) >= 4
            }
            for entity in entities:
                # Alphanumeric-only boundaries so compound identifiers like
                # "project_charter.md" still match the entity "charter"
                # (standard \b treats _ as a word char, causing misses).
                pat = r"(?<![a-zA-Z0-9])" + re.escape(entity) + r"(?![a-zA-Z0-9])"
                if re.search(pat, text_blob):
                    raise ConflictError(
                        detail=(
                            f"Destructive operation targeting protected entity "
                            f'"{entity}": content contains a blocked verb'
                        ),
                        contradicted_rule=f"protected entity: {entity}",
                    )

        # ── Pass 2: Charter / intent gate ────────────────────────────────
        if not self._constraints and not self._INJECTION_PATTERNS:
            return

        if not experiment:
            # PAPER MODE: original hard-block keyword scoring (unchanged for reproducibility)
            for constraint in self._constraints:
                keywords = re.findall(r"\b\w{4,}\b", constraint.lower())
                if not keywords:
                    continue
                hits = sum(1 for kw in keywords if kw in text_blob)
                if hits >= max(2, len(keywords) // 5):
                    raise ConflictError(
                        detail=(
                            f"Event content may contradict charter constraint: "
                            f'"{constraint[:120]}"'
                        ),
                        contradicted_rule=constraint,
                    )
        else:
            # EXPERIMENT MODE: multi-trigger OR-gate (v3).
            # Path B — intent path: injection patterns OR charter keyword overlap.
            # Each sub-signal is independently sufficient at intent_threshold=0.70.
            # This restores recall on natural-language adversarial payloads (low entropy,
            # high intent) that the v2 soft-blend (entropy_flag=0 → max score 0.25) missed.
            intent_score, intent_label = self._intent_score(text_blob)
            if intent_score >= self._INTENT_THRESHOLD:
                raise ConflictError(
                    detail=(
                        f"Intent score {intent_score:.3f} ≥ {self._INTENT_THRESHOLD} "
                        f"— adversarial intent detected via {intent_label}"
                    ),
                    contradicted_rule=intent_label,
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
    prev_hash  TEXT,
    project_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_created    ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_status     ON events (status);
CREATE INDEX IF NOT EXISTS idx_events_project_id ON events (project_id);
"""


# ---------------------------------------------------------------------------
# Internal helper — compute latest hash from an already-open connection
# ---------------------------------------------------------------------------

def _inline_latest_hash(conn: sqlite3.Connection) -> str:
    """
    Compute the current hash-chain tip using *conn* (no new connection opened).

    Mirrors ``EventLedger._latest_hash()`` exactly so that callers holding an
    open transaction (e.g. ``rollback()``) can obtain the tip hash without
    triggering a nested ``_conn()`` context manager, which would open a
    second SQLite connection and risk a torn hash chain.
    """
    row = conn.execute(
        "SELECT prev_hash, event_id FROM events ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        return hashlib.sha256(b"genesis").hexdigest()
    chain_input = f"{row['prev_hash'] or ''}{row['event_id']}"
    return hashlib.sha256(chain_input.encode()).hexdigest()


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
            # Safe migration — add project_id column if not already present
            try:
                conn.execute("ALTER TABLE events ADD COLUMN project_id TEXT")
                logger.debug("[Ledger] Migrated: added project_id to events table")
            except Exception:
                pass  # Column already exists

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
        project_id: str | None       = None,
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
                   (event_id, parent_id, event_type, content, metadata, status, prev_hash, created_at, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    parent_id,
                    event_type.value,
                    content_json,
                    json.dumps(meta, default=str),
                    status,
                    prev_hash,
                    _now_iso(),
                    project_id,
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
        event_id:   str | None = None,
        *,
        timestamp:  str | None = None,
        project_id: str | None = None,
    ) -> int:
        """
        Mark all events AFTER the target as 'rolled_back'.

        Accepts either:
          event_id  — UUID of the target event (that event stays active)
          timestamp — ISO 8601 string; all events after this time are pruned

        When project_id is provided, only events belonging to that project
        are rolled back — events from other projects are left untouched.

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
            if project_id is not None:
                cur = conn.execute(
                    "UPDATE events SET status = 'rolled_back' "
                    "WHERE rowid > ? AND status = 'active' AND event_type != ? "
                    "AND (project_id = ? OR project_id IS NULL)",
                    (anchor_rowid, EventType.ROLLBACK.value, project_id),
                )
            else:
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
                   (event_id, event_type, content, metadata, status, prev_hash, created_at, project_id)
                   VALUES (?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    rollback_id,
                    EventType.ROLLBACK.value,
                    json.dumps({"target_event_id": event_id, "target_timestamp": timestamp,
                                "pruned_count": pruned, "project_id": project_id}),
                    json.dumps({"auto": True}),
                    _inline_latest_hash(conn),
                    _now_iso(),
                    project_id,
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
        project_id: str | None  = None,
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
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)

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
