#!/usr/bin/env python3
"""Evaluate human-labelled held-out MR results; never fabricate a regression set.

Input: JSON list of {mr, expected:[fingerprint], actual:[fingerprint],
proposed_count, fabricated_count, latency_s}. At least 50 unique MRs required.
"""

import argparse
import json
import sys
from pathlib import Path


def evaluate(rows):
    if len({r["mr"] for r in rows}) < 50:
        raise ValueError("At least 50 distinct held-out MRs are required")
    proposed = sum(r["proposed_count"] for r in rows)
    fabricated = sum(r["fabricated_count"] for r in rows)
    actual = sum(len(set(r["actual"])) for r in rows)
    matched = sum(len(set(r["actual"]) & set(r["expected"])) for r in rows)
    precision = matched / actual if actual else 0
    fabrication = fabricated / proposed if proposed else 0
    latencies = sorted(r["latency_s"] for r in rows)
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    return {
        "mrs": len(rows),
        "precision": precision,
        "fabrication_rate": fabrication,
        "p95_latency_s": p95,
        "passed": precision >= 0.7 and fabrication < 0.03 and p95 < 480,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.results.read_text()))
    if args.baseline:
        baseline = evaluate(json.loads(args.baseline.read_text()))
        result["passed"] &= result["precision"] >= baseline["precision"]
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
