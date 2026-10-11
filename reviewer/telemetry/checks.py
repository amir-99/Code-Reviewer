"""Live progress of a sandboxed check: what it printed, as it runs, and how it ended.

A static analyzer or build command reports through `check` activity rows
nested under its tool row: a `running` row when it starts, batches of output
lines while it runs, and a final row with its status, exit code and duration.

Output is recorded only through the run's redactor. Without one, the rows say
how the check ended and nothing it printed. Lines are stripped of terminal
escapes and bounded in length and number, so a chatty tool cannot flood the
activity table; the complete tail stays on the review's snapshot as before.
"""

import re
from time import monotonic
from uuid import uuid4

from reviewer.telemetry.activity import parent, record

ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]|\r")
LINE_CHARS = 400  # A line longer than this is cut, with a marker.
BATCH_LINES = 40  # Write as soon as this many lines wait...
FLUSH_S = 2.0  # ...or this long has passed since the last write.
MAX_LINES = 400  # Lines streamed per check; the rest are only counted.


def clean(line):
    line = ESCAPES.sub("", line).rstrip()
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + " …"


class CheckOutput:
    def __init__(self, name, redact=None):
        self.name = name
        self.redact = redact
        self.activity_id = str(uuid4())
        # Created inside the tool's own activity, so it nests under that row.
        self.parent_id = parent.get()
        self.partial = ""
        self.waiting = []
        self.sent = 0
        self.dropped = 0
        self.written = monotonic()

    def row(self, status, **extra):
        return dict(
            name=self.name,
            status=status,
            activity_id=self.activity_id,
            parent_id=self.parent_id,
            **extra,
        )

    async def start(self):
        await record("check", self.row("running", lines=[]))

    async def feed(self, text):
        """Take a chunk of output; write complete lines when a batch is due."""
        if self.redact is None or not text:
            return
        *complete, self.partial = (self.partial + text).split("\n")
        self.waiting.extend(complete)
        if self.waiting and (
            len(self.waiting) >= BATCH_LINES or monotonic() - self.written >= FLUSH_S
        ):
            await record("check", self.row("running", lines=self.take()))

    async def finish(self, status, exit_code=None, duration_s=None):
        if self.redact is not None and self.partial:
            self.waiting.append(self.partial)
        self.partial = ""
        extra = {"exit_code": exit_code}
        if duration_s is not None:
            extra["duration_ms"] = round(duration_s * 1000)
        lines = self.take()
        if self.dropped:
            extra["truncated"] = self.dropped
        await record("check", self.row(status, lines=lines, **extra))

    def take(self):
        lines, self.waiting = self.waiting, []
        self.written = monotonic()
        if self.redact is not None and lines:
            # Redacted as one block, so a secret is replaced wherever it falls.
            lines = self.redact("\n".join(lines)).split("\n")
        lines = [clean(line) for line in lines]
        room = max(MAX_LINES - self.sent, 0)
        self.dropped += max(len(lines) - room, 0)
        lines = lines[:room]
        self.sent += len(lines)
        return lines


TAIL_LINES = 80  # Lines of a finished check's stored tail the detail shows.


def summary(result):
    """A finished check as the review detail shows it, from the run's snapshot.

    The stored tail was redacted when the run saved it; it is cleaned and cut
    to its last lines here, which is where a tool's verdict usually is.
    """
    tail = [clean(line) for line in (result.get("stdout_tail") or "").split("\n")]
    while tail and not tail[-1]:
        tail.pop()
    duration = result.get("duration_s")
    return {
        "name": result.get("name"),
        "status": result.get("status"),
        "required": bool(result.get("required")),
        "exit_code": result.get("exit_code"),
        "duration_ms": round(duration * 1000) if duration is not None else None,
        "lines": tail[-TAIL_LINES:],
        "truncated": max(len(tail) - TAIL_LINES, 0),
    }
