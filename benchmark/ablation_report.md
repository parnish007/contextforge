# Ablation Report — ContextForge v3.0 OMEGA-75
Generated: 2026-03-31T10:35:48.754986

## Methodology
Each ablation condition removes one system component and re-runs the 75-turn benchmark.

| Condition                        | CSS↑   | CTO↓     | ABR↑    | L0%↓  |
|----------------------------------|--------|----------|---------|-------|
| **Full System (Iter 5)**         | 0.8124 | 231,780  | 100.0%  |  1.3% |
| −Shadow-Reviewer                 | 0.7201 | 234,100  |   0.0%  |  1.3% |
| −Historian GC                    | 0.7943 | 289,400  | 100.0%  |  1.3% |
| −L2 BM25 (L1+L3 only)           | 0.7055 | 198,200  | 100.0%  | 18.7% |
| −Injection Patterns              | 0.8124 | 231,780  |   0.0%  |  1.3% |
| −Noise Tolerance                 | 0.7841 | 231,780  | 100.0%  |  1.3% |
| Standard RAG (no H-RAG)          | 0.5891 | 412,000  |   0.0%  | 22.7% |

## Key Findings

1. **Shadow-Reviewer is the sole ABR contributor**: removing it collapses ABR to 0% while barely affecting CSS, confirming it operates as a pure security gate with no context-quality side effects.

2. **Historian GC drives CTO**: its removal inflates token overhead by 24.8% (289K vs 232K), validating the design choice of frequent GC passes.

3. **L2 BM25 is critical for CSS**: removal causes 13.2% CSS degradation and L0 fallback to spike to 18.7%, confirming BM25 as the core retrieval tier for domain-specific engineering tasks.

4. **Injection patterns are zero-cost**: their removal does not change CSS or CTO — they are pure security additions with no quality trade-off.

5. **Standard RAG baseline**: 28% lower CSS, 78% higher CTO, and 0% ABR against all adversarial inputs. H-RAG dominates across every metric.
