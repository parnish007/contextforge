# ContextForge: An Information-Theoretic Agentic Memory System

<p align="center">
  <strong>Persistent memory · Dual-signal adversarial defense · Zero cloud retrieval cost</strong>
</p>

<p align="center">
  <img src="docs/assets/radar_comparison.png" width="520" alt="Six-Pillar Safety Profile — Stateless RAG vs ContextForge Nexus"/>
</p>

> **Author:** Trilochan Sharma — Independent Researcher  
> **Architecture:** The Nexus Architecture  
> **Benchmark:** 452-test validation across 9 suites (99 s real execution) · 100.0% pass rate · Φ = 80.7%  
> **Paper:** [`docs/contextforge_research.tex`](docs/contextforge_research.tex)

---

## The Stateless RAG Gap

Standard Retrieval-Augmented Generation systems are architecturally stateless. Each agent turn begins with a blank slate: no memory of prior decisions, no semantic index of past work, and no guard against adversarial content injected through retrieved chunks.

This creates three concrete, measurable failure modes:

| Failure Mode | Stateless RAG | Consequence |
|:-------------|:-------------:|:------------|
| **Adversarial injection** | No entropy gate | 0% adversarial block rate |
| **Provider outage** | Cold-start retry | ~480 ms recovery latency |
| **Noisy context** | Inject all chunks | 110 K+ irrelevant tokens per session |

ContextForge closes all three gaps with mathematically grounded, independently reproducible mechanisms — validated against a 100-probe dual-pass benchmark (`benchmark/engine.py`) and a 375-test OMEGA-75 suite with 99 seconds of real in-process execution.

---

## The Nexus Pillars

### 1. Dual-Signal Entropy-Gated Security

Every write to the agent memory ledger passes through `ReviewerGuard`, which applies a **dual-signal gate** combining Shannon entropy with a Lempel–Ziv compression density check:

$$H(X) = -\sum_{i} p(x_i) \log_2 p(x_i) \qquad \rho(\mathbf{w}) = \frac{|\text{LZ}(\mathbf{w})|}{|\mathbf{w}|}$$

A write is flagged when **either** $H > H^* = 3.5$ bits (obfuscated/high-vocabulary payload) **or** $\rho < 0.60$ (repetition attack). This defence-in-depth ensures no single evasion strategy — raise vocabulary diversity *or* lower it — bypasses the filter.

Flagged writes are **quarantined** rather than hard-blocked: they enter a `quarantine_events` table for secondary async validation, preserving ledger availability. A **Tiered Clearance Logic** grants authenticated internal traffic an elevated threshold of $H^*_\text{VOH} \approx 4.38$ bits, reducing false positives for legitimate high-entropy technical content. Under the full deployed system, the semantic poison suite records **zero false positives** across all 10 benign technical probes (JWT, PostgreSQL RLS, gRPC, Terraform, Redis, agent events).

**Measured result:** +85.0 pp adversarial block rate vs. the Stateless RAG baseline (0% → 85%).

---

### 2. Predictive Failover

`NexusRouter` maintains a CLOSED → OPEN → HALF_OPEN circuit breaker per LLM provider (Groq, Gemini, Ollama). When input entropy exceeds $H^*$ *and* Groq is the primary candidate, a 1-token background ping is dispatched to Gemini:

```python
if entropy > 3.5 and order[0] == "groq":
    asyncio.ensure_future(self._prewarm_gemini())   # fire-and-forget TCP/TLS prewarm
```

This pre-warms the connection, eliminating ~350 ms of cold-start overhead from the failover critical path.

**Measured result:** −68.9% failover latency (480 ms → 149.5 ms, live-measured).

---

### 3. Differential Context Injection (DCI)

`LocalIndexer` retrieves file chunks and gates injection on cosine similarity, subject to a token budget:

$$\text{inject chunk}_i \iff s_i \geq \theta = 0.75 \;\wedge\; \sum_{j \leq i} \hat{\tau}_j \leq B_{\text{token}}$$

Only semantically relevant chunks enter the LLM context. With `sentence-transformers/all-MiniLM-L6-v2`, this eliminates 87.4% of noisy context tokens while retaining all relevant content.

**Measured result:** 87.4% token noise reduction (zero irrelevant tokens injected in TF-IDF fallback mode).

---

## Scientific Delta — Live Benchmark Results

Measured on 100 probes × 2 modes via [`benchmark/engine.py`](benchmark/engine.py). The OMEGA-75 suite executed 375 tests with **99 seconds of real in-process execution** (SQLite WAL I/O, hash-chain verification, concurrent stress loads up to 500 writers — no mocking on the architectural layer).

| Dimension | Stateless RAG Baseline | ContextForge Nexus | Delta |
|:----------|:---------------------:|:-----------------:|:-----:|
| Adversarial block rate | 0.0% | **85.0%** | **+85.0 pp** |
| FP rate — unauthenticated writes | 0.0% | 70.0% | addressed by Tiered Clearance |
| FP rate — VOH traffic (deployed) | — | **0%** | zero FP on 10 benign probes |
| Mean failover latency | 480.0 ms | **149.5 ms** | **−330.5 ms (−68.9%)** |
| Token noise reduction | 0% (inject all) | **87.4%** (ST) | **+87.4 pp** |
| OMEGA-75 benchmark pass rate | 68.3% (prior baseline) | **100.0%** | **+31.7 pp** |
| Context survival rate | 74.0% | **94.3%** | **+20.3 pp** |
| **Weighted Composite Safety Index Φ** | — | — | **+80.7%** |

$$\Phi = w_S \cdot \Delta S + w_L \cdot \Delta L_{\%} + w_\text{DCI} \cdot \Delta_\text{DCI} = 0.5(85.0) + 0.3(68.9) + 0.2(87.4) = \mathbf{80.7\%}$$

Φ is stable across weight perturbations: $w_S \in [0.3, 0.7]$ yields Φ ∈ [79.3%, 82.0%], confirming the result is not an artefact of the chosen weights.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              ContextForge — Nexus Architecture           │
├──────────────┬──────────────────────────────────────────┤
│  Transport   │  Stdio (Claude Desktop) + SSE/HTTP        │
│  Router      │  Groq → Gemini → Ollama + Circuit Breaker │
│  Memory      │  Append-only SQLite ledger + ReviewerGuard│
│  Retrieval   │  Local-edge RAG + DCI cosine gate θ ≥ 0.75│
│  Sync        │  AES-256-GCM .forge snapshots + idle CP   │
└──────────────┴──────────────────────────────────────────┘
```

| Pillar | Module | Role |
|--------|--------|------|
| **Transport** | [`src/transport/server.py`](src/transport/server.py) | Dual-mode MCP: Stdio + SSE/HTTP |
| **Router** | [`src/router/nexus_router.py`](src/router/nexus_router.py) | Tri-Core LLM failover + circuit breaker + entropy prewarm |
| **Memory** | [`src/memory/ledger.py`](src/memory/ledger.py) | Append-only event ledger + ReviewerGuard + microsecond rollback |
| **Retrieval** | [`src/retrieval/local_indexer.py`](src/retrieval/local_indexer.py) | Local-edge speculative RAG, zero cloud tokens |
| **Sync** | [`src/sync/fluid_sync.py`](src/sync/fluid_sync.py) | AES-256-GCM encrypted snapshots + 15-min idle checkpoint |

---

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure (copy and edit .env)
cp .env.example .env

# Launch — Stdio mode (Claude Desktop / Cursor)
python -m src.transport.server --stdio

# Launch — SSE/HTTP mode (remote, port 8765)
python -m src.transport.server --sse --host 0.0.0.0 --port 8765

# Full interactive agent loop
python main.py
```

---

## Python API

```python
import asyncio
from src.memory.ledger import EventLedger, EventType
from src.router.nexus_router import get_router
from src.retrieval.jit_librarian import JITLibrarian
from src.sync.fluid_sync import FluidSync

# Append-only memory ledger — entropy gate active by default
ledger   = EventLedger(db_path="data/contextforge.db")
event_id = ledger.append(
    event_type = EventType.AGENT_THOUGHT,
    content    = {"text": "Implement JWT refresh token rotation"},
)
# Microsecond-precision rollback via rowid ordering
ledger.rollback(event_id)

# Tri-core LLM router with circuit breaker + predictive failover
router   = get_router()
response = asyncio.run(router.complete(
    messages    = [{"role": "user", "content": "Summarise the auth module"}],
    temperature = 0.3,
))

# Differential Context Injection — local-edge, zero cloud tokens
jit     = JITLibrarian(project_root=".", token_budget=1500)
context = asyncio.run(jit.get_context("JWT authentication", threshold=0.75))
print(context.to_string())   # injection-ready, deduped context block

# AES-256-GCM encrypted snapshot
sync          = FluidSync(ledger, snapshot_dir=".forge")
snapshot_path = sync.create_snapshot(label="before-refactor")
```

---

## Chaos & Concurrency Validation

The Heat-Death Chaos suite (Suite 05, 44.6 s elapsed) stress-tested the ledger and router under extreme concurrent load:

| Stress Profile | Writers | Events/Writer | Result |
|:---|:---:|:---:|:---|
| Concurrent router calls | 10 | — | 10/10 succeeded |
| Concurrent router calls | 50 | — | 50/50 succeeded |
| Ledger concurrent appends | 50 | 1 | 50 unique IDs, no collision |
| Ledger stress | 10 | 3 | Survived |
| Ledger stress | 50 | 10 | Survived |
| Ledger stress | 100 | 20 | Survived |
| Ledger stress | 200 | 5 | Survived |
| Ledger stress | 500 | 2 | Survived |
| All providers failed | — | — | Graceful degradation |
| Corrupt hash chain | — | — | Append continues |
| Rollback then flood | — | 100 | 101 events exported consistently |

The ledger runs in **WAL mode** (`journal_mode: wal`), confirmed by `test_ledger_wal_mode_enabled`. WAL enables concurrent readers and a single writer without blocking — critical for agentic workloads where reads vastly outnumber writes.

---

## Reproducing the Benchmark

```bash
# Dual-pass scientific benchmark — 100 probes × 2 modes
# Writes: data/academic_metrics.json
python -X utf8 benchmark/engine.py

# OMEGA-75 five-suite validation — 375 tests, 100% pass rate (99 s real execution)
python -X utf8 benchmark/test_v5/run_all.py

# Run individual suites
python -X utf8 benchmark/test_v5/iter_01_core.py    # Core Network  (4.7 s)
python -X utf8 benchmark/test_v5/iter_02_ledger.py  # Temporal Integrity  (37.2 s)
python -X utf8 benchmark/test_v5/iter_03_poison.py  # Adversarial Guard  (5.7 s)
python -X utf8 benchmark/test_v5/iter_04_scale.py   # RAG & DCI  (6.8 s)
python -X utf8 benchmark/test_v5/iter_05_chaos.py   # Heat-Death Chaos  (44.6 s)

# Regenerate publication charts at 300 DPI → docs/assets/
python -X utf8 benchmark/generate_viz.py
```

See [`data/academic_metrics.md`](data/academic_metrics.md) for the mathematical synthesis and [`docs/GUIDE.md`](docs/GUIDE.md) for the full engineering reference.

---

## Publication Outputs

| Asset | Description |
|-------|-------------|
| [`docs/assets/radar_comparison.png`](docs/assets/radar_comparison.png) | 6-pillar spider: Stateless RAG vs ContextForge (300 DPI) |
| [`docs/assets/entropy_gate_profile.png`](docs/assets/entropy_gate_profile.png) | H distribution with H* = 3.5 gate line (300 DPI) |
| [`docs/assets/failover_performance.png`](docs/assets/failover_performance.png) | T_failover comparison by scenario (300 DPI) |
| [`docs/contextforge_research.tex`](docs/contextforge_research.tex) | Submission-ready LaTeX paper |
| [`data/academic_metrics.md`](data/academic_metrics.md) | Full ΔS / ΔL / ΔDCI mathematical synthesis |
| [`data/academic_metrics.json`](data/academic_metrics.json) | Machine-readable benchmark results |

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <em>ContextForge Nexus Architecture — reproducible, information-theoretically grounded agentic memory.</em>
</p>
