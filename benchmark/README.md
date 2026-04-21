# ContextForge Benchmark Suite

End-to-end adversarial evaluation pipeline for the ContextForge Nexus
architecture.  Covers correctness, temporal integrity, poison resistance,
scale stress, chaos, adaptive adversaries, and **independent external
validation** against a publicly available prompt-injection dataset.

---

## Suite overview

| Suite | File | Tests | Focus |
|-------|------|------:|-------|
| **iter_01** | `test_v5/iter_01_core.py` | 75 | Networking, circuit breaker |
| **iter_02** | `test_v5/iter_02_ledger.py` | 75 | Temporal integrity, hash chain |
| **iter_03** | `test_v5/iter_03_poison.py` | 75 | Semantic poison, charter guard |
| **iter_04** | `test_v5/iter_04_scale.py` | 75 | RAG flooding, token budget |
| **iter_05** | `test_v5/iter_05_chaos.py` | 75 | Heat-death / full chaos |
| **iter_06** | `test_v5/iter_06_adversarial_boundary.py` | 75 | Entropy gate boundary, circuit breaker |
| **suite_06** | `suite_06_external_baseline.py` | — | Nexus vs StatelessRAG baseline (+85 pp) |
| **suite_07** | `suite_07_temporal_correlator.py` | 30 | Slow-drip detection |
| **suite_08** | `suite_08_fpr_calibration.py` | — | FPR/FNR calibration curve |
| **suite_09** | `suite_09_voh_multiprocess.py` | — | VOH multi-process trust |
| **suite_10** | `suites/suite_10_adaptive.py` | **30** | Adaptive adversary (boundary/mimicry/slow-drip) |
| **external** | `external/run_external_eval.py` | — | Independent: deepset/prompt-injections |

---

## Quick start

```bash
# Full internal suite (375 tests, ~2 min)
python -X utf8 benchmark/test_v5/run_all.py

# Adaptive adversary suite (30 tests, <5 s)
python -X utf8 benchmark/suites/suite_10_adaptive.py

# External validation — live HuggingFace download (116 samples, test split)
python -X utf8 benchmark/external/run_external_eval.py

# External validation — full dataset (train + test, ~662 samples)
python -X utf8 benchmark/external/run_external_eval.py --splits train test

# External validation — offline / no internet (static fallback corpus)
python -X utf8 benchmark/external/run_external_eval.py --offline

# Automated threshold recalibration (runs after external eval if F1 drops)
python -X utf8 src/security/gate_calibrator.py
```

---

## External validation (`benchmark/external/`)

### Purpose

Provides an **independent, out-of-sample** check that the entropy gate
threshold H\*=3.5, calibrated on the internal probe set (F1=1.0 reported
in the paper), generalises to publicly collected adversarial prompts.

### Dataset

**`deepset/prompt-injections`** — CC-BY-4.0
- URL: <https://huggingface.co/datasets/deepset/prompt-injections>
- 662 samples: 546 train + 116 test
- Labels: `1` = prompt injection / adversarial, `0` = benign

No HuggingFace account or `datasets` package is required.  Data is fetched
via the public Datasets Viewer REST API using `requests` (already in
`requirements.txt`).

### Files

| File | Role |
|------|------|
| `benchmark/external/__init__.py` | Package marker |
| `benchmark/external/promptbench_adapter.py` | Loads HF dataset (or static fallback), runs each prompt through ReviewerGuard, records H / ρ / block decision |
| `benchmark/external/run_external_eval.py` | Orchestrates eval, computes metrics, saves JSON, prints recalibration warning |

### Running the eval

```bash
# Default: test split (~116 samples, requires internet)
python -X utf8 benchmark/external/run_external_eval.py

# Full dataset
python -X utf8 benchmark/external/run_external_eval.py --splits train test

# Quick smoke-test (first 30 rows per split)
python -X utf8 benchmark/external/run_external_eval.py --max 30

# Offline (bundled 120-sample static corpus, no internet)
python -X utf8 benchmark/external/run_external_eval.py --offline

# Custom output path
python -X utf8 benchmark/external/run_external_eval.py \
    --splits train test \
    --out research/benchmark_results/external_abr_full.json
```

### Output

Results are saved to `research/benchmark_results/external_abr.json`:

```json
{
  "eval_name":   "external_adversarial_eval",
  "dataset":     "deepset/prompt-injections",
  "metrics": {
    "block_rate_on_adversarial": 0.85,
    "block_rate_on_benign":      0.34,
    "precision":  0.73,
    "recall":     0.85,
    "macro_f1":   0.76
  },
  "recalibration_recommended": true,
  "per_sample":  [...]
}
```

### Calibration warning

If the external macro-F1 deviates from the paper's F1=1.0 by more than
0.05, the eval prints:

```
⚠  THRESHOLD RECALIBRATION RECOMMENDED
   External macro-F1 = X.XX  (paper F1 = 1.00, delta = X.XX)
   Run:  python -X utf8 src/security/gate_calibrator.py
```

The calibrator performs a two-phase golden-section search over H\* and
ρ\_min to find the threshold pair that maximises macro-F1 on the provided
attack corpus.

### Interpreting the results

The external eval produces lower macro-F1 than the paper's internal
measurement because:

1. **Broader adversarial definition** — the deepset dataset includes
   jailbreaks and off-topic injections that are low-entropy (short,
   few unique words) and not caught by the entropy gate alone.

2. **Natural-language benign FP** — benign prompts in the external
   dataset are longer conversational sentences with rich vocabularies,
   pushing word entropy above H\*=3.5 and triggering false positives.

These findings motivate the domain-specific calibration workflow in
`src/security/gate_calibrator.py` and are documented in §7 of the paper.

---

## Adaptive adversary suite (`benchmark/suites/suite_10_adaptive.py`)

Tests a knowledgeable adversary who knows H\*=3.5 and gradient
threshold=0.15, and deliberately crafts payloads to sit just below those
boundaries.

### Attack classes

| Class | Tests | Description |
|-------|------:|-------------|
| **A — Boundary** | 10 | Entropy in [H\*−0.30, H\*+0.30]; measures gate classification accuracy |
| **B — Mimicry** | 10 | Word distribution tuned to benign µ=2.74 bits; gate evaded by H alone |
| **C — Slow drip** | 10 | Per-write gradient ≈ 0.14 bits/write (just under 0.15 threshold) |

### Key findings

- **LZ density gate is decisive**: word-cycling construction (equal-frequency
  N unique words) produces compressible text (mean ρ≈0.38), caught by the
  LZ gate even when the entropy gate is evaded.  Validates the dual-signal
  OR-gate design.

- **Entropy quantization limits gradient precision**: achievable gradients
  are discrete (log2(integer)), so ~40-60% of intended-evasion sequences
  accidentally overshoot the 0.15 threshold and are flagged by the
  Temporal Correlator.

Results: `research/benchmark_results/adaptive_abr.json`

---

## Gate calibrator (`src/security/gate_calibrator.py`)

Automates the "future work" item from the paper: given any new attack
corpus, recomputes optimal H\* and ρ\_min using binary search over F1.

```bash
# Run with default AdaptiveAttacker corpus (100 samples)
python -X utf8 src/security/gate_calibrator.py --verbose

# Custom output
python -X utf8 src/security/gate_calibrator.py \
    --out research/benchmark_results/calibration_result.json
```

Results: `research/benchmark_results/calibration_result.json`

---

## Results directory

All suite outputs land in `research/benchmark_results/`:

| File | Produced by |
|------|-------------|
| `adaptive_abr.json` | `suite_10_adaptive.py` |
| `external_abr.json` | `run_external_eval.py` |
| `calibration_result.json` | `gate_calibrator.py` |
| `suite_06_external_baseline.json` | `suite_06_external_baseline.py` |
| `suite_07_temporal_correlator.json` | `suite_07_temporal_correlator.py` |
| `suite_08_fpr_calibration.json` | `suite_08_fpr_calibration.py` |
| `suite_09_voh_multiprocess.json` | `suite_09_voh_multiprocess.py` |
| `iter_06_adversarial_boundary.json` | `iter_06_adversarial_boundary.py` |
| `final_combined_results.json` | `test_v5/run_all.py` |
| `ablation_report.md` | `test_v5/ablation_audit.py` |

---

## Mock vs Live benchmark modes

All suites default to **mock mode** — a deterministic, rule-based LLM stub
(seeded RNG, no network calls).  This keeps CI fast, reproducible, and
free of API-key requirements.

**Live mode** routes every LLM call through the real `NexusRouter`
(`src/router/nexus_router.py`), giving actual latency figures, real token
counts, and genuine model responses.

### API key setup

Set **at least one** of the following environment variables before running
any live-mode command:

| Variable | Provider | Where to get it |
|----------|----------|-----------------|
| `GROQ_API_KEY` | Groq (Llama 3.3 70B) | <https://console.groq.com> |
| `GEMINI_API_KEY` | Google Gemini 2.5 Flash | <https://aistudio.google.com/app/apikey> |
| `OLLAMA_HOST` | Local Ollama | `http://localhost:11434` (default) |
| `OLLAMA_MODEL` | Ollama model name | e.g. `llama3.2` |

NexusRouter tries providers in order: **Groq → Gemini → Ollama**.
If a provider fails or its key is absent, it falls back to the next one.
A "System Overloaded" soft-error is returned only when all configured
providers fail.

```bash
# Export keys (Linux / macOS / Git Bash on Windows)
export GROQ_API_KEY=gsk_...
export GEMINI_API_KEY=AIza...

# Or on Windows PowerShell:
$env:GROQ_API_KEY  = "gsk_..."
$env:GEMINI_API_KEY = "AIza..."
```

### Running the live benchmark (50 representative tests)

```bash
# Full 50-test live run — saves to results/live_results.json
python -X utf8 benchmark/live_runner.py

# Dry-run: validates keys and prints test list without making any API calls
python -X utf8 benchmark/live_runner.py --dry-run

# Verbose: print first 120 chars of each LLM response as it arrives
python -X utf8 benchmark/live_runner.py --verbose

# Smoke test: run only the first 10 tests
python -X utf8 benchmark/live_runner.py --limit 10

# Custom output path
python -X utf8 benchmark/live_runner.py --out results/my_live_run.json
```

#### Live test corpus structure

The 50 tests are split evenly across the five suite dimensions:

| Suite | Tests | Focus |
|-------|------:|-------|
| **01 — Networking** | 10 | Routing decisions, circuit breaker logic |
| **02 — Ledger** | 10 | Temporal integrity, hash-chain questions |
| **03 — Poison** | 10 | 7 adversarial injections + 3 benign |
| **04 — RAG** | 10 | Retrieval quality, token efficiency |
| **05 — Chaos** | 10 | Graceful degradation, resilience |

Each result record includes: `provider_used`, `latency_ms`, `prompt_tokens`,
`response_tokens`, `response_preview`, `passed`, and `error`.

#### Expected output (results/live_results.json)

```json
{
  "runner": "live_runner",
  "runner_version": "1.0",
  "benchmark_mode": "live",
  "run_at": "2026-04-14T12:00:00Z",
  "config": {
    "live_timeout_sec": 30,
    "live_max_tokens": 512,
    "live_temperature": 0.3
  },
  "summary": {
    "total_tests": 50,
    "passed": 49,
    "failed": 1,
    "pass_rate": 0.98,
    "mean_latency_ms": 1240.5,
    "p95_latency_ms": 3100.0,
    "adversarial_tests": 11,
    "by_suite": { ... }
  },
  "tests": [ ... ]
}
```

### Comparing mock vs live results

After running both the mock multi-seed runner and the live runner, diff them:

```bash
# Run mock benchmark (N=10 seeds) — saves results/comparison_table.json
python -X utf8 benchmark/runner.py

# Run live benchmark — saves results/live_results.json
python -X utf8 benchmark/live_runner.py

# Compute drift and flag metrics that deviate more than 10 %
python results/compare_mock_vs_live.py
```

The drift script compares three analogous metrics:

| Mock metric | Live metric | Comparable? |
|-------------|-------------|-------------|
| Nexus ABR (adversarial block rate) | Adversarial pass rate | Yes |
| Nexus `failover_ms` (simulated) | `mean_latency_ms` (real) | Yes, but expected to differ |
| Nexus CTO (context tokens out) | Mean total tokens per test | Yes |

Any metric that drifts more than 10 % (relative) is printed with
**DRIFT DETECTED** and the script exits with code 1.  Latency drift is
flagged separately as **DRIFT EXPECTED** since simulated (80–350 ms) and
real network latency are fundamentally different.

Results are saved to `results/mock_vs_live_drift.json`.

```bash
# Stricter 5 % threshold
python results/compare_mock_vs_live.py --threshold 0.05

# Print table only, no JSON saved
python results/compare_mock_vs_live.py --no-save

# Custom input paths
python results/compare_mock_vs_live.py \
    --mock results/comparison_table.json \
    --live results/my_live_run.json
```

### Tunable live-mode settings

All settings can be overridden via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BENCHMARK_MODE` | `mock` | Set to `live` to activate real API calls |
| `LIVE_TIMEOUT_SEC` | `30` | Per-call timeout in seconds |
| `LIVE_MAX_TOKENS` | `512` | Max tokens requested per call |
| `LIVE_TEMPERATURE` | `0.3` | LLM temperature for live calls |
| `MOCK_SEED` | `42` | RNG seed for the deterministic mock |

```bash
# Run live runner with tighter timeout and lower token budget
export LIVE_TIMEOUT_SEC=15
export LIVE_MAX_TOKENS=256
python -X utf8 benchmark/live_runner.py
```

### CI safety

`BENCHMARK_MODE` defaults to `mock`.  CI pipelines that do not export any of
the API key variables will automatically use mock mode — no changes to CI
configuration are needed.
