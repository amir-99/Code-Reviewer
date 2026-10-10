import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "evaluate_regression",
    Path(__file__).parents[2] / "scripts" / "evaluate_regression.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def finding(file="a.py", start=10, end=12, category="correctness"):
    return dict(file=file, line_start=start, line_end=end, category=category)


def rows(expected, actual):
    return [
        dict(
            mr=i,
            expected=expected,
            actual=actual,
            proposed_count=len(actual),
            fabricated_count=0,
            latency_s=60,
        )
        for i in range(50)
    ]


def test_location_match_ignores_claim_wording():
    result = module.evaluate(rows([finding()], [finding(start=11, end=11)]))
    assert result["precision"] == 1
    assert result["recall"] == 1


def test_other_file_category_or_distant_lines_do_not_match():
    actual = [
        finding(file="b.py"),
        finding(category="security"),
        finding(start=40, end=41),
    ]
    result = module.evaluate(rows([finding()], actual))
    assert result["precision"] == 0
    assert result["recall"] == 0


def test_one_report_cannot_satisfy_two_labels():
    result = module.evaluate(rows([finding(), finding(start=11)], [finding()]))
    assert result["precision"] == 1
    assert result["recall"] == 0.5


def test_legacy_fingerprints_match_exactly():
    result = module.evaluate(rows(["abc", "def"], ["abc"]))
    assert result["precision"] == 1
    assert result["recall"] == 0.5


def test_requires_fifty_merge_requests():
    with pytest.raises(ValueError):
        module.evaluate(rows([], [])[:49])
