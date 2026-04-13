"""
Tiered clearance diagram: shows H* standard (3.5) and H*_VOH (4.375)
as vertical thresholds over the benign distribution, illustrating how VOH
elevates the threshold without widening adversarial exposure.
"""
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from pathlib import Path

OUT = Path(__file__).parent / "fig_voh_tiers.png"


def main():
    x = np.linspace(0.5, 7.0, 600)

    # Benign unauthenticated distribution
    mu_b, sig_b = 2.74, 0.43
    pdf_b = (1 / (sig_b * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mu_b) / sig_b) ** 2)

    # VOH-eligible authenticated traffic (slightly higher entropy: technical content)
    mu_v, sig_v = 3.70, 0.30
    pdf_v = (1 / (sig_v * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mu_v) / sig_v) ** 2) * 0.5

    fig, ax = plt.subplots(figsize=(7, 3.8))

    ax.fill_between(x, pdf_b, alpha=0.25, color="#27ae60")
    ax.plot(x, pdf_b, "#27ae60", lw=2, label="Unauthenticated traffic\n(external writes)")
    ax.fill_between(x, pdf_v, alpha=0.25, color="#8e44ad")
    ax.plot(x, pdf_v, "#8e44ad", lw=2, ls="-.", label="VOH-authenticated traffic\n(internal system events)")

    ax.axvline(3.5,   color="#c0392b", lw=2.0, ls="--")
    ax.axvline(4.375, color="#8e44ad", lw=2.0, ls=":")

    # Region labels
    ax.annotate("", xy=(3.5, 0.55), xytext=(4.375, 0.55),
                arrowprops=dict(arrowstyle="<->", color="#555"))
    ax.text(3.93, 0.58, "VOH buffer\n(0.875 bits)", ha="center", fontsize=8, color="#555")

    ax.text(3.5  + 0.05, 0.72, r"$H^*=3.5$" + "\nstandard",         fontsize=8, color="#c0392b", va="top")
    ax.text(4.375 + 0.05, 0.55, r"$H^*_\mathrm{VOH}=4.375$" + "\nVOH threshold", fontsize=8, color="#8e44ad", va="top")

    # Blocked region shading
    ax.axvspan(4.375, 7.0, alpha=0.06, color="#c0392b", label="Blocked even with VOH")
    ax.axvspan(3.5,   4.375, alpha=0.06, color="#8e44ad", label="Admitted with VOH only")

    ax.set_xlabel("Shannon Entropy $H$ (bits)", fontsize=10)
    ax.set_ylabel("Probability Density", fontsize=10)
    ax.set_title("Tiered Clearance Logic \u2014 Standard vs. VOH Thresholds", fontsize=11)
    ax.legend(fontsize=8, loc="upper right")
    ax.set_xlim(0.5, 7.0)
    ax.set_ylim(0)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
