"""
Token growth comparison: CLAUDE.md (unbounded linear) vs ContextForge load_context (bounded).
Formula: CLAUDE.md ~ decisions * 150 tokens; CF always <= 1500 tokens (top_k=10).
"""
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

OUT = Path(__file__).parent / "fig_token_savings.png"


def main():
    decisions  = np.array([0, 10, 20, 50, 100, 150, 200, 300])
    claude_md  = decisions * 150
    cf_tokens  = np.minimum(decisions * 150 * 0.05 + 200, 1500)

    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(decisions, claude_md, "o-", color="#c0392b", lw=2, ms=5,
            label="CLAUDE.md \u2014 paste all context")
    ax.plot(decisions, cf_tokens, "s-", color="#27ae60", lw=2, ms=5,
            label="ContextForge \u2014 load_context(top_k=10)")

    ax.axhline(1500, color="#27ae60", ls=":", lw=1.2, alpha=0.6)
    ax.text(305, 1550, "Token budget cap\n(B = 1500)",
            fontsize=7.5, color="#27ae60", va="bottom", ha="right")

    # Savings annotations at key decision counts
    for d, cld, cf in [(100, 15000, 950), (200, 30000, 1500)]:
        pct = int((1 - cf / cld) * 100)
        ax.annotate(
            f"{pct}% saved",
            xy=(d, cf), xytext=(d - 40, cf + cld * 0.18),
            arrowprops=dict(arrowstyle="->", color="#555"),
            fontsize=8, color="#555",
        )

    ax.set_xlabel("Decisions stored", fontsize=10)
    ax.set_ylabel("Tokens per session", fontsize=10)
    ax.set_title("Token Cost Scaling: CLAUDE.md vs. ContextForge load_context", fontsize=11)
    ax.legend(fontsize=9)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda val, _: f"{int(val):,}"))
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
