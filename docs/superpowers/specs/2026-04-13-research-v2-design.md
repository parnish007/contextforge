# Design Spec — ContextForge v2 Research Paper & research/ Directory

**Date:** 2026-04-13  
**Status:** Approved  
**Scope:** New `research/` folder at project root; v2 paper; migration of existing research files; path updates across all docs.

---

## 1. Goal

Produce a thorough, self-contained research paper (`research/contextforge_v2.tex`) that:
- Covers the full five-pillar Nexus architecture **and** the 22-tool MCP server as co-equal contributions
- Incorporates all new benchmark results (suites 07–09, iter_06) not in the v1 paper
- Includes a proper ablation section, token-economics section, and related-work comparison
- Ships every figure as a Python-generated PNG (reproducible via `python research/figures/gen_all.py`)
- Is git-publishable as a standalone directory

Simultaneously: migrate all existing research assets (`docs/contextforge_research.tex`, `docs/RESEARCH.md`, ablation report, benchmark JSON logs) into `research/`, update all cross-references in README and docs.

---

## 2. Directory Structure (target state)

```
research/
├── contextforge_v2.tex              ← new paper (replaces docs/contextforge_research.tex)
├── refs.bib                         ← extended bibliography (16+ citations)
├── figures/
│   ├── gen_all.py                   ← single entry point: generates all PNGs
│   ├── fig_system_architecture.py   ← five-pillar + MCP layer diagram
│   ├── fig_entropy_gate.py          ← H distribution + gate line
│   ├── fig_calibration_sweep.py     ← FPR/FNR/F1 vs H* sweep (suite 08)
│   ├── fig_temporal_correlator.py   ← slow-drip gradient separation (suite 07)
│   ├── fig_voh_tiers.py             ← tiered threshold diagram (H* vs H*_VOH)
│   ├── fig_ablation.py              ← ablation table as a styled figure
│   ├── fig_token_savings.py         ← CLAUDE.md vs ContextForge token growth
│   ├── fig_mcp_dataflow.py          ← MCP tool → pillar mapping diagram
│   ├── fig_radar.py                 ← six-pillar spider (update existing)
│   └── *.png                        ← generated outputs (committed)
├── benchmark_results/
│   ├── suite_06_external_baseline.json
│   ├── suite_07_temporal_correlator.json
│   ├── suite_08_fpr_calibration.json
│   ├── suite_09_voh_multiprocess.json
│   ├── iter_06_adversarial_boundary.json
│   └── ablation_report.md
└── v1_archive/
    └── contextforge_v1.tex          ← moved from docs/contextforge_research.tex

docs/
├── RESEARCH.md                      ← updated: points to research/ for assets
└── (all other docs unchanged, path refs updated)
```

---

## 3. Paper Sections (contextforge_v2.tex)

| # | Section | Key content | New vs v1 |
|---|---------|-------------|-----------|
| — | Abstract | Updated numbers: 100% slow-drip, F1=1.0, 452+77 tests, Φ=80.7% | Updated |
| 1 | Introduction | Three failure modes; ContextForge + MCP as solution | Expanded |
| 2 | Related Work | Stateless RAG, MemGPT/Letta, LangChain Memory, vector DBs, MCP spec | **New** |
| 3 | System Architecture | Five pillars + 8-agent RAT; figure: full system diagram | Expanded |
| 4 | MCP Server Layer | 22 tools → pillar mapping; stdio vs SSE; protocol rationale | **New** |
| 5 | Methodology | All math: H, LZ, DCI, VOH, Φ; two-pass guard; soft-gate quarantine | Expanded |
| 6 | Experimental Setup | Hybrid execution layers; all 9 suites | Updated |
| 7 | Results | Master table + per-dimension subsections; all new figures | Updated |
| 8 | Ablation Study | Proper section with ablation table (was in separate .md) | **New** |
| 9 | Token Economics | CLAUDE.md vs ContextForge at 20/100/200 decisions | **New** |
| 10 | Discussion | Limitations, production gaps, weight sensitivity | Updated |
| 11 | Future Work | Cross-process VOH, L3 semantic cache, CRDT full deploy | Updated |
| 12 | Conclusion | | Updated |
| — | References | 16+ citations incl. MemGPT, LangChain, MCP spec, SQLite WAL | Extended |

---

## 4. Figures (8 total)

| Figure | Generator script | Data source |
|--------|-----------------|-------------|
| System architecture (five pillars + MCP) | `fig_system_architecture.py` | Drawn (matplotlib + patches) |
| Entropy gate H distribution | `fig_entropy_gate.py` | Hardcoded probe stats from engine.py |
| FPR/FNR/F1 calibration sweep | `fig_calibration_sweep.py` | `suite_08_fpr_calibration.json` |
| Temporal correlator gradient separation | `fig_temporal_correlator.py` | `suite_07_temporal_correlator.json` |
| VOH tiered threshold diagram | `fig_voh_tiers.py` | Computed from H*=3.5, discount=0.20 |
| Ablation study bar chart | `fig_ablation.py` | `ablation_report.md` hardcoded values |
| Token savings growth curve | `fig_token_savings.py` | Computed formula |
| MCP tool → pillar mapping | `fig_mcp_dataflow.py` | Drawn (matplotlib) |

All scripts: `matplotlib` only (already in requirements). Output: 300 DPI PNG to `research/figures/`.

---

## 5. Bibliography additions (beyond v1)

| Cite key | Reference |
|----------|-----------|
| `packer2023memgpt` | Packer et al. 2023, MemGPT: Towards LLMs as Operating Systems |
| `chase2022langchain` | Chase 2022, LangChain: Building applications with LLMs through composability |
| `anthropic2024mcp` | Anthropic 2024, Model Context Protocol Specification |
| `ziv1978` | Ziv & Lempel 1978, Compression of individual sequences via variable-rate coding |
| `wright2020sqlite` | Hipp 2020, SQLite WAL Mode documentation |
| `johnson2021faiss` | Johnson et al. 2021, Billion-scale similarity search with GPUs |
| `xu2023memory` | Xu et al. 2023, A Survey on the Memory Mechanism of Large Language Model based Agents |
| `nygard2007` | Nygard 2007, Release It! (circuit breaker) — already in v1 |

---

## 6. Migration plan (files to move / update)

| Current path | Action | New path |
|-------------|--------|----------|
| `docs/contextforge_research.tex` | Move to v1 archive | `research/v1_archive/contextforge_v1.tex` |
| `docs/RESEARCH.md` | Update content + paths | stays at `docs/RESEARCH.md` (updated) |
| `benchmark/ablation_report.md` | Copy | `research/benchmark_results/ablation_report.md` |
| `benchmark/logs/suite_07_*.json` | Copy | `research/benchmark_results/` |
| `benchmark/logs/suite_08_*.json` | Copy | `research/benchmark_results/` |
| `benchmark/logs/suite_09_*.json` | Copy | `research/benchmark_results/` |
| `benchmark/test_v5/logs/iter_06_*.json` | Copy | `research/benchmark_results/` |
| `docs/assets/*.png` | Keep; also generate updated versions in `research/figures/` | both |

**Docs to update references in:**
- `README.md` — paper link, assets table
- `docs/RESEARCH.md` — all asset paths
- `docs/SETUP.md` — directory structure section
- `docs/ARCHITECTURE.md` — paper reference footnote
- `mcp/README.md` — paper link

---

## 7. Git publish audit (what to keep / remove)

**Keep and publish:**
- `research/` (entire directory)
- All `docs/` markdown files
- `benchmark/` (suites, engine, run_all — fully reproducible)
- `mcp/` (both server.py and index.ts)
- `src/` (all modules)
- `prompts/` (skill prompts)
- `images/` (banner, hook)
- `.env.example`, `requirements.txt`, `LICENSE`

**Remove from git / gitignore:**
- `data/contextforge.db` (already untracked, confirm .gitignore)
- `data/test_e2e.db` (same)
- `**/__pycache__/`
- `mcp/dist/` (built output)
- `.env` (secrets)
- `benchmark/logs/*.json` (generated; keep copies in `research/benchmark_results/`)
- `*.pyc`

**Clarify status of:**
- `docs/MCP_PLAN.md`, `docs/MCP_SETUP.md` — untracked, evaluate if they add value or are superseded by SETUP.md
- `prompts/skills/pm/`, `prompts/skills/researcher/` — untracked, include if content exists

---

## 8. Success criteria

- [ ] `python research/figures/gen_all.py` produces all 8 PNGs with no errors
- [ ] `pdflatex research/contextforge_v2.tex` compiles with no undefined references or symbol errors
- [ ] All 14 sections present with no TBD/placeholder text
- [ ] `docs/RESEARCH.md` updated to point at `research/` paths
- [ ] README documentation table updated
- [ ] `.gitignore` audited; no secrets or generated binaries tracked
