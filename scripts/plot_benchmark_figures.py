"""
Generate publication-style benchmark comparison figures.

Produces PDF/PNG figures comparing Kimi, DeepSeek, OpenAI, and MiniMax across
topology scales (xs through large) for the following metrics:
  1. Detection Rate
  2. Device Localization Rate
  3. Interface Localization Rate
  4. Composite Avg Score
    5. Avg Diagnosis Time (seconds)
    6. Avg Tool Calls
    7. Input Tokens
    8. Output Tokens

Usage:
    python3 scripts/plot_benchmark_figures.py [--outdir scenario_results/figures]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ---------------------------------------------------------------------------
# Raw benchmark data
# ---------------------------------------------------------------------------

DATA = {
    "Kimi": {
        "xs":     dict(detection=85.7,  device=83.3, interface=28.6, avg_score=0.771, avg_time=103.3,  tool_calls=22.64, input_tokens=164098, output_tokens=3748),
        "small":  dict(detection=93.3,  device=91.7, interface=85.7, avg_score=0.907, avg_time=73.3,   tool_calls=17.47, input_tokens=156370, output_tokens=3380),
        "medium": dict(detection=60.7,  device=45.8, interface=35.7, avg_score=0.550, avg_time=140.4,  tool_calls=23.25, input_tokens=259499, output_tokens=4685),
        "large":  dict(detection=61.5,  device=33.3, interface=14.3, avg_score=0.342, avg_time=224.3,  tool_calls=32.98, input_tokens=516972, output_tokens=8050),
    },
    "DeepSeek": {
        "xs":     dict(detection=100.0, device=58.3, interface=85.7, avg_score=0.700, avg_time=161.5,  tool_calls=21.21, input_tokens=153745, output_tokens=3511),
        "small":  dict(detection=86.7,  device=75.0, interface=42.9, avg_score=0.773, avg_time=120.5,  tool_calls=18.47, input_tokens=165323, output_tokens=3573),
        "medium": dict(detection=89.3,  device=95.8, interface=57.1, avg_score=0.771, avg_time=120.6,  tool_calls=17.79, input_tokens=198511, output_tokens=3584),
        "large":  dict(detection=80.8,  device=37.5, interface=14.3, avg_score=0.300, avg_time=162.6,  tool_calls=16.65, input_tokens=261048, output_tokens=4065),
    },
    "OpenAI": {
        "xs":     dict(detection=100.0, device=33.3, interface=42.9, avg_score=0.486, avg_time=97.0,   tool_calls=14.50, input_tokens=105085, output_tokens=2400),
        "small":  dict(detection=100.0, device=83.3, interface=14.3, avg_score=0.733, avg_time=53.5,   tool_calls=14.10, input_tokens=126230, output_tokens=2728),
        "medium": dict(detection=100.0, device=91.7, interface=42.9, avg_score=0.829, avg_time=59.5,   tool_calls=13.80, input_tokens=154025, output_tokens=2781),
        "large":  dict(detection=80.8,  device=45.8, interface=0.0,  avg_score=0.323, avg_time=48.6,   tool_calls=12.40, input_tokens=194369, output_tokens=3027),
    },
    "MiniMax": {
        "xs":     dict(detection=25.0,  device=16.7, interface=14.3, avg_score=0.133, avg_time=68.1,   tool_calls=12.00, input_tokens=86967,  output_tokens=1986),
        "small":  dict(detection=100.0, device=83.3, interface=42.9, avg_score=0.733, avg_time=95.2,   tool_calls=15.50, input_tokens=138764, output_tokens=2999),
        "medium": dict(detection=95.8,  device=83.3, interface=35.7, avg_score=0.733, avg_time=82.3,   tool_calls=14.60, input_tokens=162954, output_tokens=2942),
        "large":  dict(detection=93.8,  device=31.2, interface=0.0,  avg_score=0.238, avg_time=104.3,  tool_calls=13.90, input_tokens=217882, output_tokens=3393),
    },
}

SCALES = ["xs", "small", "medium", "large"]
SCALE_LABELS = ["XS\n(14)", "Small\n(15)", "Medium\n(28)", "Large\n(52)"]
VENDORS = ["Kimi", "DeepSeek", "OpenAI", "MiniMax"]

# ---------------------------------------------------------------------------
# SIGCOMM/NSDI visual style
# ---------------------------------------------------------------------------

# Slightly wider to accommodate 4 grouped bars
FIG_W = 4.5   # inches
FIG_H = 3.0   # inches (extra height for below-axis legend)

# Colour-blind-friendly palette (Wong 2011 + extension)
COLORS = {
    "Kimi":     "#0072B2",   # blue
    "DeepSeek": "#D55E00",   # vermillion
    "OpenAI":   "#009E73",   # green
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
        for bar, v in zip(bars, vals):
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
            edgecolor="gray", linewidth=0.5, label=v
        )
        for v in VENDORS
    ]
    ax.legend(handles=handles, loc="upper center",
              bbox_to_anchor=(0.5, -0.28), ncol=4, frameon=False,
              columnspacing=0.8, handlelength=1.2)


# ---------------------------------------------------------------------------
# Individual figure generators
# ---------------------------------------------------------------------------

def _fig_detection(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["detection"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals,
                 ylabel="Detection Rate (%)",
                 ylim=(0, 120),
                 yticks=[0, 20, 40, 60, 80, 100])
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_detection.pdf"
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
                 ylim=(0, 290),
                 yticks=[0, 50, 100, 150, 200, 250],
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
                 ylim=(0, 42),
                 yticks=[0, 10, 20, 30, 40],
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
                 ylim=(0, 620),
                 yticks=[0, 100, 200, 300, 400, 500, 600],
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
                 ylim=(0, 9500),
                 yticks=[0, 2000, 4000, 6000, 8000],
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
        ("detection",  "Detection Rate (%)",              (0, 115), [0,20,40,60,80,100], True),
        ("device",     "Device Loc. Rate (%)",            (0, 115), [0,20,40,60,80,100], True),
        ("interface",  "Interface Loc. Rate (%)",         (0, 115), [0,20,40,60,80,100], True),
        ("avg_score",  "Composite Score",                 (0, 1.15),[0,.2,.4,.6,.8,1.0], False),
        ("avg_time",   "Avg Diagnosis Time (s)",          (0, 280), [0,50,100,150,200,250], False),
    ]

    x = np.arange(len(SCALES))
    bar_w = 0.18
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
                               edgecolor="gray", linewidth=0.5, label=v)
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
    import matplotlib.ticker  # noqa: ensure available in closures

    figs = [
        _fig_detection(outdir),
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
    print(f"{'Vendor':<10} {'Scale':<8} {'Detection':>10} {'Device Loc':>11} "
          f"{'Intf Loc':>10} {'Avg Score':>10} {'Avg Time(s)':>12}")
    print("-" * 80)
    for vendor in VENDORS:
        for scale in SCALES:
            d = DATA[vendor][scale]
            print(f"{vendor:<10} {scale:<8} {d['detection']:>9.1f}% "
                  f"{d['device']:>10.1f}% {d['interface']:>9.1f}% "
                  f"{d['avg_score']:>10.3f} {d['avg_time']:>11.1f}s")
        print()


if __name__ == "__main__":
    main()
