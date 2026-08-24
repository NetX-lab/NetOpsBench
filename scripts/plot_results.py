"""
Generate SIGCOMM/NSDI-style benchmark result figures.

Produces eight legacy cross-model PDF/PNG figures comparing Kimi, DeepSeek,
MiniMax, and OpenAI across topology scales (xs -> large) for the following
metrics:
  1. Verdict F1-score
  2. Device Localization Rate
  3. Interface Localization Rate
  4. Composite Avg Score
  5. Avg Diagnosis Time (seconds)
  6. Avg Tool Calls
  7. Avg Input Tokens
  8. Avg Output Tokens

It also produces figures for the separately versioned NetOpsBench 0.2
DeepSeek release rerun, including two compact README figures and two detailed
Results-page figures. The snapshots
are intentionally not mixed into one cross-model chart because they use
different benchmark contracts.

Usage:
    python3 scripts/plot_results.py [--outdir scenario_results/figures]
    python3 scripts/plot_results.py \
        --readme-assets-dir docs/public/assets/benchmark
    python3 scripts/plot_results.py \
        --advisor-report-dir scenario_results/advisor_report \
        --advisor-manifest /path/to/advisor-inputs.json

Requires:
    matplotlib
"""

from __future__ import annotations

import argparse
import csv
import json
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
        "xs": dict(
            verdict_f1=76.2,
            device=66.7,
            interface=71.4,
            avg_score=0.643,
            avg_time=272.0,
            tool_calls=36.5,
            input_tokens=367588.5,
            output_tokens=5750.4,
        ),
        "small": dict(
            verdict_f1=85.7,
            device=75.0,
            interface=57.1,
            avg_score=0.767,
            avg_time=145.1,
            tool_calls=24.7,
            input_tokens=258411.6,
            output_tokens=3042.8,
        ),
        "medium": dict(
            verdict_f1=88.4,
            device=79.2,
            interface=78.6,
            avg_score=0.821,
            avg_time=255.9,
            tool_calls=29.4,
            input_tokens=370888.8,
            output_tokens=4406.6,
        ),
        "large": dict(
            verdict_f1=84.7,
            device=75.0,
            interface=71.4,
            avg_score=0.740,
            avg_time=399.5,
            tool_calls=38.8,
            input_tokens=741085.9,
            output_tokens=6238.7,
        ),
    },
    "DeepSeek": {
        "xs": dict(
            verdict_f1=100.0,
            device=83.3,
            interface=57.1,
            avg_score=0.786,
            avg_time=83.1,
            tool_calls=24.9,
            input_tokens=247552.4,
            output_tokens=2857.2,
        ),
        "small": dict(
            verdict_f1=100.0,
            device=91.7,
            interface=57.1,
            avg_score=0.867,
            avg_time=79.2,
            tool_calls=18.4,
            input_tokens=223539.9,
            output_tokens=2535.7,
        ),
        "medium": dict(
            verdict_f1=100.0,
            device=91.7,
            interface=50.0,
            avg_score=0.821,
            avg_time=77.9,
            tool_calls=16.6,
            input_tokens=229409.1,
            output_tokens=2512.8,
        ),
        "large": dict(
            verdict_f1=97.9,
            device=91.7,
            interface=57.1,
            avg_score=0.837,
            avg_time=83.1,
            tool_calls=20.7,
            input_tokens=477316.2,
            output_tokens=3162.6,
        ),
    },
    "MiniMax": {
        "xs": dict(
            verdict_f1=80.0,
            device=50.0,
            interface=71.4,
            avg_score=0.607,
            avg_time=214.3,
            tool_calls=26.4,
            input_tokens=133292.1,
            output_tokens=8070.7,
        ),
        "small": dict(
            verdict_f1=73.7,
            device=50.0,
            interface=71.4,
            avg_score=0.600,
            avg_time=215.8,
            tool_calls=21.3,
            input_tokens=149411.7,
            output_tokens=7543.3,
        ),
        "medium": dict(
            verdict_f1=82.9,
            device=70.8,
            interface=50.0,
            avg_score=0.714,
            avg_time=180.3,
            tool_calls=17.9,
            input_tokens=146968.4,
            output_tokens=6333.2,
        ),
        "large": dict(
            verdict_f1=82.9,
            device=62.5,
            interface=42.9,
            avg_score=0.587,
            avg_time=230.3,
            tool_calls=24.0,
            input_tokens=316138.6,
            output_tokens=9441.7,
        ),
    },
    "OpenAI": {
        "xs": dict(
            verdict_f1=95.7,
            device=83.3,
            interface=57.1,
            avg_score=0.750,
            avg_time=71.9,
            tool_calls=17.9,
            input_tokens=85444.4,
            output_tokens=1798.9,
        ),
        "small": dict(
            verdict_f1=100.0,
            device=83.3,
            interface=71.4,
            avg_score=0.800,
            avg_time=70.0,
            tool_calls=14.6,
            input_tokens=80932.9,
            output_tokens=1757.9,
        ),
        "medium": dict(
            verdict_f1=95.7,
            device=87.5,
            interface=42.9,
            avg_score=0.786,
            avg_time=69.0,
            tool_calls=14.0,
            input_tokens=92580.0,
            output_tokens=1777.9,
        ),
        "large": dict(
            verdict_f1=95.8,
            device=79.2,
            interface=21.4,
            avg_score=0.606,
            avg_time=85.0,
            tool_calls=15.9,
            input_tokens=153676.7,
            output_tokens=2279.0,
        ),
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

DEFAULT_RELEASE_DATA = (
    Path(__file__).resolve().parents[1] / "docs" / "public" / "assets" / "benchmark" / "deepseek_v02_release.json"
)
DEFAULT_K12_CASE_DATA = (
    Path(__file__).resolve().parents[1] / "docs" / "public" / "assets" / "benchmark" / "deepseek_v02_k12_cases.json"
)
DEFAULT_ANALYSIS_DATA = (
    Path(__file__).resolve().parents[1] / "docs" / "public" / "assets" / "benchmark" / "deepseek_v02_analysis.json"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ADVISOR_TOPOLOGIES = ["medium", "large", "xlarge", "fat-tree-k8", "fat-tree-k12"]
ADVISOR_LABELS = {
    "medium": "Medium",
    "large": "Large",
    "xlarge": "Xlarge",
    "fat-tree-k8": "Fat-tree K=8",
    "fat-tree-k12": "Fat-tree K=12",
}

FAULT_ORDER = [
    "acl_misconfig",
    "bgp_neighbor_misconfig",
    "blackhole_route",
    "device_down",
    "high_latency",
    "link_down",
    "link_flapping",
    "mtu_mismatch",
    "packet_corruption",
    "packet_loss",
    "route_policy_misconfig",
    "static_route_misconfig",
]
FAULT_LABELS = {
    "acl_misconfig": "ACL misconfig",
    "bgp_neighbor_misconfig": "BGP neighbor",
    "blackhole_route": "Blackhole route",
    "device_down": "Device down",
    "high_latency": "High latency",
    "link_down": "Link down",
    "link_flapping": "Link flapping",
    "mtu_mismatch": "MTU mismatch",
    "packet_corruption": "Packet corruption",
    "packet_loss": "Packet loss",
    "route_policy_misconfig": "Route policy",
    "static_route_misconfig": "Static route",
    "healthy_network": "Healthy",
}

# ---------------------------------------------------------------------------
# SIGCOMM/NSDI visual style
# ---------------------------------------------------------------------------

# Slightly wider than a single-column plot to keep grouped labels readable.
FIG_W = 4.8  # inches
FIG_H = 3.0  # inches (extra height for below-axis legend)

# Colour-blind-friendly palette (Wong 2011 + extension)
COLORS = {
    "Kimi": "#0072B2",  # blue
    "DeepSeek": "#D55E00",  # vermillion
    "OpenAI": "#009E73",  # bluish green
    "MiniMax": "#CC79A7",  # pink/purple
}
HATCHES = {
    "Kimi": "\\\\",
    "DeepSeek": "////",
    "OpenAI": "..",
    "MiniMax": "xx",
}

FONT_FAMILY = "DejaVu Sans"
LABEL_SIZE = 8
TICK_SIZE = 7.5
LEGEND_SIZE = 7.5
TITLE_SIZE = 8.5


def _apply_base_style() -> None:
    plt.rcParams.update(
        {
            "font.family": FONT_FAMILY,
            "font.size": LABEL_SIZE,
            "axes.labelsize": LABEL_SIZE,
            "axes.titlesize": TITLE_SIZE,
            "xtick.labelsize": TICK_SIZE,
            "ytick.labelsize": TICK_SIZE,
            "legend.fontsize": LEGEND_SIZE,
            "axes.linewidth": 0.8,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "axes.grid": True,
            "grid.linestyle": "--",
            "grid.linewidth": 0.4,
            "grid.alpha": 0.5,
            "axes.axisbelow": True,
            "legend.framealpha": 0.9,
            "legend.edgecolor": "0.7",
            "legend.borderpad": 0.3,
            "legend.handlelength": 1.5,
            "legend.handletextpad": 0.4,
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.12,
        }
    )


def _grouped_bar(
    ax, values_dict: dict, ylabel: str, ylim: tuple, yticks=None, pct: bool = True, val_fmt: str = None
) -> None:
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
                ha="center",
                va="bottom",
                fontsize=5.5,
                zorder=4,
            )

    ax.set_xticks(x)
    ax.set_xticklabels(SCALE_LABELS, linespacing=1.1)
    ax.set_xlabel("Topology Scale (# scenarios)", labelpad=3)
    ax.set_ylabel(ylabel, labelpad=3)
    ax.set_ylim(*ylim)
    if yticks is not None:
        ax.set_yticks(yticks)
    if pct:
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))

    # Remove top/right spines
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Legend below the axes
    handles = [
        mpatches.Patch(facecolor=COLORS[v], hatch=HATCHES[v], edgecolor="gray", linewidth=0.5, label=LEGEND_LABELS[v])
        for v in VENDORS
    ]
    ax.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.28),
        ncol=4,
        frameon=False,
        columnspacing=0.8,
        handlelength=1.2,
    )


# ---------------------------------------------------------------------------
# Individual figure generators
# ---------------------------------------------------------------------------


def _fig_verdict_f1(outdir: Path) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    vals = {v: [DATA[v][s]["verdict_f1"] for s in SCALES] for v in VENDORS}
    _grouped_bar(ax, vals, ylabel="Verdict F1-score (%)", ylim=(0, 120), yticks=[0, 20, 40, 60, 80, 100])
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
    _grouped_bar(ax, vals, ylabel="Device Localization Rate (%)", ylim=(0, 120), yticks=[0, 20, 40, 60, 80, 100])
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
    _grouped_bar(ax, vals, ylabel="Interface Localization Rate (%)", ylim=(0, 120), yticks=[0, 20, 40, 60, 80, 100])
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
    _grouped_bar(
        ax, vals, ylabel="Composite Score (%)", ylim=(0, 115), yticks=[0, 20, 40, 60, 80, 100], val_fmt="{:.1f}"
    )
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
    _grouped_bar(
        ax, vals, ylabel="Avg Diagnosis Time (s)", ylim=(0, 650), yticks=[0, 100, 200, 300, 400, 500, 600], pct=False
    )
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
    _grouped_bar(
        ax,
        vals,
        ylabel="Avg Tool Calls / Case",
        ylim=(0, 55),
        yticks=[0, 10, 20, 30, 40, 50],
        pct=False,
        val_fmt="{:.1f}",
    )
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
    _grouped_bar(
        ax,
        vals,
        ylabel="Input Tokens / Case (K)",
        ylim=(0, 1120),
        yticks=[0, 200, 400, 600, 800, 1000],
        pct=False,
        val_fmt="{:.0f}K",
    )
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
    _grouped_bar(
        ax,
        vals,
        ylabel="Output Tokens / Case",
        ylim=(0, 12500),
        yticks=[0, 2500, 5000, 7500, 10000, 12500],
        pct=False,
        val_fmt="{:.0f}",
    )
    fig.tight_layout(rect=[0, 0.13, 1, 1])
    out = outdir / "fig_output_tokens.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# NetOpsBench 0.2 DeepSeek release rerun
# ---------------------------------------------------------------------------


def _load_release_data(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("benchmark_contract") != "netopsbench-0.2-release":
        raise ValueError(f"unsupported release result contract: {path}")
    missing_scales = set(SCALES) - set(payload.get("scales", {}))
    if missing_scales:
        raise ValueError(f"release result is missing scales {sorted(missing_scales)}: {path}")
    return payload


def _load_k12_case_data(path: Path, release_data: dict) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("benchmark_contract") != "netopsbench-0.2-release":
        raise ValueError(f"unsupported K12 case result contract: {path}")
    if payload.get("topology") != "fat-tree-k12":
        raise ValueError(f"K12 case result has the wrong topology: {path}")
    cases = payload.get("cases", [])
    scenario_ids = [case.get("scenario_id") for case in cases]
    if payload.get("case_count") != 70 or len(cases) != 70 or len(set(scenario_ids)) != 70:
        raise ValueError(f"K12 case result must contain 70 unique cases: {path}")
    healthy = [case for case in cases if case.get("fault_type") == "healthy_network"]
    if len(healthy) != 4:
        raise ValueError(f"K12 case result must contain four healthy cases: {path}")
    expected_faults = {*FAULT_ORDER, "healthy_network"}
    actual_faults = {case.get("fault_type") for case in cases}
    if actual_faults != expected_faults:
        raise ValueError(f"K12 case result has unexpected fault families: {sorted(actual_faults)}")
    score = sum(float(case["score"]) for case in cases) / len(cases)
    expected_score = float(release_data["large_topologies"]["fat-tree-k12"]["primary_reward"])
    if not np.isclose(score, expected_score, atol=1e-9):
        raise ValueError(f"K12 diagnosis score {score} does not match release result {expected_score}")
    return payload


def _load_analysis_data(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("benchmark_contract") != "netopsbench-0.2-release":
        raise ValueError(f"unsupported release analysis contract: {path}")
    totals = payload.get("totals", {})
    expected_totals = {"operational_cases": 319, "agent_scored_cases": 319, "atif_trajectories": 319}
    if any(int(totals.get(key, -1)) != value for key, value in expected_totals.items()):
        raise ValueError(f"release analysis must contain 319 operational, Agent, and ATIF cases: {path}")
    expected_faults = {*FAULT_ORDER, "healthy_network"}
    families = payload.get("fault_families", {})
    if set(families) != expected_faults:
        raise ValueError(f"release analysis has unexpected fault families: {sorted(families)}")
    for fault_type, values in families.items():
        operational = int(values["operational_cases"])
        agent_cases = int(values["agent_cases"])
        applicable = int(values["interface_applicable"])
        if operational != agent_cases:
            raise ValueError(f"{fault_type} has mismatched operational and Agent case counts")
        for numerator, denominator in (
            ("signal_cases", operational),
            ("correct_verdict", agent_cases),
            ("correct_device", agent_cases),
            ("correct_interface", applicable),
        ):
            value = int(values[numerator])
            if value < 0 or value > denominator:
                raise ValueError(f"{fault_type}.{numerator} exceeds its denominator")
    return payload


def _release_scale_rows(release_data: dict) -> list[dict]:
    rows = []
    for key, label, architecture in [
        ("xs", "XS", "CLOS"),
        ("small", "Small", "CLOS"),
        ("medium", "Medium", "CLOS"),
        ("large", "Large", "CLOS"),
        ("xlarge", "Xlarge", "CLOS"),
        ("fat-tree-k8", "K=8", "Fat-tree"),
        ("fat-tree-k12", "K=12", "Fat-tree"),
    ]:
        source = release_data["scales"].get(key) or release_data["large_topologies"][key]
        rows.append(
            {
                "key": key,
                "label": label,
                "architecture": architecture,
                "cases": int(source.get("cases", source.get("agent_scored_cases"))),
                "diagnosis_score": 100 * float(source["primary_reward"]),
                "detection_f1": 100 * float(source["detection_f1"]),
            }
        )
    return rows


def _save_release_figure(fig, outdir: Path, stem: str) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    svg = outdir / f"{stem}.svg"
    fig.savefig(svg, facecolor="white")
    svg.write_text(
        "\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n",
        encoding="utf-8",
    )
    fig.savefig(svg.with_suffix(".png"), facecolor="white")
    plt.close(fig)
    return svg


def _release_figure_header(fig, title: str, subtitle: str):
    title_text = fig.text(0.075, 0.965, title, ha="left", va="top", fontsize=14, fontweight="bold")
    subtitle_text = fig.text(0.075, 0.885, subtitle, ha="left", va="top", color="#64748B", fontsize=8.5)
    return title_text, subtitle_text


def _fig_readme_release_overview(outdir: Path, release_data: dict) -> Path:
    _apply_base_style()
    rows = _release_scale_rows(release_data)
    y = np.array([0, 1, 2, 3, 4, 6, 7], dtype=float)
    fig, ax = plt.subplots(figsize=(9.2, 4.85))
    ax.axhspan(-0.48, 4.48, color="#F8FAFC", zorder=0)
    ax.axhspan(5.52, 7.48, color="#F0FDFA", zorder=0)
    for position, row in zip(y, rows, strict=True):
        low = row["diagnosis_score"]
        high = row["detection_f1"]
        ax.hlines(position, low, high, color="#CBD5E1", linewidth=3, zorder=2)
        ax.scatter(low, position, s=58, color="#0F766E", edgecolor="white", linewidth=0.7, zorder=3)
        ax.scatter(high, position, s=58, color="#2563EB", edgecolor="white", linewidth=0.7, zorder=3)
        ax.annotate(
            f"{low:.1f}",
            (low, position),
            xytext=(-8, 0),
            textcoords="offset points",
            ha="right",
            va="center",
            fontsize=7.5,
            fontweight="bold",
            color="#0F766E",
        )
        ax.annotate(
            f"{high:.1f}",
            (high, position),
            xytext=(8, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=7.5,
            fontweight="bold",
            color="#2563EB",
        )
    ax.set_yticks(y, [f"{row['label']}   n={row['cases']}" for row in rows])
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    ax.grid(axis="x", color="#E2E8F0", linestyle="-", linewidth=0.6)
    ax.grid(axis="y", visible=False)
    ax.spines[:].set_visible(False)
    ax.tick_params(axis="both", length=0, colors="#64748B")
    ax.text(0.01, 0.985, "CLOS", transform=ax.transAxes, va="top", color="#64748B", fontsize=7, fontweight="bold")
    ax.text(
        0.01,
        0.245,
        "FAT-TREE",
        transform=ax.transAxes,
        va="top",
        color="#0F766E",
        fontsize=7,
        fontweight="bold",
    )
    handles = [
        mpatches.Patch(color="#0F766E", label="Diagnosis score"),
        mpatches.Patch(color="#2563EB", label="Fault detection F1"),
    ]
    ax.legend(handles=handles, loc="upper right", bbox_to_anchor=(1, 1.12), frameon=False, ncol=2)
    _release_figure_header(
        fig,
        "Diagnosis quality across seven scales",
        "DeepSeek V4 Pro · 319 cases · temperature 0",
    )
    fig.subplots_adjust(left=0.18, right=0.95, top=0.78, bottom=0.11)
    return _save_release_figure(fig, outdir, "fig_deepseek_v02_overview")


def _k12_case_outcome(case: dict) -> str:
    if not case["correct_verdict"]:
        return "incorrect_verdict"
    if case["fault_type"] == "healthy_network":
        return "complete"
    if case["correct_device"] and (not case["interface_applicable"] or case["correct_interface"]):
        return "complete"
    if case["correct_device"]:
        return "device_only"
    return "location_missed"


def _fig_readme_k12_case_map(outdir: Path, case_data: dict) -> Path:
    _apply_base_style()
    order = [*FAULT_ORDER, "healthy_network"]
    by_fault = {fault: [] for fault in order}
    for case in case_data["cases"]:
        by_fault[case["fault_type"]].append(case)
    for cases in by_fault.values():
        cases.sort(key=lambda case: case["scenario_id"])

    colors = {
        "complete": "#009E73",
        "device_only": "#56B4E9",
        "location_missed": "#E69F00",
        "incorrect_verdict": "#CC79A7",
    }
    labels = {
        "complete": "Fully localized / healthy correct",
        "device_only": "Device correct; interface missed",
        "location_missed": "Fault detected; location missed",
        "incorrect_verdict": "Incorrect verdict / inconclusive",
    }
    fig, ax = plt.subplots(figsize=(9.2, 5.75))
    y = np.arange(len(order))
    for row, fault in enumerate(order):
        if row % 2 == 0:
            ax.axhspan(row - 0.47, row + 0.47, color="#F8FAFC", zorder=0)
        cases = by_fault[fault]
        outcomes = [_k12_case_outcome(case) for case in cases]
        for column, outcome in enumerate(outcomes):
            ax.scatter(
                column,
                row,
                marker="s",
                s=165,
                color=colors[outcome],
                edgecolor="white",
                linewidth=1.0,
                zorder=3,
            )
        complete = outcomes.count("complete")
        ax.text(6.35, row, f"{complete}/{len(cases)} complete", va="center", fontsize=7.5, color="#475569")
    ax.set_yticks(y, [FAULT_LABELS[fault] for fault in order])
    ax.invert_yaxis()
    ax.set_xlim(-0.55, 8.0)
    ax.set_xticks([])
    ax.grid(False)
    ax.spines[:].set_visible(False)
    ax.tick_params(axis="y", length=0, colors="#334155", labelsize=8)
    handles = [mpatches.Patch(color=colors[key], label=labels[key]) for key in colors]
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(0, -0.055), frameon=False, ncol=2, fontsize=7.5)
    _release_figure_header(
        fig,
        "Fat-tree K=12 case outcomes",
        "70 cases · one square per case · grouped by fault family",
    )
    fig.text(
        0.08,
        0.012,
        "Representative deep dive for the largest validated Fat-tree profile; distributions differ across topologies.",
        color="#64748B",
        fontsize=7.5,
    )
    fig.subplots_adjust(left=0.24, right=0.94, top=0.81, bottom=0.18)
    return _save_release_figure(fig, outdir, "fig_deepseek_v02_k12_cases")


def _fig_release_evidence(outdir: Path, analysis_data: dict) -> Path:
    _apply_base_style()
    aggregate = analysis_data["fault_families"]
    order = sorted(
        FAULT_ORDER,
        key=lambda fault: (
            aggregate[fault]["signal_rate"] - aggregate[fault]["verdict_rate"],
            fault,
        ),
    )
    y = np.arange(len(order))
    fig, (left, right) = plt.subplots(
        1,
        2,
        figsize=(9.2, 6.2),
        sharey=True,
        gridspec_kw={"width_ratios": [2.65, 1.2], "wspace": 0.04},
    )
    series = [
        ("Formal Pingmesh signal", "signal_rate", "#2563EB", "o"),
        ("Correct verdict", "verdict_rate", "#D97706", "s"),
        ("Correct device", "device_rate", "#0F766E", "^"),
    ]
    for label, key, color, marker in series:
        values = [100 * float(aggregate[fault][key]) for fault in order]
        left.plot(values, y, linestyle="none", marker=marker, markersize=5.5, color=color, label=label, zorder=3)
    for position, fault in enumerate(order):
        values = [100 * float(aggregate[fault][key]) for _, key, _, _ in series]
        left.hlines(position, min(values), max(values), color="#CBD5E1", linewidth=1, zorder=1)
    left.set_yticks(y, [f"{FAULT_LABELS[fault]}  n={aggregate[fault]['agent_cases']}" for fault in order])
    left.set_xlim(-2, 104)
    left.set_xlabel("Cases in fault family")
    left.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    left.legend(loc="lower center", bbox_to_anchor=(0.5, -0.16), frameon=False, ncol=3, fontsize=7)
    left.set_title("A. Evidence, verdict, and device", fontsize=9, pad=10)
    left.spines[["top", "right"]].set_visible(False)

    for position, fault in enumerate(order):
        values = aggregate[fault]
        applicable = int(values["interface_applicable"])
        if applicable:
            correct = int(values["correct_interface"])
            rate = 100 * correct / applicable
            right.barh(position, rate, height=0.48, color="#A855F7", edgecolor="white", zorder=2)
            right.text(min(rate + 2, 88), position, f"{correct}/{applicable}", va="center", fontsize=7)
        else:
            right.text(50, position, "N/A", ha="center", va="center", color="#94A3B8", fontsize=7)
    right.set_xlim(0, 104)
    right.set_xlabel("Correct interfaces")
    right.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    right.set_title("B. Interface-applicable cases", fontsize=9, pad=10)
    right.spines[["top", "right", "left"]].set_visible(False)
    right.tick_params(axis="y", left=False, labelleft=False)
    left.invert_yaxis()
    _release_figure_header(
        fig,
        "Where diagnosis breaks down",
        "Case-level micro aggregation across all 319 v0.2 cases",
    )
    fig.text(
        0.075,
        0.018,
        "N/A means the evaluator does not require interface localization for that fault family.",
        color="#64748B",
        fontsize=7.5,
    )
    fig.subplots_adjust(left=0.25, right=0.97, bottom=0.15, top=0.78, wspace=0.04)
    return _save_release_figure(fig, outdir, "fig_deepseek_v02_evidence")


def _fig_release_cost(outdir: Path, release_data: dict) -> Path:
    _apply_base_style()
    rows = _release_scale_rows(release_data)
    fig, axes = plt.subplots(1, 3, figsize=(9.2, 4.25))
    configs = [
        ("avg_input_tokens", "Input tokens (K)", lambda value: value / 1000, "{:.0f}"),
        ("avg_tool_calls", "Tool calls", lambda value: value, "{:.1f}"),
        ("avg_agent_seconds", "Diagnosis time (s)", lambda value: value, "{:.1f}"),
    ]
    keys = [row["key"] for row in rows]
    labels = [row["label"] for row in rows]
    colors = ["#60A5FA" if row["architecture"] == "CLOS" else "#14B8A6" for row in rows]
    x = np.arange(len(rows))
    for ax, (key, ylabel, transform, value_format) in zip(axes, configs, strict=True):
        values = []
        for topology in keys:
            source = release_data["scales"].get(topology) or release_data["large_topologies"][topology]
            values.append(transform(float(source[key])))
        bars = ax.bar(x, values, width=0.68, color=colors, edgecolor="white", zorder=3)
        for bar, value in zip(bars, values, strict=True):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + max(values) * 0.025,
                value_format.format(value),
                ha="center",
                va="bottom",
                fontsize=6.5,
            )
        ax.axvline(4.5, color="#CBD5E1", linewidth=0.8, linestyle="--", zorder=1)
        ax.set_xticks(x, labels, rotation=35, ha="right")
        ax.set_ylabel(ylabel)
        ax.set_ylim(0, max(values) * 1.2)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, zorder=0)
    _release_figure_header(
        fig,
        "Diagnosis cost as topology scale grows",
        "Tool usage stays comparatively stable while context and runtime increase",
    )
    fig.subplots_adjust(left=0.075, right=0.98, bottom=0.23, top=0.76, wspace=0.34)
    return _save_release_figure(fig, outdir, "fig_deepseek_v02_cost")


# ---------------------------------------------------------------------------
# Combined 2×3 overview figure
# ---------------------------------------------------------------------------


def _fig_combined(outdir: Path) -> Path:
    _apply_base_style()
    fig, axes = plt.subplots(2, 3, figsize=(7.0, 4.0))

    configs = [
        ("verdict_f1", "Verdict F1-score (%)", (0, 115), [0, 20, 40, 60, 80, 100], True),
        ("device", "Device Loc. Rate (%)", (0, 115), [0, 20, 40, 60, 80, 100], True),
        ("interface", "Interface Loc. Rate (%)", (0, 115), [0, 20, 40, 60, 80, 100], True),
        ("avg_score", "Composite Score", (0, 1.15), [0, 0.2, 0.4, 0.6, 0.8, 1.0], False),
        ("avg_time", "Avg Diagnosis Time (s)", (0, 650), [0, 100, 200, 300, 400, 500, 600], False),
    ]

    x = np.arange(len(SCALES))
    bar_w = 0.22
    group_w = bar_w * len(VENDORS)
    offsets = np.linspace(-(group_w - bar_w) / 2, (group_w - bar_w) / 2, len(VENDORS))

    for i, (key, ylabel, ylim, yticks, is_pct) in enumerate(configs):
        ax = axes[i // 3][i % 3]
        for idx, vendor in enumerate(VENDORS):
            raw = [DATA[vendor][s][key] for s in SCALES]
            ax.bar(
                x + offsets[idx],
                raw,
                width=bar_w,
                color=COLORS[vendor],
                hatch=HATCHES[vendor],
                edgecolor="white",
                linewidth=0.5,
                label=vendor,
                zorder=3,
            )
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
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        if i == 0:
            handles = [
                mpatches.Patch(
                    facecolor=COLORS[v], hatch=HATCHES[v], edgecolor="gray", linewidth=0.5, label=LEGEND_LABELS[v]
                )
                for v in VENDORS
            ]
            ax.legend(handles=handles, loc="upper right", fontsize=6.0, ncol=2, frameon=True)

    # Hide the unused 6th subplot
    axes[1][2].set_visible(False)

    fig.suptitle("NetOpsBench: Kimi / DeepSeek / OpenAI / MiniMax across Topology Scales", fontsize=9, y=1.01)
    fig.tight_layout()
    out = outdir / "fig_combined_overview.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"))
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Five-topology advisor report
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _normalise_fault_type(value: str | None) -> str:
    return "healthy_network" if value in {None, "none", "healthy_network"} else value


def _empty_fault_metrics() -> dict:
    return {
        "operational_cases": 0,
        "signal_cases": 0,
        "agent_cases": 0,
        "correct_verdict": 0,
        "correct_device": 0,
        "interface_applicable": 0,
        "correct_interface": 0,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _with_rates(values: dict) -> dict:
    result = dict(values)
    result.update(
        {
            "signal_rate": _rate(values["signal_cases"], values["operational_cases"]),
            "verdict_rate": _rate(values["correct_verdict"], values["agent_cases"]),
            "device_rate": _rate(values["correct_device"], values["agent_cases"]),
            "interface_rate": _rate(values["correct_interface"], values["interface_applicable"]),
        }
    )
    return result


def _agent_metrics(path: Path) -> tuple[dict[str, dict], dict]:
    rows = _read_jsonl(path)
    scenario_ids = [str(row["scenario_id"]) for row in rows]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError(f"duplicate Agent scenario IDs in {path}")

    by_fault: dict[str, dict] = {}
    predicted_faults = 0
    detected_positives = 0
    positive_cases = 0
    correct_device = 0
    interface_applicable = 0
    correct_interface = 0
    correct_fault_type = 0
    correct_verdict = 0
    score_total = 0.0
    healthy_cases = 0
    healthy_correct = 0
    recursion_cases = 0

    for row in rows:
        details = row["details"]
        fault_type = _normalise_fault_type(details.get("ground_truth", {}).get("fault_type"))
        metrics = by_fault.setdefault(fault_type, _empty_fault_metrics())
        metrics["agent_cases"] += 1
        metrics["correct_verdict"] += int(bool(row["correct_verdict"]))
        correct_verdict += int(bool(row["correct_verdict"]))
        score_total += float(row["score"])

        verdict = details["agent_output"]["verdict"]
        predicted_faults += int(verdict == "fault_detected")
        recursion_cases += int(details["agent_output"].get("metadata", {}).get("error_type") == "GraphRecursionError")

        if fault_type == "healthy_network":
            healthy_cases += 1
            healthy_correct += int(bool(row["correct_verdict"]))
            continue

        positive_cases += 1
        detected_positives += int(verdict == "fault_detected")
        metrics["correct_device"] += int(bool(row["correct_device"]))
        correct_device += int(bool(row["correct_device"]))
        correct_fault_type += int(bool(row["correct_fault_type"]))
        if details["interface_applicable"]:
            metrics["interface_applicable"] += 1
            metrics["correct_interface"] += int(bool(row["correct_interface"]))
            interface_applicable += 1
            correct_interface += int(bool(row["correct_interface"]))

    precision = _rate(detected_positives, predicted_faults) or 0.0
    recall = _rate(detected_positives, positive_cases) or 0.0
    detection_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    overall = {
        "agent_cases": len(rows),
        "positive_cases": positive_cases,
        "healthy_cases": healthy_cases,
        "healthy_correct": healthy_correct,
        "correct_verdict": correct_verdict,
        "detected_positives": detected_positives,
        "predicted_faults": predicted_faults,
        "correct_device": correct_device,
        "interface_applicable": interface_applicable,
        "correct_interface": correct_interface,
        "correct_fault_type": correct_fault_type,
        "recursion_cases": recursion_cases,
        "primary_reward_sum": score_total,
        "primary_reward": score_total / len(rows),
        "detection_f1": detection_f1,
        "device_localization_rate": correct_device / positive_cases,
        "device_given_detection": correct_device / detected_positives,
        "interface_localization_rate": correct_interface / interface_applicable,
        "fault_type_accuracy": correct_fault_type / positive_cases,
        "scenario_ids": scenario_ids,
    }
    return by_fault, overall


def _observability_metrics(topology: str, path: Path) -> dict[str, dict]:
    by_fault: dict[str, dict] = {}
    payload = json.loads(path.read_text(encoding="utf-8"))

    if topology in {"medium", "large"}:
        for row in payload:
            fault_type = _normalise_fault_type(row["fault_type"])
            metrics = by_fault.setdefault(fault_type, _empty_fault_metrics())
            if not (row["case_valid"] and row["query_ok"] and row["coverage_status"] == "complete"):
                raise ValueError(f"invalid observation for {row['scenario_id']}")
            metrics["operational_cases"] += 1
            metrics["signal_cases"] += int(row["summary"]["total_anomalies"] > 0)
        return by_fault

    if topology == "xlarge":
        source = payload["pingmesh"]["by_family"]
        for raw_fault, values in source.items():
            fault_type = _normalise_fault_type(raw_fault)
            metrics = by_fault.setdefault(fault_type, _empty_fault_metrics())
            metrics["operational_cases"] = int(values["cases"])
            metrics["signal_cases"] = sum(int(total) > 0 for total in values["pingmesh_totals"])
        return by_fault

    if topology == "fat-tree-k8":
        for raw_fault, values in payload["fault_families"].items():
            fault_type = _normalise_fault_type(raw_fault)
            metrics = by_fault.setdefault(fault_type, _empty_fault_metrics())
            metrics["operational_cases"] = int(values["cases"])
            metrics["signal_cases"] = int(values["formal_anomaly_cases"])
        return by_fault

    if topology == "fat-tree-k12":
        for raw_fault, values in payload["pingmesh_by_fault_type"].items():
            fault_type = _normalise_fault_type(raw_fault)
            metrics = by_fault.setdefault(fault_type, _empty_fault_metrics())
            metrics["operational_cases"] = int(values["cases"])
            metrics["signal_cases"] = int(values["cases_with_anomaly"])
        return by_fault

    raise ValueError(f"unsupported advisor topology: {topology}")


def _official_overall(release_data: dict, topology: str) -> dict:
    if topology in {"medium", "large"}:
        values = release_data["scales"][topology]
        operational_cases = int(values["cases"])
        agent_cases = int(values["cases"])
    else:
        values = release_data["large_topologies"][topology]
        operational_cases = int(values["operationally_valid_cases"])
        agent_cases = int(values["agent_scored_cases"])
    return {
        "operational_cases": operational_cases,
        "agent_cases": agent_cases,
        "primary_reward": float(values["primary_reward"]),
        "detection_f1": float(values["detection_f1"]),
        "device_localization_rate": float(values["device_localization_rate"]),
        "interface_localization_rate": float(values["interface_localization_rate"]),
        "fault_type_accuracy": float(values["fault_type_accuracy"]),
        "avg_agent_seconds": float(values["avg_agent_seconds"]),
        "avg_tool_calls": float(values["avg_tool_calls"]),
        "avg_input_tokens": float(values["avg_input_tokens"]),
    }


def _assert_close(name: str, actual: float, expected: float, tolerance: float = 0.0011) -> None:
    if abs(actual - expected) > tolerance:
        raise ValueError(f"{name} mismatch: derived={actual:.6f} official={expected:.6f}")


def _load_advisor_manifest(path: Path) -> tuple[dict[str, Path], dict[str, Path], dict[str, dict[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("topologies")
    if not isinstance(entries, dict) or set(entries) != set(ADVISOR_TOPOLOGIES):
        raise ValueError(f"advisor manifest must define exactly {ADVISOR_TOPOLOGIES}: {path}")

    result_paths: dict[str, Path] = {}
    observability_paths: dict[str, Path] = {}
    provenance: dict[str, dict[str, str]] = {}
    for topology in ADVISOR_TOPOLOGIES:
        entry = entries[topology]
        if not isinstance(entry, dict) or set(entry) != {"results", "observability"}:
            raise ValueError(f"advisor manifest entry {topology!r} must define results and observability")
        provenance[topology] = {}
        for key, destination in (("results", result_paths), ("observability", observability_paths)):
            raw = entry[key]
            if not isinstance(raw, str) or not raw:
                raise ValueError(f"advisor manifest entry {topology!r}.{key} must be a non-empty path")
            resolved = Path(raw)
            if not resolved.is_absolute():
                resolved = path.parent / resolved
            resolved = resolved.resolve()
            if not resolved.is_file():
                raise ValueError(f"advisor input does not exist: {resolved}")
            destination[topology] = resolved
            provenance[topology][key] = raw
    return result_paths, observability_paths, provenance


def _build_advisor_dataset(
    release_data: dict,
    result_paths: dict[str, Path],
    observability_paths: dict[str, Path],
    provenance: dict[str, dict[str, str]],
) -> dict:
    topologies: dict[str, dict] = {}
    operational_ids: set[str] = set()
    all_agent_ids: set[str] = set()

    for topology in ADVISOR_TOPOLOGIES:
        agent_faults, derived = _agent_metrics(result_paths[topology])
        observation_faults = _observability_metrics(topology, observability_paths[topology])
        official = _official_overall(release_data, topology)

        if derived["agent_cases"] != official["agent_cases"]:
            raise ValueError(f"Agent case count mismatch for {topology}")
        for metric in (
            "primary_reward",
            "detection_f1",
            "device_localization_rate",
            "interface_localization_rate",
            "fault_type_accuracy",
        ):
            _assert_close(f"{topology}.{metric}", derived[metric], official[metric])

        faults: dict[str, dict] = {}
        for fault_type in [*FAULT_ORDER, "healthy_network"]:
            merged = _empty_fault_metrics()
            for key, value in observation_faults.get(fault_type, {}).items():
                merged[key] = value
            for key in (
                "agent_cases",
                "correct_verdict",
                "correct_device",
                "interface_applicable",
                "correct_interface",
            ):
                merged[key] = agent_faults.get(fault_type, {}).get(key, 0)
            faults[fault_type] = _with_rates(merged)

        operational_cases = sum(values["operational_cases"] for values in faults.values())
        if operational_cases != official["operational_cases"]:
            raise ValueError(f"operational case count mismatch for {topology}")

        topology_ids = set(derived.pop("scenario_ids"))
        if len(topology_ids) != operational_cases:
            raise ValueError(f"operational scenario identity mismatch for {topology}")
        if operational_ids & topology_ids:
            raise ValueError(f"scenario IDs overlap across topologies: {topology}")
        operational_ids.update(topology_ids)
        all_agent_ids.update(topology_ids)

        for key in (
            "positive_cases",
            "healthy_cases",
            "healthy_correct",
            "correct_verdict",
            "detected_positives",
            "predicted_faults",
            "correct_device",
            "interface_applicable",
            "correct_interface",
            "correct_fault_type",
            "recursion_cases",
            "primary_reward_sum",
            "device_given_detection",
        ):
            official[key] = derived[key]
        topologies[topology] = {"overall": official, "faults": faults}

    if len(operational_ids) != 290 or len(all_agent_ids) != 290:
        raise ValueError("advisor report requires exactly 290 operational and 290 Agent-scored scenarios")

    fault_aggregate: dict[str, dict] = {}
    for fault_type in [*FAULT_ORDER, "healthy_network"]:
        totals = _empty_fault_metrics()
        for topology in ADVISOR_TOPOLOGIES:
            for key in totals:
                totals[key] += int(topologies[topology]["faults"][fault_type][key])
        fault_aggregate[fault_type] = _with_rates(totals)

    healthy = fault_aggregate["healthy_network"]
    if healthy["operational_cases"] != 20 or healthy["signal_cases"] != 0:
        raise ValueError("expected 20 clean healthy observations")
    if healthy["agent_cases"] != 20 or healthy["correct_verdict"] != 20:
        raise ValueError("expected 20 correctly diagnosed healthy cases")

    return {
        "schema_version": 1,
        "title": "NetOpsBench v0.2.0 five-topology DeepSeek advisor report",
        "benchmark_contract": "netopsbench-0.2-release",
        "model": "deepseek-v4-pro",
        "agent": "minimal-deepagent",
        "aggregation": "case-level micro average",
        "totals": {
            "operational_cases": 290,
            "agent_scored_cases": 290,
            "healthy_observations": 20,
            "healthy_agent_cases": 20,
        },
        "topology_order": ADVISOR_TOPOLOGIES,
        "fault_order": [*FAULT_ORDER, "healthy_network"],
        "topologies": topologies,
        "fault_aggregate": fault_aggregate,
        "provenance": provenance,
        "notes": ["Interface localization uses only evaluator-marked interface-applicable cases."],
    }


def _write_advisor_csv(dataset: dict, path: Path) -> None:
    fields = [
        "section",
        "topology",
        "fault_type",
        "operational_cases",
        "signal_cases",
        "signal_rate",
        "agent_cases",
        "correct_verdict",
        "verdict_rate",
        "correct_device",
        "device_rate",
        "interface_applicable",
        "correct_interface",
        "interface_rate",
        "positive_cases",
        "healthy_cases",
        "healthy_correct",
        "predicted_faults",
        "detected_positives",
        "correct_fault_type",
        "recursion_cases",
        "primary_reward_sum",
        "primary_reward",
        "detection_f1",
        "device_localization_rate",
        "device_given_detection",
        "interface_localization_rate",
        "avg_input_tokens",
        "avg_tool_calls",
        "avg_agent_seconds",
    ]
    rows: list[dict] = []
    for topology in ADVISOR_TOPOLOGIES:
        rows.append({"section": "topology_overall", "topology": topology, **dataset["topologies"][topology]["overall"]})
        for fault_type, values in dataset["topologies"][topology]["faults"].items():
            rows.append({"section": "fault_topology", "topology": topology, "fault_type": fault_type, **values})
    for fault_type, values in dataset["fault_aggregate"].items():
        rows.append({"section": "fault_aggregate", "fault_type": fault_type, **values})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _advisor_architecture_background(ax) -> None:
    ax.axvspan(-0.5, 2.5, color="#DDEBF7", alpha=0.32, zorder=0)
    ax.axvspan(2.5, 4.5, color="#FCE4D6", alpha=0.32, zorder=0)
    ax.axvline(2.5, color="0.45", linewidth=0.8, linestyle="--", zorder=1)
    transform = ax.get_xaxis_transform()
    ax.text(1.0, 1.02, "CLOS", transform=transform, ha="center", va="bottom", fontsize=8, fontweight="bold")
    ax.text(3.5, 1.02, "Fat-tree", transform=transform, ha="center", va="bottom", fontsize=8, fontweight="bold")


def _save_advisor_figure(fig, outdir: Path, stem: str) -> Path:
    pdf = outdir / f"{stem}.pdf"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.12)
    fig.savefig(pdf.with_suffix(".png"), dpi=300, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    return pdf


def _fig_advisor_overall_quality(outdir: Path, dataset: dict) -> Path:
    _apply_base_style()
    fig, ax = plt.subplots(figsize=(8.0, 4.4))
    metrics = [
        ("Diagnosis score", "primary_reward", "#009E73", ""),
        ("Fault detection F1", "detection_f1", "#0072B2", "\\\\"),
        ("Device localization", "device_localization_rate", "#D55E00", "////"),
        ("Interface localization", "interface_localization_rate", "#CC79A7", ".."),
    ]
    x = np.arange(len(ADVISOR_TOPOLOGIES))
    width = 0.19
    offsets = np.linspace(-1.5 * width, 1.5 * width, len(metrics))
    _advisor_architecture_background(ax)
    for offset, (label, key, color, hatch) in zip(offsets, metrics, strict=False):
        values = [100 * dataset["topologies"][name]["overall"][key] for name in ADVISOR_TOPOLOGIES]
        bars = ax.bar(
            x + offset,
            values,
            width,
            label=label,
            color=color,
            hatch=hatch,
            edgecolor="white",
            linewidth=0.5,
            zorder=3,
        )
        for bar, value in zip(bars, values, strict=False):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + 1.2,
                f"{value:.0f}",
                ha="center",
                va="bottom",
                fontsize=6,
            )
    labels = []
    for name in ADVISOR_TOPOLOGIES:
        cases = dataset["topologies"][name]["overall"]["agent_cases"]
        labels.append(f"{ADVISOR_LABELS[name]}\n(n={cases})")
    ax.set_xticks(x, labels)
    ax.set_ylabel("Result (%)")
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False)
    ax.set_title("Overall Benchmark Quality Across Topologies", pad=20)
    fig.tight_layout(rect=[0, 0.04, 1, 1])
    return _save_advisor_figure(fig, outdir, "fig1_overall_quality")


def _fig_advisor_evidence_to_localization(outdir: Path, dataset: dict) -> Path:
    _apply_base_style()
    aggregate = dataset["fault_aggregate"]
    order = sorted(
        FAULT_ORDER,
        key=lambda fault: (
            (aggregate[fault]["signal_rate"] or 0) - (aggregate[fault]["verdict_rate"] or 0),
            fault,
        ),
    )
    y = np.arange(len(order))
    fig, (left, right) = plt.subplots(
        1,
        2,
        figsize=(11.0, 6.3),
        sharey=True,
        gridspec_kw={"width_ratios": [2.6, 1.25], "wspace": 0.05},
    )
    series = [
        ("Pingmesh signal", "signal_rate", "#0072B2", "o"),
        ("Correct verdict", "verdict_rate", "#D55E00", "s"),
        ("Correct device", "device_rate", "#009E73", "^"),
    ]
    for label, key, color, marker in series:
        values = [100 * float(aggregate[fault][key] or 0) for fault in order]
        left.plot(values, y, linestyle="none", marker=marker, markersize=6, color=color, label=label, zorder=3)
    for position, fault in enumerate(order):
        values = [100 * float(aggregate[fault][key] or 0) for _, key, _, _ in series]
        left.hlines(position, min(values), max(values), color="0.82", linewidth=1, zorder=1)
    labels = [
        f"{FAULT_LABELS[fault]}  (obs {aggregate[fault]['operational_cases']}, "
        f"agent {aggregate[fault]['agent_cases']})"
        for fault in order
    ]
    left.set_yticks(y, labels)
    left.set_xlim(-2, 104)
    left.set_xlabel("Cases (%)")
    left.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    left.legend(loc="lower center", bbox_to_anchor=(0.5, -0.18), frameon=False, ncol=3)
    left.set_title("A. Evidence, detection, and device localization")
    left.spines[["top", "right"]].set_visible(False)

    for position, fault in enumerate(order):
        values = aggregate[fault]
        applicable = values["interface_applicable"]
        if applicable:
            rate = 100 * values["correct_interface"] / applicable
            right.barh(position, rate, height=0.48, color="#CC79A7", edgecolor="white", zorder=2)
            right.text(
                min(rate + 2, 93),
                position,
                f"{values['correct_interface']}/{applicable}",
                va="center",
                fontsize=6.5,
            )
        else:
            right.text(50, position, "N/A", ha="center", va="center", color="0.5", fontsize=7)
    right.set_xlim(0, 104)
    right.set_xlabel("Correct interfaces (%)")
    right.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    right.set_title("B. Interface-applicable cases only")
    right.spines[["top", "right", "left"]].set_visible(False)
    right.tick_params(axis="y", left=False, labelleft=False)
    left.invert_yaxis()
    fig.suptitle("From Fault Evidence to Agent Localization", y=1.01, fontsize=10)
    fig.text(
        0.01,
        0.01,
        "Case-level micro aggregation. N/A means the evaluator does not require an interface for that fault family.",
        fontsize=6.5,
    )
    fig.subplots_adjust(left=0.25, right=0.98, bottom=0.16, top=0.86, wspace=0.05)
    return _save_advisor_figure(fig, outdir, "fig2_evidence_to_localization")


def _fig_advisor_cost(outdir: Path, dataset: dict) -> Path:
    _apply_base_style()
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.8))
    configs = [
        ("avg_input_tokens", "Input tokens (K)", lambda value: value / 1000, "{:.0f}"),
        ("avg_tool_calls", "Tool calls", lambda value: value, "{:.1f}"),
        ("avg_agent_seconds", "Diagnosis time (s)", lambda value: value, "{:.1f}"),
    ]
    x = np.arange(len(ADVISOR_TOPOLOGIES))
    colors = ["#56B4E9", "#56B4E9", "#56B4E9", "#E69F00", "#E69F00"]
    for ax, (key, ylabel, transform, value_format) in zip(axes, configs, strict=False):
        _advisor_architecture_background(ax)
        values = [transform(dataset["topologies"][name]["overall"][key]) for name in ADVISOR_TOPOLOGIES]
        bars = ax.bar(x, values, width=0.62, color=colors, edgecolor="white", zorder=3)
        for bar, value in zip(bars, values, strict=False):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + max(values) * 0.025,
                value_format.format(value),
                ha="center",
                va="bottom",
                fontsize=6.5,
            )
        ax.set_xticks(x, ["M", "L", "XL", "K8", "K12"])
        ax.set_ylabel(ylabel)
        ax.set_ylim(0, max(values) * 1.18)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Diagnosis Cost Across Topologies", y=1.02, fontsize=10)
    fig.tight_layout(rect=[0, 0.02, 1, 0.97])
    return _save_advisor_figure(fig, outdir, "fig3_diagnosis_cost")


def _annotated_heatmap(
    ax,
    values: np.ndarray,
    annotations: list[list[str]],
    row_labels: list[str],
    column_labels: list[str],
    title: str,
    *,
    show_ylabels: bool = True,
):
    cmap = matplotlib.colormaps["YlGnBu"].copy()
    cmap.set_bad("#E6E6E6")
    image = ax.imshow(np.ma.masked_invalid(values), cmap=cmap, vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(np.arange(len(column_labels)), column_labels)
    ax.set_yticks(np.arange(len(row_labels)))
    if show_ylabels:
        ax.set_yticklabels(row_labels)
    else:
        ax.tick_params(axis="y", labelleft=False)
    ax.set_title(title)
    ax.tick_params(length=0)
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            value = values[row, column]
            color = "white" if not np.isnan(value) and value >= 58 else "black"
            ax.text(column, row, annotations[row][column], ha="center", va="center", fontsize=5.8, color=color)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return image


def _fig_advisor_pingmesh_heatmap(outdir: Path, dataset: dict) -> Path:
    _apply_base_style()
    faults = [*FAULT_ORDER, "healthy_network"]
    values = np.zeros((len(faults), len(ADVISOR_TOPOLOGIES)))
    annotations: list[list[str]] = []
    for row, fault in enumerate(faults):
        labels = []
        for column, topology in enumerate(ADVISOR_TOPOLOGIES):
            metrics = dataset["topologies"][topology]["faults"][fault]
            values[row, column] = 100 * float(metrics["signal_rate"] or 0)
            expected_ecmp = fault == "bgp_neighbor_misconfig" and metrics["signal_cases"] == 0
            expected_ecmp |= fault == "link_down" and metrics["signal_cases"] < metrics["operational_cases"]
            suffix = "†" if expected_ecmp else ""
            labels.append(f"{metrics['signal_cases']}/{metrics['operational_cases']}{suffix}")
        annotations.append(labels)
    fig, ax = plt.subplots(figsize=(8.5, 6.7))
    image = _annotated_heatmap(
        ax,
        values,
        annotations,
        [FAULT_LABELS[fault] for fault in faults],
        ["Medium", "Large", "Xlarge", "K8", "K12"],
        "Cases with Formal Pingmesh Anomalies",
    )
    colorbar = fig.colorbar(image, ax=ax, fraction=0.03, pad=0.03)
    colorbar.set_label("Signal cases (%)")
    colorbar.ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    fig.text(
        0.01,
        0.01,
        "Cells show signal/valid cases. † Zero or partial signal is expected when ECMP preserves forwarding.",
        fontsize=6.5,
    )
    fig.tight_layout(rect=[0, 0.04, 1, 1])
    return _save_advisor_figure(fig, outdir, "backup1_pingmesh_heatmap")


def _fig_advisor_agent_heatmaps(outdir: Path, dataset: dict) -> Path:
    _apply_base_style()
    metrics = [
        ("Correct verdict", "correct_verdict", "agent_cases"),
        ("Correct device", "correct_device", "agent_cases"),
        ("Correct interface", "correct_interface", "interface_applicable"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(14.0, 6.4), sharey=True)
    image = None
    for index, (ax, (title, numerator, denominator)) in enumerate(zip(axes, metrics, strict=False)):
        values = np.full((len(FAULT_ORDER), len(ADVISOR_TOPOLOGIES)), np.nan)
        annotations: list[list[str]] = []
        for row, fault in enumerate(FAULT_ORDER):
            labels = []
            for column, topology in enumerate(ADVISOR_TOPOLOGIES):
                fault_metrics = dataset["topologies"][topology]["faults"][fault]
                total = int(fault_metrics[denominator])
                correct = int(fault_metrics[numerator])
                if total:
                    values[row, column] = 100 * correct / total
                    labels.append(f"{correct}/{total}")
                else:
                    labels.append("N/A")
            annotations.append(labels)
        image = _annotated_heatmap(
            ax,
            values,
            annotations,
            [FAULT_LABELS[fault] for fault in FAULT_ORDER],
            ["M", "L", "XL", "K8", "K12"],
            title,
            show_ylabels=index == 0,
        )
    fig.subplots_adjust(left=0.14, right=0.88, bottom=0.08, top=0.91, wspace=0.08)
    if image is not None:
        colorbar_axis = fig.add_axes([0.91, 0.2, 0.012, 0.62])
        colorbar = fig.colorbar(image, cax=colorbar_axis)
        colorbar.set_label("Correct cases (%)")
        colorbar.ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=100, decimals=0))
    fig.suptitle("Agent Outcomes by Fault Family and Topology", y=0.99, fontsize=10)
    fig.text(0.01, 0.01, "N/A indicates that interface localization is not required for that case.", fontsize=6.5)
    return _save_advisor_figure(fig, outdir, "backup2_agent_heatmaps")


def _advisor_talking_points(dataset: dict) -> str:
    overall = {name: dataset["topologies"][name]["overall"] for name in ADVISOR_TOPOLOGIES}
    aggregate = dataset["fault_aggregate"]

    def pct(value: float) -> str:
        return f"{100 * value:.1f}%"

    difficult = ["high_latency", "packet_loss", "packet_corruption", "mtu_mismatch"]
    evidence_lines = [
        f"- {FAULT_LABELS[fault]}：Pingmesh signal {pct(aggregate[fault]['signal_rate'])}，"
        f"Agent verdict {pct(aggregate[fault]['verdict_rate'])}，device {pct(aggregate[fault]['device_rate'])}。"
        for fault in difficult
    ]
    return (
        "\n".join(
            [
                "# NetOpsBench 0.2 五规模结果：中文汇报提纲",
                "",
                "## 30 秒结论",
                "",
                "五个规模的基础设施和 Pingmesh observation 已经稳定；规模扩大后总体得分下降，"
                "但 Agent 一旦确认存在故障，设备定位通常仍然准确。当前主要瓶颈是漏检、搜索不终止、"
                "以及 exact interface/fault taxonomy，而不是 detector 没有提供信号。",
                "",
                "## 图 1：总体质量",
                "",
                f"- 共 {dataset['totals']['operational_cases']} 个有效 observation、"
                f"{dataset['totals']['agent_scored_cases']} 个 Agent-scored cases。",
                "- 20 个 healthy case 全部零正式 Pingmesh anomaly，Agent 也全部判断健康。",
                f"- CLOS diagnosis score：Medium {pct(overall['medium']['primary_reward'])} → "
                f"Large {pct(overall['large']['primary_reward'])} → Xlarge {pct(overall['xlarge']['primary_reward'])}。",
                f"- Fat-tree diagnosis score：K8 {pct(overall['fat-tree-k8']['primary_reward'])} → "
                f"K12 {pct(overall['fat-tree-k12']['primary_reward'])}。",
                "- Interface localization 是最弱指标，大型拓扑约 28%–45%。",
                "",
                "## 图 2：有信号不等于 Agent 能正确结束诊断",
                "",
                *evidence_lines,
                "- BGP/upper-fabric redundancy fault 可以没有 endpoint anomaly；这时需要依赖 BGP、interface 和 config 证据。",
                "- 一旦 Agent 输出 fault_detected，conditional device localization："
                f"Medium {pct(overall['medium']['device_given_detection'])}、"
                f"Large {pct(overall['large']['device_given_detection'])}、"
                f"Xlarge {pct(overall['xlarge']['device_given_detection'])}、"
                f"K8 {pct(overall['fat-tree-k8']['device_given_detection'])}、"
                f"K12 {pct(overall['fat-tree-k12']['device_given_detection'])}。",
                f"- 五个规模共出现 {sum(value['recursion_cases'] for value in overall.values())} 次 "
                "GraphRecursionError，是规模扩大后漏检/inconclusive 的重要来源。",
                f"- Xlarge 的条件设备定位率为 {pct(overall['xlarge']['device_given_detection'])}，"
                "明显低于其他四个规模约 94%–97%，是设备定位层面的主要例外。",
                "",
                "## 图 3：成本",
                "",
                f"- 平均输入从 Medium {overall['medium']['avg_input_tokens']/1000:.0f}K 增长到 "
                f"K12 {overall['fat-tree-k12']['avg_input_tokens']/1000:.0f}K token。",
                f"- 工具调用只在 {min(value['avg_tool_calls'] for value in overall.values()):.1f}–"
                f"{max(value['avg_tool_calls'] for value in overall.values()):.1f} 次之间，"
                "说明成本增长主要来自每次工具返回的数据量和上下文累积。",
                f"- 平均诊断时间从 {min(value['avg_agent_seconds'] for value in overall.values()):.1f} 秒增长到 "
                f"{max(value['avg_agent_seconds'] for value in overall.values()):.1f} 秒。",
                "",
                "## 建议导师追问时的回答",
                "",
                "- 为什么 BGP fault 没有 Pingmesh anomaly？ECMP 仍能转发，属于 redundancy degradation；控制面证据仍然明确。",
                "- 为什么 device localization 总体不高？总体分母包含漏检和 inconclusive；条件定位率显示检出后的设备定位通常很高。",
                "- 下一步优化什么？优先做工具结果压缩、证据优先级和更早停止，再改善 interface attribution；不应继续放宽 detector。",
                "- 这些是不是统计显著性结论？不是；这是同一 0.2 contract 下的 benchmark characterization。",
                "",
                "## 数据口径",
                "",
                "- Fault-level 图采用 case-level micro aggregation。",
                "- K12 的 70 个有效 observation 全部具有 Agent 结果和 ATIF trajectory。",
                "- Interface accuracy 只以 evaluator 标记为 interface-applicable 的 case 为分母。",
            ]
        )
        + "\n"
    )


def _generate_advisor_report(outdir: Path, release_data: dict, manifest_path: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    result_paths, observability_paths, provenance = _load_advisor_manifest(manifest_path)
    dataset = _build_advisor_dataset(release_data, result_paths, observability_paths, provenance)
    (outdir / "presentation_metrics.json").write_text(json.dumps(dataset, indent=2, sort_keys=True), encoding="utf-8")
    _write_advisor_csv(dataset, outdir / "presentation_metrics.csv")
    (outdir / "talking_points_zh.md").write_text(_advisor_talking_points(dataset), encoding="utf-8")
    return [
        _fig_advisor_overall_quality(outdir, dataset),
        _fig_advisor_evidence_to_localization(outdir, dataset),
        _fig_advisor_cost(outdir, dataset),
        _fig_advisor_pingmesh_heatmap(outdir, dataset),
        _fig_advisor_agent_heatmaps(outdir, dataset),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="scenario_results/figures", help="Output directory for figures")
    parser.add_argument(
        "--release-data",
        type=Path,
        default=DEFAULT_RELEASE_DATA,
        help="Machine-readable NetOpsBench 0.2 release result snapshot",
    )
    parser.add_argument(
        "--readme-assets-dir",
        type=Path,
        help="Generate the four public v0.2 release figures in this directory",
    )
    parser.add_argument(
        "--k12-case-data",
        type=Path,
        default=DEFAULT_K12_CASE_DATA,
        help="Machine-readable Fat-tree K=12 case-level result snapshot",
    )
    parser.add_argument(
        "--analysis-data",
        type=Path,
        default=DEFAULT_ANALYSIS_DATA,
        help="Machine-readable all-scale fault-family analysis snapshot",
    )
    parser.add_argument(
        "--advisor-report-dir",
        type=Path,
        help="Generate only the five-topology advisor report package in this directory",
    )
    parser.add_argument(
        "--advisor-manifest",
        type=Path,
        help="JSON manifest mapping each advisor topology to results and observability inputs",
    )
    args = parser.parse_args()

    release_data = _load_release_data(args.release_data)
    if args.readme_assets_dir is not None:
        case_data = _load_k12_case_data(args.k12_case_data, release_data)
        analysis_data = _load_analysis_data(args.analysis_data)
        figures = [
            _fig_readme_release_overview(args.readme_assets_dir, release_data),
            _fig_release_evidence(args.readme_assets_dir, analysis_data),
            _fig_readme_k12_case_map(args.readme_assets_dir, case_data),
            _fig_release_cost(args.readme_assets_dir, release_data),
        ]
        print(f"Generated public release figures in {args.readme_assets_dir}/")
        for figure in figures:
            print(f"  {figure.name}  (+.png)")
        return
    if args.advisor_report_dir is not None:
        if args.advisor_manifest is None:
            parser.error("--advisor-manifest is required with --advisor-report-dir")
        figs = _generate_advisor_report(args.advisor_report_dir, release_data, args.advisor_manifest)
        print(f"Generated advisor report in {args.advisor_report_dir}/")
        for figure in figs:
            print(f"  {figure.name}  (+.png)")
        print("  presentation_metrics.json")
        print("  presentation_metrics.csv")
        print("  talking_points_zh.md")
        return
    if args.advisor_manifest is not None:
        parser.error("--advisor-manifest requires --advisor-report-dir")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    case_data = _load_k12_case_data(args.k12_case_data, release_data)
    analysis_data = _load_analysis_data(args.analysis_data)

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
        _fig_readme_release_overview(outdir, release_data),
        _fig_release_evidence(outdir, analysis_data),
        _fig_readme_k12_case_map(outdir, case_data),
        _fig_release_cost(outdir, release_data),
    ]

    print(f"Generated {len(figs)} figure(s) in {outdir}/")
    for f in figs:
        print(f"  {f.name}  (+.png)")

    # Print summary table
    print()
    print("=" * 80)
    print(
        f"{'Vendor':<10} {'Scale':<8} {'Verdict F1':>10} {'Device Loc':>11} "
        f"{'Intf Loc':>10} {'Avg Score':>10} {'Avg Time(s)':>12}"
    )
    print("-" * 80)
    for vendor in VENDORS:
        for scale in SCALES:
            d = DATA[vendor][scale]
            print(
                f"{vendor:<10} {scale:<8} {d['verdict_f1']:>9.1f}% "
                f"{d['device']:>10.1f}% {d['interface']:>9.1f}% "
                f"{d['avg_score']:>10.3f} {d['avg_time']:>11.1f}s"
            )
        print()


if __name__ == "__main__":
    main()
