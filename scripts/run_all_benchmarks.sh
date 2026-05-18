#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# run_all_benchmarks.sh — 批量跑 benchmark，按 scale 逐个执行
#
# 用法:
#   nohup bash scripts/run_all_benchmarks.sh &> benchmark_run.log &
#   # 或
#   bash scripts/run_all_benchmarks.sh          # 前台跑
#
# 每个 scale 使用不同并发 worker 数:
#   xs:     6 workers  (12 scenarios, 6×6=36 containers)
#   small:  3 workers  (12 scenarios, 3×14=42 containers)
#   medium: 2 workers  (24 scenarios, 2×28=56 containers)
#   large:  2 workers  (48 scenarios, 2×84=168 containers)
#
# 跑完后汇总 CSV 写入 scenario_results/benchmark_summary_<ts>.csv
# ─────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── 配置 ────────────────────────────────────────────────────
# Locate project Python interpreter (prefers repo venv, falls back to system python3).
# For git worktrees sharing a venv, override via: export NETOPSBENCH_PYTHON=/path/to/venv/bin/python3
# shellcheck source=scripts/lib/find_python.sh
source "$REPO_ROOT/scripts/lib/find_python.sh"
VENDOR="${BENCH_VENDOR:-minimax}"

# scale → worker count
declare -A SCALE_WORKERS=( [xs]=6 [small]=3 [medium]=2 [large]=2 )
# Deploy workers sequentially for large to avoid 40 simultaneous SONiC-VS boots
# xs reduced from 6→2 to avoid Docker API overload when many workers deploy in parallel
declare -A SCALE_DEPLOY_JOBS=( [xs]=2 [small]=2 [medium]=2 [large]=1 )
# Health check BGP retries: large needs more time (16 leafs vs 4 for medium)
declare -A SCALE_HEALTH_RETRIES=( [xs]=12 [small]=12 [medium]=12 [large]=36 )
# Ordered list of scales to run (override with BENCH_SCALES="xs small")
IFS=' ' read -ra SCALES <<< "${BENCH_SCALES:-xs small medium large}"

# 加载 API keys
set -a
source "${REPO_ROOT}/.env"
set +a

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="${REPO_ROOT}/scenario_results/benchmark_logs_${TIMESTAMP}"
mkdir -p "$LOG_DIR"

# ── Helper: clean all clab resources ────────────────────────
cleanup_clab() {
    # Preserve worker deploy logs before cleaning runtimes
    local runtime_logs_dir="$REPO_ROOT/.netopsbench/runtimes"
    if [ -d "$runtime_logs_dir" ] && [ -n "${LOG_DIR:-}" ]; then
        for logdir in "$runtime_logs_dir"/*/logs; do
            [ -d "$logdir" ] && cp -a "$logdir" "${LOG_DIR}/$(basename "$(dirname "$logdir")")_deploy_logs" 2>/dev/null || true
        done
    fi
    for cid in $(sudo docker ps -aq --filter "name=clab-" 2>/dev/null); do
        sudo docker rm -f "$cid" &>/dev/null || true
    done
    # Also remove worker telegraf containers (not prefixed with clab-)
    for cid in $(sudo docker ps -aq --filter "name=telegraf-" 2>/dev/null); do
        sudo docker rm -f "$cid" &>/dev/null || true
    done
    for net in $(sudo docker network ls --filter "name=clab-mgmt-" -q 2>/dev/null); do
        for ep in $(sudo docker network inspect "$net" --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null); do
            sudo docker network disconnect -f "$net" "$ep" &>/dev/null || true
        done
        sudo docker network rm "$net" &>/dev/null || true
    done
    sudo rm -rf "$REPO_ROOT/.netopsbench/runtimes/"* 2>/dev/null || true
}

# ── 执行计划 ────────────────────────────────────────────────
total_scenarios=0
echo "======================================================"
echo " NetOpsBench 批量 Benchmark  (${TIMESTAMP})"
echo " Vendor:  ${VENDOR}"
echo " Plan:"
for scale in "${SCALES[@]}"; do
    w="${SCALE_WORKERS[$scale]:-1}"
    n=$(find "${REPO_ROOT}/scenarios/generated/${scale}" -name '*.yaml' 2>/dev/null | wc -l)
    total_scenarios=$((total_scenarios + n))
    echo "   ${scale}: ${n} scenarios × ${w} workers"
done
echo " Total:   ${total_scenarios} scenarios"
echo " Logs:    ${LOG_DIR}"
echo "======================================================"

# Clean stale data
sudo rm -rf "$REPO_ROOT/.netopsbench/runs/"* 2>/dev/null || true
cleanup_clab

# ── 逐 scale 执行 ──────────────────────────────────────────
failed=0
for scale in "${SCALES[@]}"; do
    workers="${SCALE_WORKERS[$scale]:-1}"
    label="${VENDOR}_${scale}"
    log_file="${LOG_DIR}/${label}.log"
    echo ""
    echo "[$(date '+%H:%M:%S')] >>> ${label}: workers=${workers}"

    if NETOPSBENCH_WORKER_DEPLOY_JOBS="${SCALE_DEPLOY_JOBS[$scale]:-${workers}}" \
       NETOPSBENCH_WORKER_HEALTH_RETRIES="${SCALE_HEALTH_RETRIES[$scale]:-12}" \
       PYTHONPATH="$REPO_ROOT" "$PYTHON" examples/03_run_scale_benchmark.py \
            --vendor "$VENDOR" --scale "$scale" --workers "$workers" \
            > "$log_file" 2>&1; then
        echo "[$(date '+%H:%M:%S')]  ✓  ${label} — 完成"
    else
        echo "[$(date '+%H:%M:%S')]  ✗  ${label} — 失败 (see ${log_file})"
        failed=$((failed + 1))
    fi

    # Clean up between scales
    cleanup_clab
done

echo ""
echo "======================================================"
echo " 全部执行完毕  (失败: ${failed})"
echo "======================================================"

# ── 汇总结果 ────────────────────────────────────────────────
# 找最新的 run 目录，提取 report.json 的 summary 字段
SUMMARY_CSV="${REPO_ROOT}/scenario_results/benchmark_summary_${TIMESTAMP}.csv"

"$PYTHON" - "$REPO_ROOT" "$TIMESTAMP" <<'PYEOF'
import json, sys, csv
from pathlib import Path
from datetime import datetime

repo = Path(sys.argv[1])
ts = sys.argv[2]
results_dir = repo / ".netopsbench" / "runs"

# Collect all report.json files
reports = []
for rj in sorted(results_dir.rglob("report.json")):
    try:
        data = json.loads(rj.read_text())
    except Exception:
        continue
    s = data.get("summary", {})
    if not s:
        continue

    # Extract vendor/model from detailed_results
    vendor = "unknown"
    model = "unknown"
    dr = data.get("detailed_results", [])
    if dr:
        agent_out = dr[0].get("details", {}).get("agent_output", {})
        meta = agent_out.get("metadata", {})
        vendor = meta.get("provider") or vendor
        model = meta.get("model") or model

    # Prefer report-provided topology_scale; fall back to scenario ids.
    scale = data.get("topology_scale") or s.get("topology_scale") or "unknown"
    if scale == "unknown":
        sid = ""
        if dr:
            sid = dr[0].get("details", {}).get("scenario_id", "") or dr[0].get("scenario_id", "")
        if not sid:
            raw_ids = (data.get("raw") or {}).get("scenario_ids") or []
            sid = raw_ids[0] if raw_ids else ""
        for sc in ("xs", "small", "medium", "large"):
            if f"_{sc}_" in sid:
                scale = sc
                break

    reports.append({
        "run_dir": str(rj.parent.relative_to(results_dir)),
        "vendor": vendor,
        "model": model,
        "scale": scale,
        "total_cases": s.get("total_cases", 0),
        "overall_accuracy": s.get("overall_accuracy", 0),
        "detection_accuracy": s.get("detection_accuracy", 0),
        "detection_recall": s.get("detection_recall", 0),
        "detection_f1": s.get("detection_f1", 0),
        "detection_macro_f1": s.get("detection_macro_f1"),
        "device_localization_rate": s.get("device_localization_rate", s.get("device_accuracy", 0)),
        "fault_type_accuracy": s.get("fault_type_accuracy", 0),
        "interface_localization_rate": s.get("interface_localization_rate", 0),
        "localization_composite_score": s.get("localization_composite_score", 0),
        "avg_score": s.get("average_score", 0),
        "avg_time_s": s.get("avg_time_seconds", 0),
        "avg_tool_calls": s.get("avg_tool_calls", 0),
        "avg_input_tokens": s.get("avg_input_tokens_per_case", 0),
        "avg_output_tokens": s.get("avg_output_tokens_per_case", 0),
        "total_input_tokens": s.get("total_input_tokens", 0),
        "total_output_tokens": s.get("total_output_tokens", 0),
        "status": s.get("status", ""),
        "started_at": s.get("started_at", ""),
    })

if not reports:
    print("No report.json files found.")
    sys.exit(0)

# Sort by vendor, scale
scale_order = {"xs": 0, "small": 1, "medium": 2, "large": 3}
reports.sort(key=lambda r: (r["vendor"], scale_order.get(r["scale"], 99), r["started_at"]))

# Write CSV
csv_path = repo / "scenario_results" / f"benchmark_summary_{ts}.csv"
fields = list(reports[0].keys())
with open(csv_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(reports)

# Print table
print()
print("=" * 129)
print(f" Benchmark Summary — {len(reports)} runs")
print("=" * 129)
header = f"{'vendor':<10} {'model':<16} {'scale':<8} {'cases':>5} {'overall':>8} {'recall':>8} {'macro_f1':>9} {'device':>8} {'fault':>8} {'intf':>8} {'score':>8} {'time':>7} {'tools':>6} {'in_tok':>8} {'out_tok':>8}"
print(header)
print("-" * 129)
for r in reports:
    print(f"{r['vendor']:<10} {r['model']:<16} {r['scale']:<8} "
          f"{r['total_cases']:>5} "
          f"{r['overall_accuracy']:>7.1%} "
          f"{r['detection_recall']:>7.1%} "
          f"{(r['detection_macro_f1'] or 0):>8.3f} "
            f"{r['device_localization_rate']:>7.1%} "
          f"{r['fault_type_accuracy']:>7.1%} "
          f"{r['interface_localization_rate']:>7.1%} "
          f"{r['avg_score']:>8.3f} "
          f"{r['avg_time_s']:>6.1f}s "
          f"{r['avg_tool_calls']:>5.1f} "
          f"{r['avg_input_tokens']:>8.0f} "
          f"{r['avg_output_tokens']:>8.0f}")
print("-" * 129)

# Per-vendor average
for v in sorted(set(r["vendor"] for r in reports)):
    vr = [r for r in reports if r["vendor"] == v]
    n = sum(r["total_cases"] for r in vr)
    if n == 0:
        continue
    # Weighted average by total_cases
    def wavg(key):
        return sum((r[key] or 0) * r["total_cases"] for r in vr) / n
    print(f"{'['+v+']':<10} {'AVERAGE':<16} {'all':<8} "
          f"{n:>5} "
          f"{wavg('overall_accuracy'):>7.1%} "
          f"{wavg('detection_recall'):>7.1%} "
          f"{wavg('detection_macro_f1'):>8.3f} "
            f"{wavg('device_localization_rate'):>7.1%} "
          f"{wavg('fault_type_accuracy'):>7.1%} "
          f"{wavg('interface_localization_rate'):>7.1%} "
          f"{wavg('avg_score'):>8.3f} "
          f"{wavg('avg_time_s'):>6.1f}s "
          f"{wavg('avg_tool_calls'):>5.1f} "
          f"{wavg('avg_input_tokens'):>8.0f} "
          f"{wavg('avg_output_tokens'):>8.0f}")
print("=" * 129)
print(f"\nCSV saved to: {csv_path}")
PYEOF

echo ""
echo "Done. Summary CSV: ${SUMMARY_CSV}"
