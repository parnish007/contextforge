# RATIONALE: In-process simulation of all ContextForge MCP tools.
#            Mirrors the real server.py + index.ts tool contracts without
#            needing a running MCP server — lets benchmark scenarios run
#            as pure Python with no subprocess overhead.
"""
mcp_tool_client.py — In-process ContextForge MCP tool simulator.

All tools return the same JSON shapes as the real MCP server so that
coding_agent_sim.py can be ported to a real MCP client with minimal changes.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Lightweight in-memory data stores (mirrors SQLite schemas)
# ---------------------------------------------------------------------------

@dataclass
class DecisionNode:
    id: str
    project_id: str
    area: str
    summary: str
    rationale: str
    confidence: float
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    content_hash: str = ""
    created_by_agent: str = "coding_agent_sim"
    validated_by: str = ""
    tombstone: bool = False

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                (self.summary + self.rationale).encode()
            ).hexdigest()[:16]


@dataclass
class LedgerEvent:
    event_id: str
    event_type: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                json.dumps(self.payload, sort_keys=True).encode()
            ).hexdigest()[:16]


# ---------------------------------------------------------------------------
# ReviewerGuard — minimal in-process port of the real CF ReviewerGuard.
# Uses word-level entropy (paper mode) to flag adversarial writes.
# ---------------------------------------------------------------------------

def _word_entropy(text: str) -> float:
    """Compute Shannon word-level entropy of a text string."""
    import math
    words = re.findall(r"\w+", text.lower())
    if not words:
        return 0.0
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    n = len(words)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


_INJECTION_PATTERNS = [
    r"ignore\s+(previous|prior|all)\s+instructions?",
    r"disregard\s+.{0,30}(prompt|system|instruction)",
    r"you\s+are\s+now\s+(a|an)\s+",
    r"new\s+persona",
    r"act\s+as\s+.{0,20}(DAN|evil|unrestricted)",
    r"jailbreak",
    r"override\s+safety",
    r"rm\s+-rf\s+/",
    r"drop\s+table",
    r"exec\s*\(",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

_H_STAR_PAPER = 3.5      # word-level entropy threshold (paper mode)


def reviewer_guard(text: str) -> dict[str, Any]:
    """
    Returns {"pass": bool, "reason": str, "entropy": float, "pattern_hit": str|None}.
    Mirrors CF_MODE=paper behaviour (word-level H* = 3.5).
    """
    h = _word_entropy(text)
    for pat in _COMPILED:
        m = pat.search(text)
        if m:
            return {
                "pass": False,
                "reason": "regex_pattern_match",
                "entropy": round(h, 3),
                "pattern_hit": m.group(0),
            }
    if h < _H_STAR_PAPER:
        return {
            "pass": False,
            "reason": "low_entropy",
            "entropy": round(h, 3),
            "pattern_hit": None,
        }
    return {"pass": True, "reason": "ok", "entropy": round(h, 3), "pattern_hit": None}


# ---------------------------------------------------------------------------
# MCPToolClient — the tool façade
# ---------------------------------------------------------------------------

class MCPToolClient:
    """
    In-process simulation of all ContextForge MCP tools.

    State is held in memory; each MCPToolClient instance is an isolated
    session.  Call reset() between independent test scenarios.
    """

    def __init__(self, project_id: str = "sim-project") -> None:
        self.project_id = project_id
        self._nodes: dict[str, DecisionNode] = {}
        self._events: list[LedgerEvent] = []
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._projects: dict[str, dict[str, Any]] = {}
        self._call_log: list[dict[str, Any]] = []
        self._latencies_ms: list[float] = []

    # ------------------------------------------------------------------ #
    # Instrumentation helpers
    # ------------------------------------------------------------------ #

    def _record(self, tool: str, args: dict, result: dict, elapsed_ms: float) -> None:
        self._call_log.append({
            "tool": tool,
            "args": args,
            "result": result,
            "elapsed_ms": round(elapsed_ms, 2),
        })
        self._latencies_ms.append(elapsed_ms)

    def _append_event(self, event_type: str, payload: dict) -> str:
        eid = str(uuid.uuid4())
        self._events.append(LedgerEvent(
            event_id=eid,
            event_type=event_type,
            payload=payload,
        ))
        return eid

    # ------------------------------------------------------------------ #
    # Tool: init_project
    # ------------------------------------------------------------------ #

    def init_project(
        self,
        project_id: str,
        name: str,
        project_type: str = "code",
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        self.project_id = project_id
        self._projects[project_id] = {
            "project_id": project_id,
            "name": name,
            "type": project_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "node_count": 0,
        }
        self._append_event("PROJECT_INIT", {"project_id": project_id, "name": name})
        result = {"status": "ok", "project_id": project_id, "name": name, "type": project_type}
        self._record("init_project", {"project_id": project_id, "name": name}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: capture_decision
    # ------------------------------------------------------------------ #

    def capture_decision(
        self,
        summary: str,
        rationale: str,
        area: str = "architecture",
        confidence: float = 0.85,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()

        # ReviewerGuard check
        guard = reviewer_guard(summary + " " + rationale)
        if not guard["pass"]:
            result = {
                "status": "blocked",
                "reason": guard["reason"],
                "entropy": guard["entropy"],
                "pattern_hit": guard["pattern_hit"],
            }
            self._record("capture_decision", {"summary": summary[:60]}, result,
                         (time.perf_counter() - t0) * 1000)
            return result

        node = DecisionNode(
            id=str(uuid.uuid4()),
            project_id=self.project_id,
            area=area,
            summary=summary,
            rationale=rationale,
            confidence=confidence,
            tags=tags or [],
        )
        self._nodes[node.id] = node
        self._append_event("NODE_WRITE", {"node_id": node.id, "area": area, "summary": summary})
        result = {
            "status": "ok",
            "node_id": node.id,
            "content_hash": node.content_hash,
            "entropy": guard["entropy"],
        }
        self._record("capture_decision", {"summary": summary[:60], "area": area}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: get_knowledge_node
    # ------------------------------------------------------------------ #

    def get_knowledge_node(
        self,
        query: str = "",
        project_id: str | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        pid = project_id or self.project_id
        nodes = [n for n in self._nodes.values()
                 if n.project_id == pid and not n.tombstone and n.status == "active"]
        if query:
            q = query.lower()
            nodes = [n for n in nodes
                     if q in n.summary.lower() or q in n.rationale.lower() or q in n.area.lower()]
        nodes = sorted(nodes, key=lambda n: n.confidence, reverse=True)[:top_k]
        payload = [
            {
                "node_id": n.id,
                "area": n.area,
                "summary": n.summary,
                "rationale": n.rationale,
                "confidence": n.confidence,
                "tags": n.tags,
                "content_hash": n.content_hash,
                "created_at": n.created_at,
            }
            for n in nodes
        ]
        result = {"status": "ok", "count": len(payload), "nodes": payload}
        self._record("get_knowledge_node", {"query": query, "top_k": top_k}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: load_context
    # ------------------------------------------------------------------ #

    def load_context(
        self,
        project_id: str | None = None,
        query: str = "",
        detail_level: str = "L1",
        model_context_window: int | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        pid = project_id or self.project_id
        nodes = [n for n in self._nodes.values()
                 if n.project_id == pid and not n.tombstone and n.status == "active"]

        if model_context_window:
            budget = min(int(0.25 * model_context_window), 8000)
        else:
            budget = 1500

        lines: list[str] = []
        tokens_used = 0
        for n in sorted(nodes, key=lambda x: -x.confidence):
            if detail_level == "L0":
                line = f"[{n.area}] {n.summary}"
            elif detail_level == "L1":
                line = f"[{n.area}] {n.summary} | {n.rationale[:80]}"
            else:
                line = (f"[{n.area}] {n.summary} | {n.rationale} "
                        f"(conf={n.confidence:.2f}, hash={n.content_hash})")
            approx_tokens = len(line.split())
            if tokens_used + approx_tokens > budget:
                break
            lines.append(line)
            tokens_used += approx_tokens

        result = {
            "status": "ok",
            "project_id": pid,
            "detail_level": detail_level,
            "token_budget": budget,
            "tokens_used": tokens_used,
            "node_count": len(lines),
            "context": "\n".join(lines),
        }
        self._record("load_context", {"query": query, "detail_level": detail_level}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: search_context
    # ------------------------------------------------------------------ #

    def search_context(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.75,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        # BM25-style term overlap scoring
        q_terms = set(re.findall(r"\w+", query.lower()))
        scored: list[tuple[float, DecisionNode]] = []
        for n in self._nodes.values():
            if n.tombstone or n.status != "active":
                continue
            doc_terms = set(re.findall(r"\w+", (n.summary + " " + n.rationale).lower()))
            if not doc_terms:
                continue
            overlap = len(q_terms & doc_terms)
            score = overlap / (len(q_terms) + 0.5) if q_terms else 0.0
            if score >= threshold * 0.2:  # soften threshold for simulation (single-term overlap ≥ 1)
                scored.append((score, n))
        scored.sort(key=lambda x: -x[0])
        hits = [
            {
                "node_id": n.id,
                "score": round(s, 4),
                "area": n.area,
                "summary": n.summary,
                "tags": n.tags,
            }
            for s, n in scored[:top_k]
        ]
        result = {"status": "ok", "query": query, "hits": hits}
        self._record("search_context", {"query": query, "top_k": top_k}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: update_decision
    # ------------------------------------------------------------------ #

    def update_decision(
        self,
        node_id: str,
        summary: str | None = None,
        rationale: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        node = self._nodes.get(node_id)
        if not node or node.tombstone:
            result = {"status": "error", "reason": "node_not_found", "node_id": node_id}
            self._record("update_decision", {"node_id": node_id}, result,
                         (time.perf_counter() - t0) * 1000)
            return result

        if summary is not None:
            guard = reviewer_guard(summary + " " + (rationale or node.rationale))
            if not guard["pass"]:
                result = {
                    "status": "blocked",
                    "reason": guard["reason"],
                    "entropy": guard["entropy"],
                    "node_id": node_id,
                }
                self._record("update_decision", {"node_id": node_id}, result,
                             (time.perf_counter() - t0) * 1000)
                return result
            node.summary = summary

        if rationale is not None:
            node.rationale = rationale
        if confidence is not None:
            node.confidence = max(0.0, min(1.0, confidence))
        if status is not None:
            node.status = status

        node.updated_at = time.time()
        node.content_hash = hashlib.sha256(
            (node.summary + node.rationale).encode()
        ).hexdigest()[:16]
        self._append_event("NODE_UPDATE", {"node_id": node_id})

        result = {
            "status": "ok",
            "node_id": node_id,
            "content_hash": node.content_hash,
            "updated_fields": [k for k, v in
                               [("summary", summary), ("rationale", rationale),
                                ("confidence", confidence), ("status", status)]
                               if v is not None],
        }
        self._record("update_decision", {"node_id": node_id}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: delete_decision  (tombstone — soft delete)
    # ------------------------------------------------------------------ #

    def delete_decision(self, node_id: str, reason: str = "") -> dict[str, Any]:
        t0 = time.perf_counter()
        node = self._nodes.get(node_id)
        if not node:
            result = {"status": "error", "reason": "node_not_found"}
            self._record("delete_decision", {"node_id": node_id}, result,
                         (time.perf_counter() - t0) * 1000)
            return result
        node.tombstone = True
        node.status = "tombstoned"
        self._append_event("NODE_DELETE", {"node_id": node_id, "reason": reason})
        result = {"status": "ok", "node_id": node_id, "tombstoned": True}
        self._record("delete_decision", {"node_id": node_id}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: rollback
    # ------------------------------------------------------------------ #

    def rollback(
        self,
        event_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        if not event_id and not timestamp:
            return {"status": "error", "reason": "provide event_id or timestamp"}

        if event_id:
            idx = next((i for i, e in enumerate(self._events) if e.event_id == event_id), None)
            if idx is None:
                result = {"status": "error", "reason": "event_not_found", "event_id": event_id}
                self._record("rollback", {"event_id": event_id}, result,
                             (time.perf_counter() - t0) * 1000)
                return result
            pruned = len(self._events) - idx - 1
            self._events = self._events[: idx + 1]
        else:
            # timestamp-based prune
            ts = datetime.fromisoformat(timestamp).timestamp()  # type: ignore[arg-type]
            before = len(self._events)
            self._events = [e for e in self._events if e.timestamp <= ts]
            pruned = before - len(self._events)

        # Re-derive node states from surviving events (simplified: just re-tombstone)
        # Real implementation replays the full event log.
        result = {"status": "rolled_back", "pruned_events": pruned}
        self._record("rollback", {"event_id": event_id, "timestamp": timestamp}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: snapshot
    # ------------------------------------------------------------------ #

    def snapshot(self, label: str = "manual") -> dict[str, Any]:
        t0 = time.perf_counter()
        snap_id = f"snap_{label}_{int(time.time())}"
        self._snapshots[snap_id] = {
            "nodes": {nid: vars(n).copy() for nid, n in self._nodes.items()},
            "events": [vars(e).copy() for e in self._events],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
        }
        self._append_event("SNAPSHOT", {"snap_id": snap_id, "label": label})
        result = {
            "status": "ok",
            "snapshot_id": snap_id,
            "snapshot_path": f"snapshots/{snap_id}.forge",
            "node_count": len(self._nodes),
            "event_count": len(self._events),
        }
        self._record("snapshot", {"label": label}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: list_events
    # ------------------------------------------------------------------ #

    def list_events(
        self,
        last_n: int = 20,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        events = self._events
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        events = events[-last_n:]
        payload = [
            {
                "event_id": e.event_id,
                "type": e.event_type,
                "timestamp": e.timestamp,
                "content_hash": e.content_hash,
            }
            for e in events
        ]
        result = {"status": "ok", "count": len(payload), "events": payload}
        self._record("list_events", {"last_n": last_n, "type": event_type}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: replay_sync
    # ------------------------------------------------------------------ #

    def replay_sync(self, snap_id: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        snap = self._snapshots.get(snap_id)
        if not snap:
            result = {"status": "error", "reason": "snapshot_not_found", "snap_id": snap_id}
            self._record("replay_sync", {"snap_id": snap_id}, result,
                         (time.perf_counter() - t0) * 1000)
            return result
        # Restore nodes from snapshot
        self._nodes = {
            nid: DecisionNode(**{k: v for k, v in nd.items() if k != "content_hash"},
                              content_hash=nd["content_hash"])
            for nid, nd in snap["nodes"].items()
        }
        self._events = [LedgerEvent(**{k: v for k, v in e.items() if k != "content_hash"},
                                    content_hash=e["content_hash"])
                        for e in snap["events"]]
        result = {
            "status": "synced",
            "snap_id": snap_id,
            "replayed_events": len(self._events),
            "restored_nodes": len(self._nodes),
        }
        self._record("replay_sync", {"snap_id": snap_id}, result,
                     (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Tool: agent_status
    # ------------------------------------------------------------------ #

    def agent_status(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        result = {
            "status": "ok",
            "agents": {
                "Sentry":          {"online": True,  "events_processed": len(self._events)},
                "Librarian":       {"online": True,  "cache_entries": len(self._nodes)},
                "Ghost-Coder":     {"online": True,  "nodes_written": sum(
                                      1 for e in self._events if e.event_type == "NODE_WRITE")},
                "Shadow-Reviewer": {"online": True,  "blocks_issued": sum(
                                      1 for c in self._call_log if c.get("result", {}).get("status") == "blocked")},
                "Historian":       {"online": True,  "snapshots": len(self._snapshots)},
                "Token-Gater":     {"online": True,  "calls_routed": len(self._call_log)},
                "PM":              {"online": False, "note": "not wired in sim"},
                "Architect":       {"online": False, "note": "not wired in sim"},
            },
            "knowledge_graph": {
                "active_nodes": sum(1 for n in self._nodes.values() if not n.tombstone),
                "tombstoned_nodes": sum(1 for n in self._nodes.values() if n.tombstone),
                "total_events": len(self._events),
            },
        }
        self._record("agent_status", {}, result, (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Clear knowledge-graph state between scenarios; preserves call log for aggregate stats."""
        self._nodes.clear()
        self._events.clear()
        self._snapshots.clear()
        self._projects.clear()
        # _call_log and _latencies_ms are intentionally preserved so the runner
        # can collect cross-scenario tool coverage stats from a single client.

    def summary(self) -> dict[str, Any]:
        """Return a call summary for the current session."""
        tool_counts: dict[str, int] = {}
        tool_blocks: dict[str, int] = {}
        for c in self._call_log:
            tool_counts[c["tool"]] = tool_counts.get(c["tool"], 0) + 1
            if c.get("result", {}).get("status") == "blocked":
                tool_blocks[c["tool"]] = tool_blocks.get(c["tool"], 0) + 1
        avg_lat = sum(self._latencies_ms) / len(self._latencies_ms) if self._latencies_ms else 0.0
        return {
            "total_calls": len(self._call_log),
            "tool_counts": tool_counts,
            "blocked_calls": sum(tool_blocks.values()),
            "tool_blocks": tool_blocks,
            "avg_latency_ms": round(avg_lat, 3),
            "active_nodes": sum(1 for n in self._nodes.values() if not n.tombstone),
            "total_events": len(self._events),
            "snapshots": len(self._snapshots),
        }
