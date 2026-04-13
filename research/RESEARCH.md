# ContextForge — Research Assets

> **Author:** Trilochan Sharma — Independent Researcher · [parnish007](https://github.com/parnish007)

← [README](../README.md) · [Engineering Reference](../docs/ENGINEERING_REFERENCE.md) · [Benchmark Results](../docs/BENCHMARK_RESULTS.md)

This directory contains all research assets for ContextForge: the v2 paper source,
figure generation scripts, benchmark result archives, and the v1 paper archive.

---

## Paper

| File | Description |
|------|-------------|
| [contextforge_v2.tex](contextforge_v2.tex) | v2 paper — full architecture + MCP server (April 2026) |
| [refs.bib](refs.bib) | Extended bibliography (15 citations) |
| [v1_archive/contextforge_v1.tex](v1_archive/contextforge_v1.tex) | v1 paper archive (March 2026) |

### Compile the paper

```bash
cd research
pdflatex contextforge_v2.tex
bibtex contextforge_v2
pdflatex contextforge_v2.tex
pdflatex contextforge_v2.tex   # second pass resolves citations
```

Requires a standard LaTeX distribution (texlive, MiKTeX, or MacTeX).

---

## Figures

All 9 figures are generated from Python scripts using matplotlib only.

```bash
# Generate all 9 PNGs (from project root):
python research/figures/gen_all.py
```

| Script | Figure | Data source |
|--------|--------|-------------|
| [fig_entropy_gate.py](figures/fig_entropy_gate.py) | Shannon entropy distributions | Hardcoded from engine.py measurements |
| [fig_calibration_sweep.py](figures/fig_calibration_sweep.py) | FPR/FNR/F1 sweep | suite_08_fpr_calibration.json |
| [fig_temporal_correlator.py](figures/fig_temporal_correlator.py) | Slow-drip gradient separation | suite_07_temporal_correlator.json |
| [fig_voh_tiers.py](figures/fig_voh_tiers.py) | VOH tiered threshold diagram | Computed (H*=3.5, discount=0.20) |
| [fig_ablation.py](figures/fig_ablation.py) | Ablation study bar chart | ablation_report.md hardcoded values |
| [fig_token_savings.py](figures/fig_token_savings.py) | Token cost scaling | Computed formula |
| [fig_system_architecture.py](figures/fig_system_architecture.py) | Full system block diagram | Drawn (matplotlib patches) |
| [fig_mcp_dataflow.py](figures/fig_mcp_dataflow.py) | 22 tools → pillar mapping | Drawn (matplotlib) |
| [fig_radar.py](figures/fig_radar.py) | Six-pillar spider chart | Hardcoded benchmark values |

All PNG outputs are committed at 300 DPI for paper reproducibility.

---

## Benchmark Results

All JSON outputs from the benchmark suites are archived here.
The source suites live in `benchmark/` and can be re-run at any time to regenerate.

| File | Suite | Tests | Key result |
|------|-------|-------|-----------|
| [suite_06_external_baseline.json](benchmark_results/suite_06_external_baseline.json) | External RAG baseline | — | Multi-corpus retrieval comparison |
| [suite_07_temporal_correlator.json](benchmark_results/suite_07_temporal_correlator.json) | Temporal correlator | 15+15 sequences | 100% detection, 0% FP |
| [suite_08_fpr_calibration.json](benchmark_results/suite_08_fpr_calibration.json) | FPR calibration sweep | 11 thresholds | F1=1.0 at H*=3.5 (unique) |
| [suite_09_voh_multiprocess.json](benchmark_results/suite_09_voh_multiprocess.json) | VOH multiprocess | 500 writers | 0 lost writes, HMAC verified |
| [iter_06_adversarial_boundary.json](benchmark_results/iter_06_adversarial_boundary.json) | Adversarial boundary | 77 cases | 100% ABR |
| [final_combined_results.json](benchmark_results/final_combined_results.json) | All OMEGA iterations | 450 tests | Iter 5 final: CSS=0.8124 |
| [ablation_report.md](benchmark_results/ablation_report.md) | Ablation study | 7 conditions | Shadow-Reviewer sole ABR source |

### Re-run all suites

```bash
python -X utf8 benchmark/suite_07_temporal_correlator.py
python -X utf8 benchmark/suite_08_fpr_calibration.py
python -X utf8 benchmark/suite_09_voh_multiprocess.py
python -X utf8 benchmark/test_v5/iter_06_adversarial_boundary.py
python -X utf8 benchmark/test_v5/run_all.py     # 375 OMEGA tests
```

---

## Key Numbers

| Metric | Stateless RAG | ContextForge | Δ |
|--------|--------------|--------------|---|
| CSS (Context Survival Score) | 0.589 | **0.812** | +37.8% |
| CTO (token overhead) | 412,000 | **231,780** | −43.7% |
| Adversarial Block Rate | 0% | **85.0%** | +85.0 pp |
| L0 Fallback Rate | 22.7% | **1.3%** | −94.3% |
| Slow-drip Detection | 0% | **100%** | +100 pp |
| F1 at H*=3.5 | — | **1.0** | unique maximum |
| Failover Latency | 480 ms | **149 ms** | −68.9% |
| Weighted Safety Index Φ | — | **80.7%** | — |
| Tests passing | — | **527/527** | 100% |
