# RATIONALE: Full OR-Set CRDT with vector clocks for cross-IDE synchronisation
# of decision_nodes. Replaces the server-authoritative LWW fallback described
# in the paper with a correct, convergent distributed data structure.
"""
OR-Set CRDT Synchronisation Engine
====================================

Implements an Observed-Remove Set (OR-Set) for ContextForge's decision_nodes
collection, with per-node vector clocks that track writes from each client
replica.

OR-Set semantics
────────────────
  An element E is in the set iff at least one add(E, tag) operation exists in
  the log for which no remove(E, tag) operation is also present.  This is the
  "add wins over remove" variant: concurrent add and remove on the same element
  from different replicas keeps the element.

  This matches ContextForge's safety requirement: if an IDE client adds a
  knowledge node while offline, that add should survive even if another client
  concurrently deleted a different version of the same node.

Vector clocks
─────────────
  Each replica maintains a vector clock { replica_id → counter }.  On every
  write, the replica increments its own counter.  Clocks are compared with the
  standard happened-before relation:

      A → B  iff  A[r] ≤ B[r] for all r, and A ≠ B

  Concurrent writes (neither A → B nor B → A) are handled according to the
  ConflictPolicy setting.

Conflict resolution policies
─────────────────────────────
  LWW      — last write wins (timestamp-based); backward-compatible default
  OR_SET   — OR-Set semantics; requires all participating replicas to run
             this module (opt-in, enabled via CRDT_SYNC_MODE=or_set)
  MANUAL   — concurrent writes are quarantined in the "conflicts" table for
             human review

Usage
─────
  from src.sync.crdt_sync import ORSetSync, ConflictPolicy

  sync_a = ORSetSync("client_a")
  sync_b = ORSetSync("client_b")

  sync_a.add("node_123", {"summary": "JWT auth", "confidence": 0.9})
  sync_b.add("node_123", {"summary": "JWT auth v2", "confidence": 0.95})

  # Merge B's state into A
  sync_a.merge(sync_b.export_state())
  # Both replicas converge to the same set

Snapshot format
───────────────
  create_snapshot_metadata() returns a dict that FluidSync embeds in the
  manifest.json of every .forge file, enabling causal replay ordering.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from loguru import logger


# ── Configuration ─────────────────────────────────────────────────────────────

CRDT_SYNC_MODE: str = os.getenv("CRDT_SYNC_MODE", "lww").lower()
_VALID_MODES = {"lww", "or_set", "manual"}
if CRDT_SYNC_MODE not in _VALID_MODES:
    raise ValueError(
        f"Invalid CRDT_SYNC_MODE={CRDT_SYNC_MODE!r}. Valid: {_VALID_MODES}"
    )


# ── Enums ─────────────────────────────────────────────────────────────────────

class ConflictPolicy(str, Enum):
    """
    How to resolve concurrent writes to the same node from different replicas.

    LWW      Last-write-wins (default, backward-compatible with v4.x).
    OR_SET   OR-Set CRDT semantics — concurrent add beats concurrent remove.
    MANUAL   Quarantine the conflict for human review via @historian gc.
    """
    LWW    = "lww"
    OR_SET = "or_set"
    MANUAL = "manual"


class ORSetOp(str, Enum):
    """Type of OR-Set operation recorded in the log."""
    ADD    = "add"
    REMOVE = "remove"


# ── Vector Clock ──────────────────────────────────────────────────────────────

@dataclass
class VectorClock:
    """
    Logical clock for a set of named replicas.

    Represented as { replica_id → counter } where counter is a non-negative
    integer incremented on each local write.
    """
    clock: dict[str, int] = field(default_factory=dict)

    def tick(self, replica_id: str) -> "VectorClock":
        """Increment this replica's counter and return a new clock."""
        new = VectorClock(clock=dict(self.clock))
        new.clock[replica_id] = new.clock.get(replica_id, 0) + 1
        return new

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Return a new clock that is the component-wise maximum."""
        all_ids = set(self.clock) | set(other.clock)
        return VectorClock(clock={
            r: max(self.clock.get(r, 0), other.clock.get(r, 0))
            for r in all_ids
        })

    def happened_before(self, other: "VectorClock") -> bool:
        """
        Return True iff self → other (self happened-before other).

        self → other  iff  self[r] ≤ other[r] for all r, and self ≠ other.
        """
        all_ids = set(self.clock) | set(other.clock)
        leq = all(
            self.clock.get(r, 0) <= other.clock.get(r, 0)
            for r in all_ids
        )
        return leq and self.clock != other.clock

    def concurrent_with(self, other: "VectorClock") -> bool:
        """Return True iff neither clock happened-before the other."""
        return (
            not self.happened_before(other)
            and not other.happened_before(self)
            and self.clock != other.clock
        )

    def to_dict(self) -> dict[str, int]:
        return dict(self.clock)

    @classmethod
    def from_dict(cls, d: dict[str, int]) -> "VectorClock":
        return cls(clock=dict(d))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VectorClock):
            return NotImplemented
        return self.clock == other.clock

    def __repr__(self) -> str:
        items = ", ".join(f"{k}:{v}" for k, v in sorted(self.clock.items()))
        return f"VC({{{items}}})"


# ── OR-Set entry types ────────────────────────────────────────────────────────

@dataclass
class ORSetEntry:
    """
    A single element in the OR-Set with its unique add-tag.

    The tag is a UUID generated at add time; it makes each add distinguishable
    from every other add, even if they add the same element.
    """
    element_id:   str           # node_id or content hash
    tag:          str           # unique UUID assigned at add time
    replica_id:   str           # which replica performed this add
    vector_clock: VectorClock
    timestamp:    float         # wall-clock (for LWW fallback)
    payload:      dict[str, Any]  # the node content
    removed_tags: set[str] = field(default_factory=set)  # tags explicitly removed

    def is_present(self) -> bool:
        """Element is in the set iff its tag has not been removed."""
        return self.tag not in self.removed_tags

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id":   self.element_id,
            "tag":          self.tag,
            "replica_id":   self.replica_id,
            "vector_clock": self.vector_clock.to_dict(),
            "timestamp":    self.timestamp,
            "payload":      self.payload,
            "removed_tags": list(self.removed_tags),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ORSetEntry":
        return cls(
            element_id   = d["element_id"],
            tag          = d["tag"],
            replica_id   = d["replica_id"],
            vector_clock = VectorClock.from_dict(d.get("vector_clock", {})),
            timestamp    = float(d.get("timestamp", 0.0)),
            payload      = d.get("payload", {}),
            removed_tags = set(d.get("removed_tags", [])),
        )


@dataclass
class ConflictRecord:
    """
    A quarantined concurrent write awaiting manual resolution (MANUAL policy).
    """
    conflict_id:  str
    element_id:   str
    local_entry:  ORSetEntry
    remote_entry: ORSetEntry
    detected_at:  float
    resolved:     bool = False
    resolution:   Optional[str] = None   # "keep_local" | "keep_remote" | "merge"

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id":  self.conflict_id,
            "element_id":   self.element_id,
            "local_entry":  self.local_entry.to_dict(),
            "remote_entry": self.remote_entry.to_dict(),
            "detected_at":  self.detected_at,
            "resolved":     self.resolved,
            "resolution":   self.resolution,
        }


# ── ORSetSync ─────────────────────────────────────────────────────────────────

class ORSetSync:
    """
    OR-Set CRDT for ContextForge decision_nodes.

    Each instance represents one replica (one IDE client).  Replicas
    exchange state via export_state() / merge(state).

    Parameters
    ----------
    replica_id      : Unique identifier for this IDE client (e.g. hostname + PID).
    policy          : Conflict resolution policy (default: from CRDT_SYNC_MODE env).
    """

    def __init__(
        self,
        replica_id: str,
        policy:     ConflictPolicy | None = None,
    ) -> None:
        self._replica_id = replica_id
        self._policy     = policy or ConflictPolicy(CRDT_SYNC_MODE)

        # element_id → list of ORSetEntry (one per add operation)
        self._entries:   dict[str, list[ORSetEntry]] = {}

        # Remove log: element_id → set of tags that have been removed
        self._removes:   dict[str, set[str]] = {}

        # Global vector clock for this replica
        self._clock:     VectorClock = VectorClock()

        # Quarantine queue (MANUAL policy)
        self._conflicts: list[ConflictRecord] = []

        logger.info(
            f"[ORSetSync] replica={replica_id!r}  policy={self._policy.value}"
        )

    # ── Core OR-Set operations ────────────────────────────────────────────────

    def add(
        self,
        element_id: str,
        payload:    dict[str, Any],
    ) -> str:
        """
        Add element_id to the set with the given payload.

        Returns the tag UUID assigned to this add operation.
        """
        self._clock = self._clock.tick(self._replica_id)
        tag = str(uuid.uuid4())

        entry = ORSetEntry(
            element_id   = element_id,
            tag          = tag,
            replica_id   = self._replica_id,
            vector_clock = self._clock,
            timestamp    = time.time(),
            payload      = payload,
        )
        self._entries.setdefault(element_id, []).append(entry)
        logger.debug(
            f"[ORSetSync] add  element={element_id[:12]}  tag={tag[:8]}  "
            f"clock={self._clock}"
        )
        return tag

    def remove(self, element_id: str) -> int:
        """
        Remove all current instances of element_id from the set.

        Records a remove for every add-tag currently in the local set.
        Returns the number of tags removed.
        """
        self._clock = self._clock.tick(self._replica_id)
        tags_removed = 0
        for entry in self._entries.get(element_id, []):
            if entry.tag not in entry.removed_tags:
                entry.removed_tags.add(entry.tag)
                self._removes.setdefault(element_id, set()).add(entry.tag)
                tags_removed += 1
        logger.debug(
            f"[ORSetSync] remove  element={element_id[:12]}  "
            f"tags_removed={tags_removed}"
        )
        return tags_removed

    def contains(self, element_id: str) -> bool:
        """Return True iff element_id has at least one un-removed add."""
        return any(e.is_present() for e in self._entries.get(element_id, []))

    def get(self, element_id: str) -> Optional[dict[str, Any]]:
        """
        Return the payload for element_id.

        For OR_SET and MANUAL policies: returns the payload of the entry with
        the highest vector clock (most recent causal write).

        For LWW: returns the highest-timestamp payload.
        """
        present = [
            e for e in self._entries.get(element_id, [])
            if e.is_present()
        ]
        if not present:
            return None
        if self._policy == ConflictPolicy.LWW:
            return max(present, key=lambda e: e.timestamp).payload
        # OR_SET / MANUAL: pick the entry whose clock is not dominated by any other
        dominant = self._find_dominant(present)
        return dominant.payload

    def elements(self) -> dict[str, dict[str, Any]]:
        """Return all currently present {element_id: payload} pairs."""
        result: dict[str, dict[str, Any]] = {}
        for eid in self._entries:
            p = self.get(eid)
            if p is not None:
                result[eid] = p
        return result

    # ── Merge ─────────────────────────────────────────────────────────────────

    def merge(self, remote_state: dict[str, Any]) -> dict[str, int]:
        """
        Merge a remote replica's exported state into this replica.

        Implements standard OR-Set merge:
          1. Union the add-sets (deduplicate by tag).
          2. Union the remove-sets.
          3. Propagate removes to local entries.
          4. Apply conflict resolution policy for concurrent writes.

        Returns a summary dict: {added, removed, conflicts}.
        """
        remote_entries_raw: list[dict] = remote_state.get("entries", [])
        remote_removes_raw: dict       = remote_state.get("removes", {})
        remote_clock       = VectorClock.from_dict(
            remote_state.get("clock", {})
        )

        stats = {"added": 0, "removed": 0, "conflicts": 0}

        # ── Step 1: Ingest remote adds ────────────────────────────────────
        local_tags: set[str] = {
            e.tag
            for entries in self._entries.values()
            for e in entries
        }
        for raw in remote_entries_raw:
            entry = ORSetEntry.from_dict(raw)
            if entry.tag in local_tags:
                continue  # already known

            # Check for concurrent write conflict
            local_list = self._entries.get(entry.element_id, [])
            concurrent_local = [
                le for le in local_list
                if le.is_present()
                and le.vector_clock.concurrent_with(entry.vector_clock)
            ]

            if concurrent_local and self._policy != ConflictPolicy.LWW:
                self._handle_conflict(
                    entry.element_id,
                    concurrent_local[0],
                    entry,
                    stats,
                )
                if self._policy == ConflictPolicy.MANUAL:
                    # Skip adding until resolved
                    continue

            self._entries.setdefault(entry.element_id, []).append(entry)
            stats["added"] += 1

        # ── Step 2: Propagate remote removes ──────────────────────────────
        for eid, tags in remote_removes_raw.items():
            if isinstance(tags, list):
                tags = set(tags)
            for entry in self._entries.get(eid, []):
                if entry.tag in tags and entry.tag not in entry.removed_tags:
                    entry.removed_tags.add(entry.tag)
                    self._removes.setdefault(eid, set()).add(entry.tag)
                    stats["removed"] += 1

        # ── Step 3: Advance local clock ───────────────────────────────────
        self._clock = self._clock.merge(remote_clock)

        logger.info(
            f"[ORSetSync] merge complete  "
            f"added={stats['added']}  removed={stats['removed']}  "
            f"conflicts={stats['conflicts']}  clock={self._clock}"
        )
        return stats

    # ── Export ────────────────────────────────────────────────────────────────

    def export_state(self) -> dict[str, Any]:
        """
        Export the full OR-Set state for transmission to other replicas.

        This is the payload exchanged during a sync handshake.
        """
        return {
            "replica_id": self._replica_id,
            "clock":      self._clock.to_dict(),
            "policy":     self._policy.value,
            "entries": [
                e.to_dict()
                for entries in self._entries.values()
                for e in entries
            ],
            "removes": {
                eid: list(tags)
                for eid, tags in self._removes.items()
            },
            "conflicts": [c.to_dict() for c in self._conflicts],
        }

    def create_snapshot_metadata(self) -> dict[str, Any]:
        """
        Return vector-clock metadata for embedding in a FluidSync snapshot.

        FluidSync writes this into manifest.json under the "crdt" key so that
        snapshot replays can reconstruct causal ordering correctly.
        """
        return {
            "crdt_version":    "1.0",
            "replica_id":      self._replica_id,
            "policy":          self._policy.value,
            "vector_clock":    self._clock.to_dict(),
            "element_count":   len(self._entries),
            "present_count":   sum(1 for eid in self._entries if self.contains(eid)),
            "remove_count":    sum(len(t) for t in self._removes.values()),
            "conflict_count":  len([c for c in self._conflicts if not c.resolved]),
        }

    # ── Conflict management ───────────────────────────────────────────────────

    def list_conflicts(self) -> list[ConflictRecord]:
        """Return unresolved conflicts (MANUAL policy)."""
        return [c for c in self._conflicts if not c.resolved]

    def resolve_conflict(
        self,
        conflict_id: str,
        resolution:  str,  # "keep_local" | "keep_remote" | "merge"
    ) -> bool:
        """
        Resolve a MANUAL conflict.

        "keep_local"  — discard remote entry
        "keep_remote" — discard local entry (apply remote remove)
        "merge"       — keep both payloads, merge into a new entry
        """
        for c in self._conflicts:
            if c.conflict_id != conflict_id or c.resolved:
                continue

            if resolution == "keep_local":
                c.remote_entry.removed_tags.add(c.remote_entry.tag)
                self._removes.setdefault(c.element_id, set()).add(c.remote_entry.tag)
            elif resolution == "keep_remote":
                c.local_entry.removed_tags.add(c.local_entry.tag)
                self._removes.setdefault(c.element_id, set()).add(c.local_entry.tag)
                # Add remote entry now
                self._entries.setdefault(c.element_id, []).append(c.remote_entry)
            elif resolution == "merge":
                merged_payload = {**c.local_entry.payload, **c.remote_entry.payload}
                self.add(c.element_id, merged_payload)
                # Remove both originals
                self.remove(c.element_id)

            c.resolved   = True
            c.resolution = resolution
            logger.info(
                f"[ORSetSync] conflict resolved  id={conflict_id[:8]}  "
                f"resolution={resolution}"
            )
            return True
        return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_dominant(self, entries: list[ORSetEntry]) -> ORSetEntry:
        """
        Return the entry whose vector clock is not dominated by any other.

        If multiple entries are concurrent (no dominance), return the one
        with the highest timestamp (LWW as tiebreak).
        """
        dominated: set[int] = set()
        for i, a in enumerate(entries):
            for j, b in enumerate(entries):
                if i == j:
                    continue
                if a.vector_clock.happened_before(b.vector_clock):
                    dominated.add(i)
        candidates = [e for i, e in enumerate(entries) if i not in dominated]
        return max(candidates, key=lambda e: e.timestamp)

    def _handle_conflict(
        self,
        element_id:   str,
        local_entry:  ORSetEntry,
        remote_entry: ORSetEntry,
        stats:        dict[str, int],
    ) -> None:
        """Apply conflict policy for a concurrent write."""
        stats["conflicts"] += 1

        if self._policy == ConflictPolicy.OR_SET:
            # OR-Set: add wins — keep both entries; get() will pick the dominant
            pass  # caller will add the remote entry after this returns

        elif self._policy == ConflictPolicy.MANUAL:
            # Quarantine — do NOT add the remote entry yet
            rec = ConflictRecord(
                conflict_id  = str(uuid.uuid4()),
                element_id   = element_id,
                local_entry  = local_entry,
                remote_entry = remote_entry,
                detected_at  = time.time(),
            )
            self._conflicts.append(rec)
            logger.warning(
                f"[ORSetSync] CONFLICT quarantined  element={element_id[:12]}  "
                f"conflict_id={rec.conflict_id[:8]}"
            )

    def __repr__(self) -> str:
        present = sum(1 for eid in self._entries if self.contains(eid))
        return (
            f"ORSetSync(replica={self._replica_id!r}, "
            f"policy={self._policy.value}, "
            f"present={present}/{len(self._entries)})"
        )
