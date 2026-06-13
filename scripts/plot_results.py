"""
Generate SIGCOMM/NSDI-style benchmark comparison figures.

Produces eight separate PDF/PNG figures comparing Kimi, DeepSeek, MiniMax, and
OpenAI across topology scales (xs -> large) for the following metrics:
  1. Verdict F1-score
  2. Device Localization Rate
  3. Interface Localization Rate
  4. Composite Avg Score
  5. Avg Diagnosis Time (seconds)
  6. Avg Tool Calls
  7. Avg Input Tokens
  8. Avg Output Tokens

Usage:
    python3 scripts/plot_results.py [--outdir scenario_results/figures]

Requires:
    matplotlib
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import matplotlib.patches as mpatches
import matplotlib.ticker
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# ---------------------------------------------------------------------------
# Raw benchmark data
# ---------------------------------------------------------------------------

DATA = {
    "Kimi": {
        "xs":     dict(verdict_f1=76.2, device=66.7, interface=71.4, avg_score=0.643, avg_time=272.0, tool_calls=36.5, input_tokens=367588.5, output_tokens=5750.4),
        "small":  dict(verdict_f1=85.7, device=75.0, interface=57.1, avg_score=0.767, avg_time=145.1, tool_calls=24.7, input_tokens=258411.6, output_tokens=3042.8),
        "medium": dict(verdict_f1=88.4, device=79.2, interface=78.6, avg_score=0.821, avg_time=255.9, tool_calls=29.4, input_tokens=370888.8, output_tokens=4406.6),
        "large":  dict(verdict_f1=58.0, device=20.8, interface=21.4, avg_score=0.250, avg_time=576.8, tool_calls=47.3, input_tokens=1029419.1, output_tokens=11137.1),
    },
    "DeepSeek": {
        "xs":     dict(verdict_f1=100.0, device=83.3, interface=57.1, avg_score=0.786, avg_time=83.1,  tool_calls=24.9, input_tokens=247552.4, output_tokens=2857.2),
        "small":  dict(verdict_f1=100.0, device=91.7, interface=57.1, avg_score=0.867, avg_time=79.2,  tool_calls=18.4, input_tokens=223539.9, output_tokens=2535.7),
        "medium": dict(verdict_f1=100.0, device=91.7, interface=50.0, avg_score=0.821, avg_time=77.9,  tool_calls=16.6, input_tokens=229409.1, output_tokens=2512.8),
        "large":  dict(verdict_f1=91.7,  device=45.8, interface=39.3, avg_score=0.394, avg_time=106.2, tool_calls=22.7, input_tokens=564170.5, output_tokens=3562.0),
    },
    "MiniMax": {
        "xs":     dict(verdict_f1=80.0, device=50.0, interface=71.4, avg_score=0.607, avg_time=214.3, tool_calls=26.4, input_tokens=133292.1, output_tokens=8070.7),
        "small":  dict(verdict_f1=73.7, device=50.0, interface=71.4, avg_score=0.600, avg_time=215.8, tool_calls=21.3, input_tokens=149411.7, output_tokens=7543.3),
        "medium": dict(verdict_f1=82.9, device=70.8, interface=50.0, avg_score=0.714, avg_time=180.3, tool_calls=17.9, input_tokens=146968.4, output_tokens=6333.2),
        "large":  dict(verdict_f1=35.5, device=16.7, interface=7.1,  avg_score=0.163, avg_time=176.8, tool_calls=15.0, input_tokens=167732.9, output_tokens=6139.1),
    },
    "OpenAI": {
        "xs":     dict(verdict_f1=95.7, device=83.3, interface=57.1, avg_score=0.750, avg_time=71.9, tool_calls=17.9, input_tokens=85444.4, output_tokens=1798.9),
        "small":  dict(verdict_f1=100.0, device=83.3, interface=71.4, avg_score=0.800, avg_time=70.0, tool_calls=14.6, input_tokens=80932.9, output_tokens=1757.9),
        "medium": dict(verdict_f1=95.7, device=87.5, interface=42.9, avg_score=0.786, avg_time=69.0, tool_calls=14.0, input_tokens=92580.0, output_tokens=1777.9),
        "large":  dict(verdict_f1=66.7, device=43.8, interface=10.7, avg_score=0.327, avg_time=80.5, tool_calls=15.3, input_tokens=147606.6, output_tokens=2201.3),
    },
}

SCALES = ["xs", "small", "medium", "large"]
SCALE_LABELS = ["XS\n(14)", "Small\n(15)", "Medium\n(28)", "Large\n(52)"]
VENDORS = ["Kimi", "DeepSeek", "OpenAI", "MiniMax"]
LEGEND_LABELS = {
    "Kimi": "Kimi K2.6",
    "DeepSeek": "DeepSeek V4 Pro",
    "OpenAI": "OpenAI GPT-5.5",
    "MiniMax": "MiniMax M3",
}

# ---------------------------------------------------------------------------
# SIGCOMM/NSDI visual style
# ---------------------------------------------------------------------------

# Slightly wider than a single-column plot to keep grouped labels readable.
FIG_W = 4.8   # inches
FIG_H = 3.0   # inches (extra height for below-axis legend)

# Colour-blind-friendly palette (Wong 2011 + extension)
COLORS = {
    "Kimi":     "#0072B2",   # blue
    "DeepSeek": "#D55E00",   # vermillion
    "OpenAI":   "#009E73",   # bluish green
    "MiniMax":  "#CC79A7",   # pink/purple
}
HATCHES = {
    "Kimi":     "\\\\",
    "DeepSeek": "////",
    "OpenAI":   "..",
    "MiniMax":  "xx",
}

FONT_FAMILY = "DejaVu Sans"
LABEL_SIZE  = 8
TICK_SIZE   = 7.5
LEGEND_SIZE = 7.5
TITLE_SIZE  = 8.5

def _apply_base_style() -> None:
    plt.rcParams.update({
        "font.family":        FONT_FAMILY,
        "font.size":          LABEL_SIZE,
        "axes.labelsize":     LABEL_SIZE,
        "axes.titlesize":     TITLE_SIZE,
        "xtick.labelsize":    TICK_SIZE,
        "ytick.labelsize":    TICK_SIZE,
        "legend.fontsize":    LEGEND_SIZE,
        "axes.linewidth":     0.8,
        "xtick.major.width":  0.6,
        "ytick.major.width":  0.6,
        "xtick.major.size":   2.5,
        "ytick.major.size":   2.5,
        "axes.grid":          True,
        "grid.linestyle":     "--",
        "grid.linewidth":     0.4,
        "grid.alpha":         0.5,
        "axes.axisbelow":     True,
        "legend.framealpha":  0.9,
        "legend.edgecolor":   "0.7",
        "legend.borderpad":   0.3,
        "legend.handlelength": 1.5,
        "legend.handletextpad": 0.4,
        "figure.dpi":         300,
        "savefig.dpi":        300,
        "savefig.bbox":       "tight",
        "savefig.pad_inches": 0.12,
    })


def _grouped_bar(ax, values_dict: dict, ylabel: str, ylim: tuple,
                 yticks=None, pct: bool = True, val_fmt: str = None) -> None:
    """
    Draw a grouped bar chart on *ax*.

    values_dict: {vendor: [v_xs, v_small, v_medium, v_large]}
    """
    n = len(SCALES)
    n_vendors = len(VENDORS)
    bar_w = 0.20
    group_w = bar_w * n_vendors
    offsets = np.linspace(-(group_w - bar_w) / 2, (group_w - bar_w) / 2, n_vendors)
    span = ylim[1] - ylim[0]

    x = np.arange(n)

    # Auto-choose label format
    if val_fmt is None:
        val_fmt = "{:.0f}" if pct else "{:.0f}"

    for idx, vendor in enumerate(VENDORS):
        vals = values_dict[vendor]
        bars = ax.bar(
            x + offsets[idx],
            vals,
            width=bar_w,
            color=COLORS[vendor],
            hatch=HATCHES[vendor],
            edgecolor="white",
            linewidth=0.5,
            label=vendor,
            zorder=3,
        )
        # Value labels above each bar
        for bar, v in zip(bars, vals, strict=False):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                v + span * 0.013,
                val_fmt.format(v),
                ha="center", va="bottom", fontsize=5.5, zorder=4,
            )

    ax.set_xticks(x)
    ax.set_xticklabels(SCALE_LABELS, linespacing=1.1)
    ax.set_xlabel("Topology Scale (# scenarios)", labelpad=3)
    ax.set_ylabel(ylabel, labelpad=3)
    ax.set_ylim(*ylim)
    if yticks is not None:
        ax.set_yticks(yticks)
    if pct:
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%")
        )

    # Remove top/right spines
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Legend below the axes
    handles = [
        mpatches.Patch(
            facecolor=COLORS[v], hatch=HATCHES[v],
            edgecolor="gray", linewidth=0.5, label=LEGEND_LABELS[v]
        )
        for v in VENDORS
    ]
    ax.legend(handles=handles, loc="upper center",
              bbox_to_anchor=(0.5, -0.28), ncol=4, frameon=False,
              columnspacing=0.8, handlelength=1.2)


# ---------------------------------------------------------------------------
# Individual figure generators
# ---------------------------------------------------------------------------

def _fig_verdict_f1(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["verdict_f1"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Verdict F1-score (%)",
                 ylim=(0, 120),
                 yticks=[0, 20, 40, 60, 80, 100])
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_verdict_f1.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_device_loc(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["device"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Device Localization Rate (%)",
                 ylim=(0, 120),
                 yticks=[0, 20, 40, 60, 80, 100])
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_device_loc.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_intf_loc(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["interface"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Interface Localization Rate (%)",
                 ylim=(0, 120),
                 yticks=[0, 20, 40, 60, 80, 100])
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_intf_loc.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_avg_score(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["avg_score"] * 100 for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Composite Score (%)",
                 ylim=(0, 115),
                 yticks=[0, 20, 40, 60, 80, 100],
                 val_fmt="{:.1f}")
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_avg_score.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_avg_time(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["avg_time"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Avg Diagnosis Time (s)",
                 ylim=(0, 650),
                 yticks=[0, 100, 200, 300, 400, 500, 600],
                 pct=False)
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_avg_time.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_tool_calls(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["tool_calls"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Avg Tool Calls / Case",
                 ylim=(0, 55),
                 yticks=[0, 10, 20, 30, 40, 50],
                 pct=False,
                 val_fmt="{:.1f}")
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_tool_calls.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_input_tokens(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    # Convert to thousands for readability
    vals = {v: [DATA[v][s]["input_tokens"] / 1000 for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Input Tokens / Case (K)",
                 ylim=(0, 1120),
                 yticks=[0, 200, 400, 600, 800, 1000],
                 pct=False,
                 val_fmt="{:.0f}K")
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_input_tokens.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


def _fig_output_tokens(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["output_tokens"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Output Tokens / Case",
                 ylim=(0, 12500),
                 yticks=[0, 2500, 5000, 7500, 10000, 12500],
                 pct=False,
                 val_fmt="{:.0f}")
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_output_tokens.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Combined 2×3 overview figure
# ---------------------------------------------------------------------------

def _fig_combined(outdir: Path) -> Path:
    _apply_base_style()
    fig, axes = plt.subplots(2, 3, figsize=(7.0, 4.0))

    configs = [
        ("verdict_f1", "Verdict F1-score (%)",            (0, 115), [0,20,40,60,80,100], True),
        ("device",     "Device Loc. Rate (%)",            (0, 115), [0,20,40,60,80,100], True),
        ("interface",  "Interface Loc. Rate (%)",         (0, 115), [0,20,40,60,80,100], True),
        ("avg_score",  "Composite Score",                 (0, 1.15),[0,.2,.4,.6,.8,1.0], False),
        ("avg_time",   "Avg Diagnosis Time (s)",          (0, 650), [0,100,200,300,400,500,600], False),
    ]

    x = np.arange(len(SCALES))
    bar_w = 0.22
    group_w = bar_w * len(VENDORS)
    offsets = np.linspace(-(group_w - bar_w) / 2, (group_w - bar_w) / 2, len(VENDORS))

    for i, (key, ylabel, ylim, yticks, is_pct) in enumerate(configs):
        ax = axes[i // 3][i % 3]
        for idx, vendor in enumerate(VENDORS):
            raw = [DATA[vendor][s][key] for s in SCALES]
            ax.bar(x + offsets[idx], raw, width=bar_w,
                   color=COLORS[vendor], hatch=HATCHES[vendor],
                   edgecolor="white", linewidth=0.5, label=vendor, zorder=3)
        ax.set_xticks(x)
        ax.set_xticklabels(["XS", "Sm", "Md", "Lg"])
        ax.set_ylim(*ylim)
        ax.set_yticks(yticks)
        ax.set_ylabel(ylabel, labelpad=2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, linestyle="--", linewidth=0.4, alpha=0.5, axis="y", zorder=0)
        ax.set_axisbelow(True)
        if is_pct:
            ax.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%")
            )
        if i == 0:
            handles = [
                mpatches.Patch(facecolor=COLORS[v], hatch=HATCHES[v],
                               edgecolor="gray", linewidth=0.5,
                               label=LEGEND_LABELS[v])
                for v in VENDORS
            ]
            ax.legend(handles=handles, loc="upper right", fontsize=6.0,
                      ncol=2, frameon=True)

    # Hide the unused 6th subplot
    axes[1][2].set_visible(False)

    fig.suptitle("NetOpsBench: Kimi / DeepSeek / OpenAI / MiniMax across Topology Scales",
                 fontsize=9, y=1.01)
    fig.tight_layout()
    out = outdir / "fig_combined_overview.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="scenario_results/figures",
                        help="Output directory for figures")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    _apply_base_style()
    figs = [
        _fig_verdict_f1(outdir),
        _fig_device_loc(outdir),
        _fig_intf_loc(outdir),
        _fig_avg_score(outdir),
        _fig_avg_time(outdir),
        _fig_tool_calls(outdir),
        _fig_input_tokens(outdir),
        _fig_output_tokens(outdir),
    ]

    print(f"Generated {len(figs)} figure(s) in {outdir}/")
    for f in figs:
        print(f"  {f.name}  (+.png)")

    # Print summary table
    print()
    print("=" * 80)
    print(f"{'Vendor':<10} {'Scale':<8} {'Verdict F1':>10} {'Device Loc':>11} "
          f"{'Intf Loc':>10} {'Avg Score':>10} {'Avg Time(s)':>12}")
    print("-" * 80)
    for vendor in VENDORS:
        for scale in SCALES:
            d = DATA[vendor][scale]
            print(f"{vendor:<10} {scale:<8} {d['verdict_f1']:>9.1f}% "
                  f"{d['device']:>10.1f}% {d['interface']:>9.1f}% "
                  f"{d['avg_score']:>10.3f} {d['avg_time']:>11.1f}s")
        print()


if __name__ == "__main__":
    main()
