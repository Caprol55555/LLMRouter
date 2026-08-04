"""Aggregate benchmark_pipeline/results/*.json into markdown tables.

Usage:
    python aggregate_results.py                # writes results/tables.md
    python aggregate_results.py --csv          # also writes results/results.csv
"""

import argparse
import glob
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")

DATASET_ORDER = [
    "llmrouter_generic", "memory_locomo", "memory_longmemeval", "timeseries",
    "video", "multimodal_geometry3k", "multimodal_mathvista", "personalized",
]


def load_all():
    out = []
    for f in glob.glob(os.path.join(RESULTS_DIR, "*.json")):
        with open(f) as fh:
            out.append(json.load(fh))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    results = load_all()
    if not results:
        raise SystemExit("no results found; run run_pipeline.py first")

    weights = sorted({(r["alpha"], r["beta"]) for r in results}, key=lambda w: -w[0])
    datasets = [d for d in DATASET_ORDER
                if any(r["dataset"] == d for r in results)]

    lines = ["# Benchmark Pipeline Results", ""]

    # Per-dataset tables, one per weight setting
    for ds in datasets:
        lines += [f"## {ds}", ""]
        for (a, b) in weights:
            rows = [r for r in results if r["dataset"] == ds
                    and r["alpha"] == a and r["beta"] == b]
            if not rows:
                continue
            rows.sort(key=lambda r: -(r.get("avg_reward") or 0))
            lines += [f"### alpha={a}, beta={b}", "",
                      "| Router | Performance | Token Cost | Price Cost ($) | Reward |",
                      "|--------|------------|-----------|---------------|--------|"]
            for r in rows:
                lines.append(f"| {r['router']} | {r['avg_performance']:.4f} | "
                             f"{r['avg_token_cost']:.1f} | {r['avg_price_cost']:.8f} | "
                             f"{r['avg_reward']:.4f} |")
            lines.append("")

    # Overall ranking per weight setting
    lines += ["## Overall ranking (averaged across datasets)", ""]
    for (a, b) in weights:
        agg = defaultdict(lambda: {"perf": [], "cost": [], "reward": []})
        for r in results:
            if r["alpha"] == a and r["beta"] == b:
                agg[r["router"]]["perf"].append(r["avg_performance"])
                agg[r["router"]]["cost"].append(r["avg_price_cost"])
                agg[r["router"]]["reward"].append(r["avg_reward"])
        ranked = sorted(agg.items(), key=lambda kv: -sum(kv[1]["reward"]) / len(kv[1]["reward"]))
        lines += [f"### alpha={a}, beta={b}", "",
                  "| Rank | Router | Avg Performance | Avg Price Cost ($) | Avg Reward | #Datasets |",
                  "|------|--------|----------------|-------------------|-----------|-----------|"]
        for i, (router, s) in enumerate(ranked, 1):
            n = len(s["perf"])
            lines.append(f"| {i} | {router} | {sum(s['perf'])/n:.4f} | "
                         f"{sum(s['cost'])/n:.8f} | {sum(s['reward'])/n:.4f} | {n} |")
        lines.append("")

    out_md = os.path.join(RESULTS_DIR, "tables.md")
    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote", out_md)

    if args.csv:
        import csv
        out_csv = os.path.join(RESULTS_DIR, "results.csv")
        keys = ["dataset", "router", "alpha", "beta", "avg_performance",
                "avg_token_cost", "avg_price_cost", "avg_reward", "num_queries"]
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in sorted(results, key=lambda r: (r["dataset"], r["router"], -r["alpha"])):
                w.writerow(r)
        print("wrote", out_csv)


if __name__ == "__main__":
    main()
