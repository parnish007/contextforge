"""
ContextForge Nexus Architecture — Local-Edge Speculative RAG Indexer
=============================================================

All semantic search runs on the user's local hardware — zero cloud tokens
consumed during retrieval.

Architecture
────────────
  1. Indexing  — crawl project files (.py .js .ts .md .yaml .json etc.)
                 split into chunks (≤ 512 tokens), embed with
                 all-MiniLM-L6-v2 (sentence-transformers), store in a
                 numpy in-memory matrix + on-disk cache (.forge/index.npz).

  2. Search    — encode query → cosine similarity against all chunks →
                 return chunks with score ≥ threshold.

  3. Differential Context Injection
                 Only chunks with cosine ≥ 0.75 (configurable) are
                 injected into the LLM context, keeping the prompt window
                 as small as possible.

  4. JIT Warmed Cache
                 The indexer pre-warms the relevant chunks as soon as
                 `search()` is called so subsequent calls are instant.

Fallback
────────
  If sentence-transformers is not installed, the indexer falls back to
  a pure-Python TF-IDF cosine similarity (no external deps), matching
  the approach used in the OMEGA-75 benchmark baseline.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    chunk_id:    str
    file_path:   str
    start_line:  int
    end_line:    int
    text:        str
    file_hash:   str      = ""
    embedding:   list[float] = field(default_factory=list, repr=False)


# ---------------------------------------------------------------------------
# TF-IDF fallback (no external deps)
# ---------------------------------------------------------------------------

class _TFIDFFallback:
    """Minimal TF-IDF cosine similarity — used when sentence-transformers absent."""

    def __init__(self) -> None:
        self._corpus:  list[str]       = []
        self._vectors: list[dict[str, float]] = []
        self._idf:     dict[str, float] = {}

    def fit(self, corpus: list[str]) -> None:
        self._corpus = corpus
        N = len(corpus)
        df: Counter[str] = Counter()
        tfs: list[Counter[str]] = []
        for doc in corpus:
            tokens = self._tokenize(doc)
            tf     = Counter(tokens)
            tfs.append(tf)
            df.update(set(tokens))

        self._idf = {
            term: math.log((N + 1) / (freq + 1)) + 1
            for term, freq in df.items()
        }

        self._vectors = []
        for tf in tfs:
            total  = sum(tf.values()) or 1
            vec    = {t: (c / total) * self._idf.get(t, 1.0) for t, c in tf.items()}
            self._vectors.append(vec)

    def query(self, text: str, top_k: int, threshold: float) -> list[tuple[int, float]]:
        q_tokens = self._tokenize(text)
        total    = len(q_tokens) or 1
        q_tf     = Counter(q_tokens)
        q_vec    = {t: (c / total) * self._idf.get(t, 1.0) for t, c in q_tf.items()}

        results: list[tuple[int, float]] = []
        for idx, doc_vec in enumerate(self._vectors):
            score = self._cosine(q_vec, doc_vec)
            if score >= threshold:
                results.append((idx, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z_][a-z0-9_]{2,}", text.lower())

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        dot   = sum(a.get(k, 0) * v for k, v in b.items())
        norm_a = math.sqrt(sum(v * v for v in a.values()))
        norm_b = math.sqrt(sum(v * v for v in b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# LocalIndexer
# ---------------------------------------------------------------------------

# Extensions to index
_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".yaml",
               ".yml", ".json", ".toml", ".txt", ".sh", ".env.example"}

# Max tokens per chunk (approximated as words)
_CHUNK_WORDS = 400

# On-disk index cache location
_INDEX_DIR = Path(".forge")


class LocalIndexer:
    """
    Local-edge semantic indexer over project files.

    Usage
    ─────
    indexer = LocalIndexer(project_root=".")
    indexer.build_index()                         # one-time (or on-change)
    results = indexer.search("JWT authentication")
    """

    def __init__(
        self,
        project_root: str  = ".",
        index_dir:    str  = ".forge",
        threshold:    float = 0.75,
        chunk_words:  int   = _CHUNK_WORDS,
    ) -> None:
        self._root       = Path(project_root).resolve()
        self._index_dir  = Path(index_dir)
        self._threshold  = threshold
        self._chunk_words = chunk_words

        self._chunks:      list[Chunk] = []
        self._warm_cache:  dict[str, list[dict[str, Any]]] = {}   # query → results

        # Try sentence-transformers; fall back to TF-IDF
        self._st_model = None
        self._tfidf    = _TFIDFFallback()
        self._use_st   = False
        self._try_load_st()

        # Auto-build if cache exists
        self._load_or_build()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_index(self, force: bool = False) -> int:
        """
        Crawl project files, chunk them, embed, save cache.
        Returns number of chunks indexed.
        """
        cache_path = self._index_dir / "index_meta.json"

        if not force and cache_path.exists():
            # Check if any file has changed since last index
            meta = json.loads(cache_path.read_text(encoding="utf-8"))
            if not self._files_changed(meta.get("file_hashes", {})):
                logger.debug("[LocalIndexer] Index cache is fresh — skipping rebuild")
                return len(self._chunks)

        logger.info("[LocalIndexer] Building local index …")
        t0 = time.monotonic()

        self._chunks = []
        file_hashes: dict[str, str] = {}

        for fpath in self._iter_files():
            try:
                text  = fpath.read_text(encoding="utf-8", errors="ignore")
                fhash = hashlib.sha256(text.encode()).hexdigest()[:16]
                file_hashes[str(fpath)] = fhash

                for chunk in self._split_file(fpath, text, fhash):
                    self._chunks.append(chunk)
            except Exception as exc:
                logger.debug(f"[LocalIndexer] Skip {fpath}: {exc}")

        # Embed / fit
        corpus = [c.text for c in self._chunks]
        if self._use_st and self._st_model:
            embeddings = self._st_model.encode(
                corpus, batch_size=64, show_progress_bar=False
            )
            for chunk, emb in zip(self._chunks, embeddings):
                chunk.embedding = emb.tolist()
        else:
            self._tfidf.fit(corpus)

        # Persist cache
        self._save_cache(file_hashes)
        elapsed = time.monotonic() - t0
        logger.info(
            f"[LocalIndexer] Indexed {len(self._chunks)} chunks "
            f"from {len(file_hashes)} files in {elapsed:.2f}s "
            f"(backend={'sentence-transformers' if self._use_st else 'tfidf'})"
        )
        return len(self._chunks)

    def search(
        self,
        query:     str,
        top_k:     int   = 5,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Semantic search. Returns chunks with cosine ≥ threshold.

        Each result:
          {file_path, start_line, end_line, text, score}
        """
        if threshold is None:
            threshold = self._threshold

        cache_key = f"{query}|{top_k}|{threshold:.2f}"
        if cache_key in self._warm_cache:
            return self._warm_cache[cache_key]

        if not self._chunks:
            return []

        if self._use_st and self._st_model:
            results = self._st_search(query, top_k, threshold)
        else:
            results = self._tfidf_search(query, top_k, threshold)

        self._warm_cache[cache_key] = results
        return results

    def invalidate_file(self, file_path: str) -> None:
        """Remove all chunks belonging to a file (called by Sentry on change)."""
        before = len(self._chunks)
        self._chunks = [c for c in self._chunks if c.file_path != file_path]
        self._warm_cache.clear()
        removed = before - len(self._chunks)
        if removed:
            logger.debug(f"[LocalIndexer] Invalidated {removed} chunks for {file_path}")
            # Rebuild TF-IDF on remaining corpus
            if not self._use_st:
                self._tfidf.fit([c.text for c in self._chunks])

    def stats(self) -> dict[str, Any]:
        return {
            "chunks":  len(self._chunks),
            "backend": "sentence-transformers" if self._use_st else "tfidf",
            "cache_hits": len(self._warm_cache),
        }

    # ------------------------------------------------------------------
    # Search backends
    # ------------------------------------------------------------------

    def _st_search(
        self, query: str, top_k: int, threshold: float
    ) -> list[dict[str, Any]]:
        import numpy as np  # type: ignore

        q_emb   = self._st_model.encode([query])[0]
        q_norm  = q_emb / (np.linalg.norm(q_emb) + 1e-10)

        scores  = []
        for idx, chunk in enumerate(self._chunks):
            if not chunk.embedding:
                continue
            c_emb  = np.array(chunk.embedding)
            c_norm = c_emb / (np.linalg.norm(c_emb) + 1e-10)
            score  = float(np.dot(q_norm, c_norm))
            if score >= threshold:
                scores.append((idx, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [self._format_result(self._chunks[i], s) for i, s in scores[:top_k]]

    def _tfidf_search(
        self, query: str, top_k: int, threshold: float
    ) -> list[dict[str, Any]]:
        hits = self._tfidf.query(query, top_k=top_k, threshold=threshold)
        return [self._format_result(self._chunks[i], s) for i, s in hits]

    # ------------------------------------------------------------------
    # File crawling & chunking
    # ------------------------------------------------------------------

    def _iter_files(self):
        skip_dirs = {".git", "__pycache__", ".venv", "node_modules",
                     ".forge", "data", "papers", "benchmark"}
        for fpath in self._root.rglob("*"):
            if fpath.is_dir():
                continue
            if any(part in skip_dirs for part in fpath.parts):
                continue
            if fpath.suffix.lower() in _EXTENSIONS:
                yield fpath

    def _split_file(self, fpath: Path, text: str, fhash: str) -> list[Chunk]:
        """Split a file into overlapping chunks of ≤ chunk_words words."""
        lines  = text.splitlines()
        chunks: list[Chunk] = []
        words_so_far: list[str] = []
        start_line = 0

        for lineno, line in enumerate(lines):
            words_so_far.extend(line.split())
            if len(words_so_far) >= self._chunk_words:
                chunk_text = " ".join(words_so_far)
                cid = hashlib.sha256(
                    f"{fpath}:{start_line}:{chunk_text[:64]}".encode()
                ).hexdigest()[:16]
                chunks.append(Chunk(
                    chunk_id   = cid,
                    file_path  = str(fpath.relative_to(self._root)),
                    start_line = start_line,
                    end_line   = lineno,
                    text       = chunk_text[:2000],  # cap at 2000 chars
                    file_hash  = fhash,
                ))
                # 50% overlap
                words_so_far = words_so_far[self._chunk_words // 2:]
                start_line   = lineno

        # Remainder
        if words_so_far:
            chunk_text = " ".join(words_so_far)
            cid = hashlib.sha256(
                f"{fpath}:{start_line}:tail:{chunk_text[:32]}".encode()
            ).hexdigest()[:16]
            chunks.append(Chunk(
                chunk_id   = cid,
                file_path  = str(fpath.relative_to(self._root)),
                start_line = start_line,
                end_line   = len(lines) - 1,
                text       = chunk_text[:2000],
                file_hash  = fhash,
            ))
        return chunks

    # ------------------------------------------------------------------
    # Cache persistence
    # ------------------------------------------------------------------

    def _save_cache(self, file_hashes: dict[str, str]) -> None:
        self._index_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "file_hashes": file_hashes,
            "chunk_count": len(self._chunks),
            "backend":     "st" if self._use_st else "tfidf",
            "built_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (self._index_dir / "index_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        if self._use_st:
            # Save embeddings as npz
            try:
                import numpy as np  # type: ignore
                embs    = [c.embedding for c in self._chunks if c.embedding]
                ids     = [c.chunk_id  for c in self._chunks if c.embedding]
                if embs:
                    np.savez_compressed(
                        str(self._index_dir / "embeddings.npz"),
                        embeddings=np.array(embs, dtype="float32"),
                        ids=np.array(ids),
                    )
            except Exception as exc:
                logger.debug(f"[LocalIndexer] Could not save npz cache: {exc}")

    def _load_or_build(self) -> None:
        meta_path = self._index_dir / "index_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not self._files_changed(meta.get("file_hashes", {})):
                    # TODO: reload chunks from cache (for now rebuild)
                    pass
            except Exception:
                pass
        # Always do a lightweight build on startup
        try:
            self.build_index()
        except Exception as exc:
            logger.warning(f"[LocalIndexer] Index build failed: {exc}")

    def _files_changed(self, cached_hashes: dict[str, str]) -> bool:
        for fpath in self._iter_files():
            try:
                text  = fpath.read_text(encoding="utf-8", errors="ignore")
                fhash = hashlib.sha256(text.encode()).hexdigest()[:16]
                if cached_hashes.get(str(fpath)) != fhash:
                    return True
            except Exception:
                return True
        return False

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_result(chunk: Chunk, score: float) -> dict[str, Any]:
        return {
            "file_path":  chunk.file_path,
            "file":       chunk.file_path,   # alias for backwards-compat with tests
            "start_line": chunk.start_line,
            "end_line":   chunk.end_line,
            "score":      round(score, 4),
            "text":       chunk.text[:800],
        }

    # ------------------------------------------------------------------
    # sentence-transformers loader
    # ------------------------------------------------------------------

    def _try_load_st(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            model_name = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
            self._st_model = SentenceTransformer(model_name)
            self._use_st   = True
            logger.info(f"[LocalIndexer] sentence-transformers loaded: {model_name}")
        except ImportError:
            logger.info(
                "[LocalIndexer] sentence-transformers not installed — "
                "using TF-IDF fallback (run: pip install sentence-transformers)"
            )
        except Exception as exc:
            logger.warning(f"[LocalIndexer] sentence-transformers failed: {exc} — using TF-IDF")
