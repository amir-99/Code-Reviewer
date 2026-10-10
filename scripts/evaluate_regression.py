#!/usr/bin/env python3
"""Evaluate human-labelled held-out MR results; never fabricate a regression set.

Input: JSON list of {mr, expected:[finding], actual:[finding], proposed_count,
fabricated_count, latency_s}. At least 50 unique MRs required.

A finding is an object {file, line_start, line_end, category} — the location a
reviewer labelled, or the location the service reported. A labelled defect is
found when a reported finding names the same file and category and its line
range overlaps the label's, widened by --tolerance lines. Wording never decides
a match: the same defect described in other words is still the same defect.

A bare string is accepted as a legacy fingerprint and matches only an identical
string, so older result files keep their (stricter) meaning.
"""

import argparse
import json
import sys
from pathlib import Path


def matches(expected, actual, tolerance):
    if isinstance(expected, str) or isinstance(actual, str):
        return expected == actual
    return (
        expected["file"] == actual["file"]
        and expected["category"] == actual["category"]
        and actual["line_start"] <= expected["line_end"] + tolerance
        and expected["line_start"] <= actual["line_end"] + tolerance
    )


def pair(expected, actual, tolerance):
    """Matched (expected, actual) index pairs; each finding is used at most once."""
    used = set()
    pairs = []
    for i, label in enumerate(expected):
        for j, reported in enumerate(actual):
            if j not in used and matches(label, reported, tolerance):
                used.add(j)
                pairs.append((i, j))
                break
    return pairs


def unique(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def evaluate(rows, tolerance=3):
    if len({r["mr"] for r in rows}) < 50:
        raise ValueError("At least 50 distinct held-out MRs are required")
    proposed = sum(r["proposed_count"] for r in rows)
    fabricated = sum(r["fabricated_count"] for r in rows)
    actual = expected = matched = 0
    for r in rows:
        labels, reported = unique(r["expected"]), unique(r["actual"])
        actual += len(reported)
        expected += len(labels)
        matched += len(pair(labels, reported, tolerance))
    precision = matched / actual if actual else 0
    recall = matched / expected if expected else 0
    fabrication = fabricated / proposed if proposed else 0
    latencies = sorted(r["latency_s"] for r in rows)
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    return {
        "mrs": len(rows),
        "precision": precision,
        "recall": recall,
        "fabrication_rate": fabrication,
        "p95_latency_s": p95,
        "passed": precision >= 0.7 and fabrication < 0.03 and p95 < 480,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--tolerance", type=int, default=3)
    args = parser.parse_args()
    result = evaluate(json.loads(args.results.read_text()), args.tolerance)
    if args.baseline:
        baseline = evaluate(json.loads(args.baseline.read_text()), args.tolerance)
        result["passed"] &= result["precision"] >= baseline["precision"]
        result["baseline_recall"] = baseline["recall"]
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
