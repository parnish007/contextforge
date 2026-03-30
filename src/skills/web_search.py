"""
ContextForge v3.0 — Web Search Skill

Tiered search backend for the Researcher agent:
  1. Tavily     — TAVILY_API_KEY   (best quality, structured results)
  2. Serper     — SERPER_API_KEY   (Google results via serper.dev)
  3. DuckDuckGo — no key needed    (always available, duckduckgo-search pkg)

Each backend returns a common list[dict]:
  {title: str, url: str, snippet: str}
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

SearchResult = dict  # {title: str, url: str, snippet: str}


# ---------------------------------------------------------------------------
# WebSearchSkill
# ---------------------------------------------------------------------------

class WebSearchSkill:
    """
    Thin wrapper around tiered web search backends.

    Parameters
    ----------
    max_results : int
        Maximum results to return per query (default 5).
    """

    def __init__(self, max_results: int = 5):
        self.max_results = max_results
        self._backend: str = self._detect_backend()
        logger.info(f"WebSearch: active backend = {self._backend}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, max_results: int | None = None) -> list[SearchResult]:
        """
        Execute a web search and return normalised results.

        Falls through the tier chain; returns empty list only if all fail.
        """
        n = max_results or self.max_results
        for attempt in [self._backend] + [
            b for b in ["tavily", "serper", "duckduckgo"] if b != self._backend
        ]:
            try:
                results = self._run(attempt, query, n)
                if results:
                    logger.info(
                        f"WebSearch: '{query[:40]}' → {len(results)} result(s) via {attempt}"
                    )
                    return results
            except Exception as exc:
                logger.warning(f"WebSearch: {attempt} failed — {exc}")
        logger.error(f"WebSearch: all backends failed for query '{query[:40]}'")
        return []

    @property
    def backend(self) -> str:
        return self._backend

    # ------------------------------------------------------------------
    # Backend detection
    # ------------------------------------------------------------------

    def _detect_backend(self) -> str:
        if os.getenv("TAVILY_API_KEY", "").strip():
            return "tavily"
        if os.getenv("SERPER_API_KEY", "").strip():
            return "serper"
        return "duckduckgo"

    # ------------------------------------------------------------------
    # Backend runners
    # ------------------------------------------------------------------

    def _run(self, backend: str, query: str, n: int) -> list[SearchResult]:
        if backend == "tavily":
            return self._tavily(query, n)
        if backend == "serper":
            return self._serper(query, n)
        return self._duckduckgo(query, n)

    def _tavily(self, query: str, n: int) -> list[SearchResult]:
        from tavily import TavilyClient  # type: ignore[import]
        client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        resp = client.search(query, max_results=n)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            for r in resp.get("results", [])
        ]

    def _serper(self, query: str, n: int) -> list[SearchResult]:
        import requests  # type: ignore[import]
        key = os.getenv("SERPER_API_KEY", "")
        r = requests.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": n},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
            for item in data.get("organic", [])[:n]
        ]

    def _duckduckgo(self, query: str, n: int) -> list[SearchResult]:
        from duckduckgo_search import DDGS  # type: ignore[import]
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=n))
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in raw
        ]
