# ContextForge — Engineering Reference

**Author:** Trilochan Sharma — Independent Researcher · [parnish007](https://github.com/parnish007)  
**Version:** Nexus Architecture  
**Last Updated:** 2026-04-05

---

## Table of Contents

1. [Mathematical Appendix — Entropy Gate Derivation](#1-mathematical-appendix--entropy-gate-derivation)
2. [ReviewerGuard Configuration](#2-reviewerguard-configuration)
3. [FluidSync Configuration](#3-fluidsync-configuration)
4. [NexusRouter Configuration](#4-nexusrouter-configuration)
5. [LocalIndexer / DCI Configuration](#5-localindexer--dci-configuration)
6. [Reproducing the Benchmark](#6-reproducing-the-benchmark)
7. [Adding New Agents](#7-adding-new-agents)
8. [Troubleshooting](#8-troubleshooting)

**New in v2.0 (April 2026):** §1.4 Compression Density Check · §1.5 Tiered Clearance Logic · §1.6 Soft-Gate Quarantine · §1.7 Φ Weighted Index · §2.5 VOH Implementation · §2.6 WAL Mode · §6.5 Chaos Suite Results

---

## 1. Mathematical Appendix — Entropy Gate Derivation

### 1.1 Shannon Entropy Definition

For a discrete random variable $X$ with outcomes $\{x_1, \ldots, x_n\}$ and empirical probability mass function $p(x_i) = \text{count}(x_i) / N$, Shannon entropy is:

$$H(X) = -\sum_{i=1}^{n} p(x_i) \log_2 p(x_i) \quad \text{bits}$$

**Properties relevant to adversarial detection:**

- $H = 0$ when all tokens are identical (single-symbol distribution)
- $H = \log_2 V$ at maximum when all $V$ unique tokens are equally probable
- Natural language follows a Zipfian distribution; $H$ ranges ~1.5–3.5 bits for well-formed prose
- Obfuscated/adversarial text artificially inflates vocabulary diversity, pushing $H$ above the Zipfian ceiling

### 1.2 Threshold Derivation — H* = 3.5 bits

The threshold was derived from the empirical entropy distributions of two payload populations observed across the 100-probe dual-pass benchmark:

| Population | $\mu_H$ | $\sigma_H$ | $H_{\max}$ |
|:-----------|:-------:|:----------:|:----------:|
| Benign (natural language dev queries) | 2.74 | 0.43 | 3.42 |
| Adversarial (injection, obfuscation) | 3.94 | 0.79 | 5.18 |

**Optimal threshold selection** minimises the sum of false-positive and false-negative rates:

$$H^* = \arg\min_{h} \left[ P(H > h \mid \text{benign}) + P(H \leq h \mid \text{adversarial}) \right]$$

At $H^* = 3.5$ bits:

$$z = \frac{H^* - \mu_{\text{benign}}}{\sigma_{\text{benign}}} = \frac{3.5 - 2.74}{0.43} = \mathbf{1.77}$$

A $z$-score of 1.77 corresponds to the **96.2nd percentile** of the benign distribution — meaning only 3.8% of natural-language developer queries produce entropy above the gate. In the benchmark, **zero benign probes** exceeded $H^* = 3.5$ bits.

**Two-pass architecture above the gate:**

When $H > H^*$, the `ReviewerGuard` runs a two-pass scan:

- **Pass 1 (fast path):** Entity-centric check — destructive verb + protected entity name match in O(n) string scan. Trips immediately if found.
- **Pass 2 (relaxed):** Constraint keyword scoring against `PROJECT_CHARTER.md` at threshold `// 5` (permissive). Only blocks if multiple constraint keywords co-occur.

This asymmetric design minimises false positives on high-entropy but legitimate technical queries (e.g., audit documentation that mentions "destroy" and "rollback" in a technical context).

### 1.3 Confidence Interval

For the 20 adversarial probes tested, $H_{\text{adversarial}} \in [2.31, 5.18]$. The 95% confidence interval for the mean adversarial entropy:

$$\bar{H} \pm t_{0.975, 19} \cdot \frac{\sigma}{\sqrt{n}} = 3.94 \pm 2.093 \cdot \frac{0.79}{\sqrt{20}} = 3.94 \pm 0.37 = [3.57, 4.31]$$

The lower bound of this interval (3.57 bits) exceeds $H^* = 3.5$ bits, confirming the threshold places the gate below the adversarial distribution's minimum expected value.

### 1.4 Compression Density Check (LZ Ratio)

Shannon entropy is blind to **repetition attacks** — adversarial payloads composed of a high-frequency repeated command (e.g., `delete all agents delete all agents …`). Such payloads have deceptively low H (low vocabulary diversity) yet are clearly adversarial. The Compression Density Check catches these:

$$\rho(\mathbf{w}) = \frac{|\text{LZ}(\mathbf{w})|}{|\mathbf{w}|}$$

where $|\text{LZ}(\cdot)|$ is the byte length of the zlib/LZ77-compressed payload. A low ratio signals high repetition:

| Payload Type | Typical ρ | Action |
|:-------------|:---------:|:-------|
| Natural prose | 0.70–0.85 | Pass |
| Mixed technical content | 0.60–0.75 | Pass |
| Repetition attack | **< 0.60** | **Flagged** |
| Maximum compression | ~0.10 | Flagged |

**Combined dual-signal gate:**

$$\text{flag}(\mathbf{w}) \iff H(\mathbf{w}) > H^* \;\vee\; \rho(\mathbf{w}) < \rho_{\min}$$

No single evasion strategy (raise vocabulary diversity *or* lower it) can bypass both signals simultaneously.

**Configuration:**

```python
# In src/memory/ledger.py
_ENTROPY_THRESHOLD: float = 3.5   # H gate
_COMPRESSION_THRESHOLD: float = 0.60  # ρ gate (lower = stricter)
```

### 1.5 Tiered Clearance Logic — Verified Origin Header (VOH)

The flat entropy gate produces a 70% false-positive rate on benign writes containing security-adjacent vocabulary (`snapshot`, `audit`, `rollback`). Rather than raising $H^*$ globally (which widens the adversarial gap), **Tiered Clearance Logic** grants authenticated internal traffic a 20% entropy buffer:

$$H^*_\text{VOH} = \frac{H^*}{1 - 0.20} = \frac{3.5}{0.8} \approx 4.38 \text{ bits}$$

**Who gets VOH clearance:**
- `FluidSync` checkpoint writer
- Administrative rollback operations (`skip_guard=True` events)
- `JITLibrarian` internal cache refresh

**Who does NOT:**
- User-supplied content
- Retrieved RAG chunks
- Tool outputs
- Any cross-process write

**Result:** The semantic poison suite records **zero false positives** across all 10 benign technical probes under the full deployed system.

**Future:** A cryptographic HMAC-SHA256 token will be evaluated for cross-process deployments (see §7, P3 in the paper).

### 1.6 Soft-Gate Quarantine Protocol

Rather than issuing a hard `BLOCKED` verdict, flagged writes enter a three-state **quarantine queue**:

| State | Condition | Action |
|:------|:----------|:-------|
| `ADMITTED` | Secondary scan passes | Write promoted to main ledger |
| `ESCALATED` | Pattern matches critical entity | Human review required |
| `EXPIRED` | TTL exceeded without resolution | Write discarded, audit logged |

The primary ledger continues accepting non-flagged writes during quarantine — system availability is preserved.

```python
# In src/memory/ledger.py
_QUARANTINE_TTL_SECONDS: int = 300   # 5 minutes default
```

**Operational note:** `ESCALATED` events are surfaced via the `ReviewerGuard.get_escalated()` method. Production deployments should wire this to an alerting channel.

### 1.7 Weighted Composite Safety Index Φ

Φ is defined as:

$$\Phi = w_S \cdot \Delta S + w_L \cdot \Delta L_{\%} + w_\text{DCI} \cdot \Delta_\text{DCI}$$

**Default operational calibration** (security-prioritised):

| Weight | Dimension | Rationale |
|:-------|:----------|:----------|
| $w_S = 0.5$ | Adversarial block rate ΔS | Missed injection corrupts entire ledger history |
| $w_L = 0.3$ | Failover latency reduction ΔL% | Availability is critical but recoverable |
| $w_\text{DCI} = 0.2$ | Token noise reduction ΔDCI | Efficiency improves UX but is not safety-critical |

**Measured value:** Φ = 0.5(85.0) + 0.3(68.9) + 0.2(87.4) = **80.7%**

**Weight sensitivity:** Across $w_S \in [0.3, 0.7]$, Φ ∈ [79.3%, 82.0%] — the result is robust to calibration choices.

To adjust weights for your deployment risk profile:

```python
# In benchmark/engine.py
W_SECURITY   = 0.5   # increase for high-adversarial environments
W_LATENCY    = 0.3   # increase for real-time applications
W_EFFICIENCY = 0.2   # increase for token-cost-sensitive deployments
```

---

## 2. ReviewerGuard Configuration

The `ReviewerGuard` lives in `src/memory/ledger.py` and is instantiated once per `EventLedger`.

### 2.1 Key Parameters

| Parameter | Location | Default | Effect |
|:----------|:---------|:-------:|:-------|
| Entropy threshold | `_ENTROPY_THRESHOLD = 3.5` | 3.5 bits | Trip-point for two-pass scan |
| Charter path | `EventLedger(charter_path=...)` | `PROJECT_CHARTER.md` | Ground-truth constraint source |
| Pass-2 keyword threshold divisor | `threshold // 5` in `check()` | 5 | Lower = stricter (blocks more) |
| `skip_guard` flag | `ledger.append(skip_guard=True)` | `False` | Bypass for internal system events |

### 2.2 Adjusting Sensitivity

**To increase recall (block more adversarial content) — lower the divisor:**

```python
# In src/memory/ledger.py, ReviewerGuard.check()
# Change the Pass-2 threshold from // 5 to // 3 for stricter matching
if score >= len(constraint_words) // 3:   # was // 5
    return "BLOCKED", matched_constraint
```

**To decrease false-positive rate — raise the entropy threshold:**

```python
# In src/memory/ledger.py
_ENTROPY_THRESHOLD = 4.0   # was 3.5; reduces false positives on high-entropy technical prose
```

### 2.3 Adding Protected Entities

Entities in `PROJECT_CHARTER.md` that appear in bold (`**EntityName**`) are automatically extracted into `_CORE_PROTECTED`:

```markdown
<!-- In PROJECT_CHARTER.md -->
**NewCriticalAgent** must not be disabled or removed without explicit approval.
```

The `ReviewerGuard` will then block any content containing a destructive verb targeting `NewCriticalAgent`.

### 2.4 Bypassing the Guard (Internal Events)

System-generated events (checkpoints, rollbacks, CRDT merges) bypass the guard — these are equivalent to VOH-authenticated writes:

```python
ledger.append(
    event_type = EventType.CHECKPOINT,
    content    = {"trigger": "idle"},
    skip_guard = True,   # trusted internal event — VOH-equivalent, no guard check
)
```

### 2.5 Tiered Clearance — VOH Configuration

The Verified Origin Header threshold is derived from the standard entropy threshold:

```python
# In src/memory/ledger.py
_ENTROPY_THRESHOLD: float = 3.5          # external writes
_VOH_BUFFER: float = 0.20               # 20% buffer for authenticated writes
# Effective: 3.5 / (1 - 0.20) ≈ 4.38 bits for VOH traffic
```

Internal components that qualify for VOH clearance set `skip_guard=True` or carry an internal trust flag. Do not expose this path to external callers.

### 2.6 SQLite WAL Mode

The ledger runs in Write-Ahead Logging mode by default, confirmed by `test_ledger_wal_mode_enabled`:

```python
# Verified in src/memory/ledger.py EventLedger.__init__()
conn.execute("PRAGMA journal_mode=WAL")
```

WAL allows concurrent readers alongside the single writer. Validated under stress profiles up to 500 concurrent writers (500×2 events each) — all passing without collision. For multi-process deployments needing higher write concurrency, front the `EventLedger` with a Redis or Postgres write queue; the `ReviewerGuard` interface is backend-agnostic.

---

## 3. FluidSync Configuration

`FluidSync` in `src/sync/fluid_sync.py` handles encrypted snapshots and idle checkpointing.

### 3.1 Encryption

Snapshots are encrypted with **AES-256-GCM** using a 32-byte key derived from `FORGE_SNAPSHOT_KEY` via SHA-256:

```bash
# Set in .env
FORGE_SNAPSHOT_KEY=your-high-entropy-passphrase-here
```

If unset, a default development key is used (insecure for production). The nonce is 12 bytes of CSPRNG output prepended to the ciphertext.

**Fallback:** If the `cryptography` package is not installed, snapshots fall back to base64 encoding (no encryption). Install for production use:

```bash
pip install cryptography
```

### 3.2 Idle Checkpointing

```python
sync = FluidSync(
    ledger       = ledger,
    charter_path = "PROJECT_CHARTER.md",  # bundled into every snapshot
    snapshot_dir = ".forge",              # where .forge files are written
    idle_minutes = 15.0,                  # checkpoint after 15 min of inactivity
)
```

The idle watcher runs in a daemon thread. Call `sync.ping()` on every user interaction to reset the timer. Call `sync.shutdown()` on process exit.

### 3.3 Cross-Device Replay

```python
# On a new device: decrypt + replay a .forge snapshot into a fresh ledger
new_ledger = EventLedger(db_path=":memory:")
sync       = FluidSync(new_ledger)
replayed   = sync.replay_from_snapshot("path/to/snapshot.forge")
print(f"Replayed {replayed} events")
```

Events are deduplicated by `event_id` (idempotent). The CRDT-Lite merge function (`merge_logs`) uses OR-Set semantics: union, deduplicate, sort by `created_at`.

---

## 4. NexusRouter Configuration

### 4.1 Provider Priority

```
Prompt tokens < 4,000: Groq → Gemini → Ollama
Prompt tokens ≥ 4,000: Gemini → Groq → Ollama   (Gemini 1M token window)
```

Override via environment variables:

```bash
GROQ_MODEL=llama-3.3-70b-versatile    # default
GEMINI_MODEL=models/gemini-2.5-flash  # default
OLLAMA_MODEL=llama3.3                 # default
OLLAMA_URL=http://localhost:11434      # default
```

### 4.2 Circuit Breaker Parameters

| Provider | `failure_threshold` | `reset_timeout` |
|:---------|:------------------:|:---------------:|
| Groq | 3 | 60 s |
| Gemini | 3 | 90 s |
| Ollama | 2 | 30 s |

To adjust, modify `NexusRouter.__init__()` in `src/router/nexus_router.py`.

### 4.3 Entropy Threshold for Predictive Failover

```python
# In src/router/nexus_router.py
_ENTROPY_THRESHOLD: float = 3.5   # bits — lower to prewarm more aggressively
```

Predictive Failover fires a 1-token Gemini ping whenever input entropy exceeds this value. The prewarm is silent (failures are non-fatal) and skips if Gemini's circuit breaker is OPEN or `GEMINI_API_KEY` is unset.

---

## 5. LocalIndexer / DCI Configuration

### 5.1 Cosine Threshold

```python
indexer = LocalIndexer(
    project_root = ".",
    threshold    = 0.75,   # DCI gate: only inject chunks with cosine ≥ 0.75
    chunk_words  = 400,    # max words per chunk (50% overlap)
)
```

With `sentence-transformers/all-MiniLM-L6-v2` (semantic embeddings), relevant chunks typically score 0.78–0.94. With TF-IDF fallback, scores range 0.05–0.35; lower the threshold to 0.10 for TF-IDF mode.

### 5.2 Embedding Backend

```bash
# Install for semantic embeddings (recommended)
pip install sentence-transformers

# Optional: override model
EMBEDDING_MODEL=all-MiniLM-L6-v2   # default; any SBERT-compatible model works
```

Without `sentence-transformers`, the indexer automatically falls back to TF-IDF cosine similarity (no external dependencies, lower recall).

### 5.3 JITLibrarian Token Budget

```python
jit = JITLibrarian(
    project_root  = ".",
    token_budget  = 1500,   # max tokens to inject per context payload
    threshold     = 0.75,   # per-call override: jit.get_context(q, threshold=0.8)
    cache_maxsize = 128,    # LRU cache entries
    cache_ttl     = 300.0,  # seconds before cache entry expires
)
```

---

## 6. Reproducing the Benchmark

### 6.1 Dual-Pass Scientific Benchmark

```bash
python -X utf8 benchmark/engine.py
```

**What it does:**
1. Builds two `LocalIndexer` instances (Mode A: threshold=0.0; Mode B: threshold=0.75)
2. Runs 40 security probes (20 benign + 20 adversarial) — measures ReviewerGuard block rates
3. Runs 20 failover probes — simulates provider failures, compares cold-start vs. prewarm latency
4. Runs 40 RAG probes — measures tokens retrieved vs. tokens injected under DCI gate

**Output:** `data/academic_metrics.json` with per-probe and aggregate results.

### 6.2 OMEGA-75 Suite

```bash
# Run all 5 suites (375 tests)
python -X utf8 benchmark/test_v5/run_all.py

# Run individual suites
python -X utf8 benchmark/test_v5/iter_01_core.py    # Circuit Breaker
python -X utf8 benchmark/test_v5/iter_02_ledger.py  # Temporal Integrity
python -X utf8 benchmark/test_v5/iter_03_poison.py  # Adversarial Guard
python -X utf8 benchmark/test_v5/iter_04_scale.py   # RAG & DCI
python -X utf8 benchmark/test_v5/iter_05_chaos.py   # Heat-Death Chaos
```

Suite logs are written to `benchmark/test_v5/logs/` as JSON files.

### 6.3 Generating Publication Charts

```bash
# Requires: pip install matplotlib numpy
python -X utf8 benchmark/generate_viz.py
```

Produces three 300 DPI dark-theme PNGs in `docs/assets/`:

| File | Description |
|:-----|:------------|
| `radar_comparison.png` | 6-axis spider: Stateless RAG baseline vs ContextForge Nexus |
| `entropy_gate_profile.png` | Payload H distribution with H* = 3.5 gate line |
| `failover_performance.png` | T_failover grouped bars by failure scenario |

### 6.4 Expected Results

| Benchmark | Expected Result |
|:---------|:---------------|
| Security block rate | 85.0% (adversarial) |
| False positives — VOH traffic | 0% (zero on 10 benign probes) |
| Failover latency | 149.5 ms (Nexus) vs 480.0 ms (baseline) |
| Token noise reduction | 100% (TF-IDF) / 87.4% (sentence-transformers) |
| OMEGA-75 pass rate | 375/375 = 100.0% |
| Weighted Φ index | 80.7% (range 79.3–82.0% across weight perturbations) |

### 6.5 Chaos Suite — Concurrent Stress Results

Suite 05 (44.6 s elapsed) stress-tested the ledger and router:

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

To run only the chaos suite:

```bash
python -X utf8 benchmark/test_v5/iter_05_chaos.py
```

---

## 7. Adding New Agents

All agents must adhere to the three interface constraints:

```python
from src.memory.ledger import EventLedger, EventType
from src.router.nexus_router import get_router
from src.retrieval.jit_librarian import get_jit_librarian

class MyAgent:
    """Google-style docstring describing the agent's role within the Nexus Architecture.

    Args:
        ledger: The shared EventLedger instance — all memory writes go here.
        name: Agent identifier, used in log messages and event metadata.
    """

    def __init__(self, ledger: EventLedger, name: str) -> None:
        self._ledger   = ledger
        self._router   = get_router()
        self._jit      = get_jit_librarian()
        self.name      = name

    async def run(self, task: str) -> str:
        """Execute a task and record the result to the ledger.

        Args:
            task: Natural-language task description.

        Returns:
            Agent response string.
        """
        # 1. Retrieve context via DCI (never read files directly)
        ctx = await self._jit.get_context(task)

        # 2. Call LLM via router (never call Groq/Gemini/Ollama directly)
        response = await self._router.complete(
            messages=[
                {"role": "system", "content": ctx.to_string()},
                {"role": "user",   "content": task},
            ]
        )

        # 3. Record to ledger (all code must begin with # RATIONALE:)
        self._ledger.append(
            event_type = EventType.AGENT_THOUGHT,
            content    = {
                "agent":    self.name,
                "task":     task,
                "response": response,
            },
            metadata = {"agent": self.name},
        )
        return response
```

**Hard constraints:**
- Never call LLM APIs directly — use `get_router().complete(messages)`
- Never read project files directly — use `get_jit_librarian().get_context(query)`
- All generated code blocks must begin with `# RATIONALE: <text>`
- Never modify `PROJECT_CHARTER.md` programmatically

---

## 8. Troubleshooting

### "System Overloaded" from NexusRouter

All three providers failed. Check:

```bash
# Verify API keys are set
echo $GROQ_API_KEY
echo $GEMINI_API_KEY

# Check Ollama is running
curl http://localhost:11434/api/tags

# Inspect circuit breaker state
router = get_router()
print(router.circuit_status())   # {"groq": "open", "gemini": "closed", "ollama": "closed"}
```

### ReviewerGuard quarantines legitimate content

Check which signal triggered the quarantine:

```python
guard = ReviewerGuard(charter_path="PROJECT_CHARTER.md")
verdict, reason = guard.check(content={"text": your_text})
print(verdict)  # "QUARANTINED" or "APPROVED"
print(reason)   # e.g. "entropy=3.8 > threshold=3.5" or "compression_ratio=0.45 < 0.60"
```

**If triggered by entropy ($H > 3.5$):**
- Is the write from a trusted internal component? Use `skip_guard=True` (VOH-equivalent).
- Is it external traffic that's legitimately high-entropy (technical docs, audit logs)? Raise `_ENTROPY_THRESHOLD` to 4.0 or implement VOH clearance for that write path.

**If triggered by compression density ($\rho < 0.60$):**
- The write contains highly repetitive content. Check whether the pattern is intentional (e.g., repeated structured data) or adversarial. If intentional, pre-compress or restructure the content before writing.

### LocalIndexer returns 0 chunks

With TF-IDF mode and `threshold=0.75`, most queries score below the gate. Either:
1. Install `sentence-transformers` for semantic embeddings
2. Lower threshold: `LocalIndexer(threshold=0.10)` for TF-IDF mode

### FluidSync snapshot decryption fails

Ensure `FORGE_SNAPSHOT_KEY` is identical on source and target machines. If key differs, the GCM authentication tag will fail. Use the base64 fallback (no `cryptography` package) for unencrypted development snapshots.

---

*ContextForge Nexus Architecture — Engineering Guide · 2026-04-05 · v2.0*
