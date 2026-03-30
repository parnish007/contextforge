"""
ContextForge v3.0 — Researcher Agent (Phase 2)

Web-enabled agent that finds up-to-date documentation and API information,
then writes Knowledge Nodes (type=research) into the Librarian to prevent
Ghost-Coder from hallucinating outdated APIs.

Actions handled in reply() / metadata:
  research     — {"action": "research", "query": str}
  get_recent   — {"action": "get_recent", "limit": int}
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Callable

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

from src.skills.web_search import WebSearchSkill


# ---------------------------------------------------------------------------
# System prompt for result synthesis
# ---------------------------------------------------------------------------

_SYNTHESIS_PROMPT = """\
You are the Researcher for ContextForge.
Given web search results about a technical topic, synthesize the key findings.

Output ONLY a valid JSON object with these fields:
  summary     : str  — 2-3 sentences of key findings
  rationale   : str  — why this is relevant to the ContextForge project
  area        : str  — topic area (e.g. "rag", "agentscope", "llm-api")
  key_links   : list[str]  — top 2-3 source URLs
  confidence  : float  — 0.0-1.0 (how reliable the sources appear)

No markdown, no prose outside the JSON object.
"""


# ---------------------------------------------------------------------------
# ResearcherAgent
# ---------------------------------------------------------------------------

class ResearcherAgent(AgentBase):
    """
    Phase 2 — Researcher.

    Parameters
    ----------
    name : str
    model_fn : Callable[[list[dict]], str] | None
        LLM wrapper for synthesis.  None = extract-first-snippet fallback.
    search_skill : WebSearchSkill | None
        Pre-built search tool.  Created automatically if None.
    librarian : AgentBase | None
        Wired Librarian to write research nodes into.
    project_id : str | None
    """

    def __init__(
        self,
        name: str = "Researcher",
        model_fn: Callable[[list[dict]], str] | None = None,
        search_skill: WebSearchSkill | None = None,
        librarian=None,
        project_id: str | None = None,
    ):
        super().__init__()
        self.name = name
        self._model_fn = model_fn
        self._search = search_skill or WebSearchSkill(max_results=5)
        self._librarian = librarian
        self._project_id = project_id or os.getenv("PROJECT_ID", "default")

        logger.info(
            f"Researcher initialised — backend={self._search.backend}, "
            f"model={'llm' if model_fn else 'extract-fallback'}, "
            f"project={self._project_id}"
        )

    # ------------------------------------------------------------------
    # AgentBase interface
    # ------------------------------------------------------------------

    async def reply(self, x: Msg | None = None) -> Msg:
        if x is None:
            return self._noop()
        payload: dict = {}
        if x.metadata and isinstance(x.metadata, dict):
            payload = x.metadata
        elif isinstance(x.content, str):
            try:
                payload = json.loads(x.content)
            except Exception:
                return self._noop()

        action = payload.get("action", "")

        if action == "research":
            return await self._handle_research(payload)
        if action == "get_recent":
            return await self._handle_get_recent(payload)

        return self._noop()

    # ------------------------------------------------------------------
    # Public sync entry point (mirrors GhostCoder.process_batch pattern)
    # ------------------------------------------------------------------

    def research(self, query: str) -> dict:
        """Sync entry point for Director loop."""
        import asyncio
        msg = asyncio.run(
            self._handle_research({"action": "research", "query": query})
        )
        return msg.metadata or {}

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_research(self, payload: dict) -> Msg:
        query = payload.get("query", "").strip()
        if not query:
            return self._error("research requires 'query' field")

        logger.info(f"Researcher: searching — '{query[:60]}'")

        # Step 1: Web search
        results = self._search.search(query)
        if not results:
            logger.warning(f"Researcher: no results for '{query[:40]}'")
            return Msg(
                self.name,
                content=f"Researcher: no results found for '{query}'",
                role="assistant",
                metadata={"action": "research_empty", "query": query},
            )

        # Step 2: Synthesise into a knowledge node
        node = self._synthesise(query, results)

        # Step 3: Write to Librarian
        node_id: str | None = None
        if self._librarian is not None:
            write_msg = Msg(
                self.name,
                content=f"write_node: {node.get('id', '?')[:8]}",
                role="assistant",
                metadata={"action": "write_node", "node": node},
            )
            resp = await self._librarian.reply(write_msg)
            node_id = node.get("id")
            logger.info(
                f"Researcher: knowledge node {(node_id or '?')[:8]} → Librarian"
            )
        else:
            logger.warning("Researcher: no Librarian wired — node not persisted")

        return Msg(
            self.name,
            content=f"Researcher: synthesised '{query[:50]}' → node {(node_id or 'unpersisted')[:8]}",
            role="assistant",
            metadata={
                "action": "research_done",
                "query": query,
                "node": node,
                "raw_results": results,
            },
        )

    async def _handle_get_recent(self, payload: dict) -> Msg:
        """Return recent research nodes from Librarian cache (best-effort)."""
        limit = int(payload.get("limit", 2))
        # We ask the Librarian for recent research-area nodes if it's wired
        nodes: list[dict] = []
        if self._librarian is not None:
            try:
                query_msg = Msg(
                    self.name,
                    content="get_recent_research",
                    role="user",
                    metadata={"action": "get", "area": "research", "limit": limit},
                )
                resp = await self._librarian.reply(query_msg)
                if resp.metadata and isinstance(resp.metadata.get("nodes"), list):
                    nodes = resp.metadata["nodes"][:limit]
            except Exception as exc:
                logger.debug(f"Researcher: could not fetch recent nodes — {exc}")
        return Msg(
            self.name,
            content=f"Researcher: {len(nodes)} recent research node(s)",
            role="assistant",
            metadata={"action": "recent_research", "nodes": nodes},
        )

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _synthesise(self, query: str, results: list[dict]) -> dict:
        """Produce a structured knowledge node from search results."""
        if self._model_fn:
            try:
                snippet = self._build_synthesis_prompt(query, results)
                raw = self._model_fn([
                    {"role": "system", "content": _SYNTHESIS_PROMPT},
                    {"role": "user", "content": snippet},
                ])
                node_data = self._parse_synthesis(raw)
                if node_data:
                    return self._wrap_node(query, node_data, results)
            except Exception as exc:
                logger.warning(f"Researcher: LLM synthesis failed ({exc}) — fallback")

        return self._fallback_synthesis(query, results)

    def _build_synthesis_prompt(self, query: str, results: list[dict]) -> str:
        lines = [f"Query: {query}\n\nSearch results:"]
        for i, r in enumerate(results[:5], 1):
            lines.append(
                f"{i}. {r.get('title', 'No title')}\n"
                f"   URL: {r.get('url', '')}\n"
                f"   {r.get('snippet', '')[:200]}"
            )
        return "\n".join(lines)

    def _parse_synthesis(self, raw: str) -> dict | None:
        text = raw.strip()
        # strip markdown fences
        if "```" in text:
            start = text.find("{", text.find("```"))
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        else:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        try:
            return json.loads(text)
        except Exception:
            return None

    def _fallback_synthesis(self, query: str, results: list[dict]) -> dict:
        """Build a node from the first result snippet when no LLM is available."""
        top = results[0] if results else {}
        all_snippets = " ".join(r.get("snippet", "") for r in results[:3])
        return self._wrap_node(
            query,
            {
                "summary": all_snippets[:300] or f"Search results for: {query}",
                "rationale": f"Retrieved to provide up-to-date context for: {query}",
                "area": "research",
                "key_links": [r.get("url", "") for r in results[:3]],
                "confidence": 0.60,
            },
            results,
        )

    def _wrap_node(self, query: str, data: dict, results: list[dict]) -> dict:
        """Wrap synthesis output into a full decision_node dict."""
        now = datetime.utcnow().isoformat()
        return {
            "id": str(uuid.uuid4()),
            "project_id": self._project_id,
            "summary": data.get("summary", "")[:500],
            "rationale": data.get("rationale", ""),
            "area": data.get("area", "research"),
            "alternatives": [],
            "dependencies": [],
            "triggered_by": "researcher.web_search",
            "confidence": float(data.get("confidence", 0.70)),
            "importance": 0.7,
            "vclock": {},
            "origin_client": "researcher",
            "tombstone": False,
            "created_by_agent": self.name,
            "validated_by": "",
            "audited_by": "",
            "status": "active",
            "type_metadata": {
                "query": query,
                "key_links": data.get("key_links", [r.get("url") for r in results[:3]]),
                "result_count": len(results),
                "search_backend": self._search.backend,
            },
            "created_at": now,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})

    def _error(self, msg: str) -> Msg:
        logger.error(f"Researcher: {msg}")
        return Msg(self.name, content=msg, role="assistant", metadata={"action": "error", "detail": msg})
