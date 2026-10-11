"""Check output: streamed redacted and bounded, or not at all without a redactor."""

from types import SimpleNamespace

import pytest

from reviewer.context.redaction import Redactor
from reviewer.services.static.runner import StaticRunner
from reviewer.telemetry import checks
from reviewer.telemetry.activity import sink
from reviewer.telemetry.checks import CheckOutput, summary


class Rows:
    def __init__(self):
        self.rows = []

    async def append_event(self, review_id, kind, data):
        self.rows.append((kind, data))

    def of(self, kind):
        return [data for k, data in self.rows if k == kind]


@pytest.fixture
def rows():
    store = Rows()
    token = sink.set((store, "review"))
    yield store
    sink.reset(token)


async def test_output_streams_redacted_in_batches_with_a_final_status(rows):
    check = CheckOutput("ruff", Redactor().text)
    await check.start()
    # A batch is due once enough lines wait; a partial line waits for its end.
    lines = "".join(f"a.py:{n}:1: E501 line too long\n" for n in range(40))
    await check.feed(lines + "api_key = 'abcdefghijklmnop12345' \x1b[31mred")
    await check.feed("\x1b[0m\n")
    await check.finish("failed", 1, 0.25)
    running, batch, final = rows.of("check")
    assert running["status"] == "running" and running["lines"] == []
    assert (
        len(batch["lines"]) == 40
        and batch["lines"][0] == "a.py:0:1: E501 line too long"
    )
    assert final["status"] == "failed" and final["exit_code"] == 1
    assert final["duration_ms"] == 250
    [last] = final["lines"]
    assert "abcdefghijklmnop12345" not in last and "[REDACTED:credential]" in last
    assert "\x1b" not in last and last.endswith("red")
    assert {r["activity_id"] for r in (running, batch, final)} == {check.activity_id}


async def test_nothing_printed_is_recorded_without_a_redactor(rows):
    check = CheckOutput("go vet")
    await check.start()
    await check.feed("secret output\n" * 50)
    await check.finish("passed", 0, 1.0)
    assert all(row["lines"] == [] for row in rows.of("check"))
    assert rows.of("check")[-1]["status"] == "passed"


async def test_long_and_endless_output_is_bounded(rows, monkeypatch):
    monkeypatch.setattr(checks, "MAX_LINES", 5)
    check = CheckOutput("eslint", lambda text: text)
    await check.feed("x" * 1000 + "\n" + "line\n" * 9)
    await check.finish("failed", 1)
    streamed = [line for row in rows.of("check") for line in row["lines"]]
    assert len(streamed) == 5
    assert streamed[0].endswith(" …") and len(streamed[0]) < 410
    assert rows.of("check")[-1]["truncated"] == 5


async def test_disabled_analyzer_reports_its_name_and_skip(rows):
    tool = SimpleNamespace(name="semgrep", required=False, timeout_s=5)
    result = await StaticRunner(enabled=False).run(tool, None, redact=str)
    assert result.status == "skipped"
    [started, *_] = rows.of("tool")
    assert started["name"] == "Static analyzer: semgrep"
    final = rows.of("check")[-1]
    assert final["name"] == "semgrep" and final["status"] == "skipped"
    # Nested under the analyzer's own row.
    assert final["parent_id"] == started["activity_id"]


def test_finished_checks_show_the_end_of_their_stored_tail():
    result = {
        "name": "build: go build ./...",
        "status": "failed",
        "required": True,
        "exit_code": 2,
        "duration_s": 1.5,
        "stdout_tail": "\n".join(f"line {n}" for n in range(100)) + "\n\n",
    }
    view = summary(result)
    assert view["lines"][-1] == "line 99" and len(view["lines"]) == 80
    assert view["truncated"] == 20 and view["duration_ms"] == 1500
