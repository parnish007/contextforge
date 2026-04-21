"""
Weighted Composite Safety Index Φ — weight sensitivity heatmap.
Sweeps w_abr ∈ [0.1, 0.9] and w_tnr ∈ [0.1, 0.9] (w_lat = 1 − w_abr − w_tnr).
Cells where w_lat < 0 are masked (infeasible region).

ContextForge values used:
  ABR        = 85.0 %  (adversarial block rate)
  lat_norm   = 68.9 %  (1 − 149/480; normalised latency gain)
  TNR        = 87.4 %  (token noise reduction)
"""
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from pathlib import Path

OUT = Path(__file__).parent / "fig_weight_sensitivity.png"

ABR     = 0.850
LAT     = 0.689
TNR     = 0.874

STEP = 0.05
W = np.arange(0.05, 0.96, STEP)   # w_abr and w_tnr axes

W_ABR, W_TNR = np.meshgrid(W, W)
W_LAT  = 1.0 - W_ABR - W_TNR

PHI  = W_ABR * ABR + W_LAT * LAT + W_TNR * TNR
MASK = W_LAT < 0.0

PHI_masked = np.where(MASK, np.nan, PHI)

# Named presets: (w_abr, w_tnr)
PRESETS = {
    "ide_workflow\n(0.5, 0.3, 0.2)":           (0.50, 0.20),
    "backend_auto\n(0.3, 0.4, 0.3)":           (0.30, 0.30),
    "research_pipe\n(0.4, 0.2, 0.4)":          (0.40, 0.40),
}
PRESET_MARKERS = ["*", "^", "D"]
PRESET_COLOURS = ["#f39c12", "#8e44ad", "#e74c3c"]


def main():
    fig, ax = plt.subplots(figsize=(7, 5.5))

    # Colour map centred on the ide_workflow Φ value
    phi_center = 0.50 * ABR + 0.30 * LAT + 0.20 * TNR   # ≈ 0.807
    cmap   = plt.cm.RdYlGn
    norm   = mcolors.TwoSlopeNorm(vmin=0.60, vcenter=phi_center, vmax=0.93)

    im = ax.pcolormesh(
        W, W, PHI_masked,
        cmap=cmap, norm=norm, shading="auto",
    )

    # Infeasible region (grey hatching)
    ax.pcolormesh(
        W, W, np.where(MASK, 0.0, np.nan),
        cmap=mcolors.ListedColormap(["#bdc3c7"]),
        shading="auto", alpha=0.7,
    )
    # Diagonal boundary line (w_abr + w_tnr = 1)
    boundary = W
    ax.plot(boundary, 1.0 - boundary, "k--", lw=1.2, alpha=0.5, label="$w_{\\mathrm{abr}}+w_{\\mathrm{tnr}}=1$")

    # Preset markers
    for (label, (w_abr, w_tnr)), marker, colour in zip(
        PRESETS.items(), PRESET_MARKERS, PRESET_COLOURS
    ):
        phi_val = w_abr * ABR + (1 - w_abr - w_tnr) * LAT + w_tnr * TNR
        ax.scatter(
            w_abr, w_tnr,
            marker=marker, s=140, color=colour,
            edgecolors="black", linewidths=0.8, zorder=5,
            label=f"{label}  Φ={phi_val:.1%}",
        )

    # Colourbar
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Weighted Safety Index Φ", fontsize=9)
    cbar.ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))

    ax.set_xlabel("$w_{\\mathrm{abr}}$ (adversarial weight)", fontsize=10)
    ax.set_ylabel("$w_{\\mathrm{tnr}}$ (token noise weight)", fontsize=10)
    ax.set_title(
        "Weight Sensitivity of Composite Safety Index Φ\n"
        r"$\Phi = w_{\mathrm{abr}}\cdot\mathrm{ABR}"
        r" + w_{\mathrm{lat}}\cdot\Delta_{\mathrm{lat}}"
        r" + w_{\mathrm{tnr}}\cdot\mathrm{TNR}$",
        fontsize=10.5,
    )

    ax.set_xlim(W[0] - STEP / 2, W[-1] + STEP / 2)
    ax.set_ylim(W[0] - STEP / 2, W[-1] + STEP / 2)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1f}"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1f}"))

    ax.legend(
        fontsize=7.5, loc="upper right",
        framealpha=0.85, borderpad=0.6,
        handlelength=1.2,
    )

    # Infeasible label
    ax.text(
        0.78, 0.15, "Infeasible\n($w_{\\mathrm{lat}}<0$)",
        transform=ax.transAxes, fontsize=8,
        color="#7f8c8d", ha="center", va="center",
        rotation=0,
    )

    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()