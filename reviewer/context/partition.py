import re
from pathlib import PurePosixPath

from pydantic import BaseModel

from reviewer.context.models import DiffLine

# A displayed diff line: right-aligned new line number (blank for a removed
# line), a marker (+ added, - removed, space for unchanged), then the text.
NUMBERED = re.compile(r"^ *(\d+) [+ ] ", re.M)
GAP = "  ..."
SIGN = {"added": "+", "removed": "-", "context": " "}


class WorkUnit(BaseModel):
    id: str
    paths: list[str]
    content: str
    omitted: bool = False
    file_context: dict[str, dict] = {}
    max_findings: int = 3


def render(line):
    number = "" if line.new_line is None else str(line.new_line)
    return f"{number:>5} {SIGN[line.kind]} {line.text}"


def header(file):
    renamed = (
        f" (renamed from {file.old_path})" if file.change_type == "renamed" else ""
    )
    return f"=== {file.path}{renamed} ==="


def cap(content):
    """Findings allowed for a unit: three, plus one per further 8 KB, at most 8."""
    return min(8, 3 + len(content.encode()) // 8000)


def hunk_lines(file):
    """The file's diff lines split into hunks, using the hunk line counts."""
    hunks, offset = [], 0
    for hunk in file.hunks:
        old_left, new_left = hunk.old_lines or 0, hunk.new_lines or 0
        start = offset
        while offset < len(file.lines) and (old_left > 0 or new_left > 0):
            line = file.lines[offset]
            old_left -= line.kind != "added"
            new_left -= line.kind != "removed"
            offset += 1
        hunks.append(file.lines[start:offset])
    if offset < len(file.lines):
        hunks.append(file.lines[offset:])
    return [h for h in hunks if h]


def segments(file):
    """Hunks widened by enclosing scope lines, as (first, last, lines) runs.

    Scope lines fill toward the enclosing definition's edges, so the reader
    sees whole functions instead of fragments. Runs stay separate so packing
    keeps each hunk intact; first/last are new line numbers (None when a run
    has none) and tell the packer whether lines were omitted between runs.
    """
    scope = file.scope

    def context(n):
        return DiffLine(text=scope[n], new_line=n, old_line=None, kind="context")

    runs = []  # (first number, last number, lines)
    for hunk in hunk_lines(file):
        numbers = [line.new_line for line in hunk if line.new_line is not None]
        if not numbers:
            if runs:
                runs[-1][2].extend(hunk)
            else:
                runs.append([None, None, list(hunk)])
            continue
        floor = max((r[1] for r in runs if r[1] is not None), default=0)
        low, high = numbers[0], numbers[-1]
        while low - 1 > floor and low - 1 in scope:
            low -= 1
        while high + 1 in scope:
            high += 1
        lines = (
            [context(n) for n in range(low, numbers[0])]
            + list(hunk)
            + [context(n) for n in range(numbers[-1] + 1, high + 1)]
        )
        runs.append([low, high, lines])
    return runs


def file_chunks(file, limit):
    """Pack whole runs, splitting an oversized run only between complete lines.

    Returns lists of rendered lines; GAP marks lines omitted between runs. A
    complete definition that fits a fresh chunk starts one rather than being
    split because preceding code consumed the allowance.
    """
    runs = segments(file)
    if not runs:
        return [[f"{file.path}: binary or rename-only change; no textual hunks"]]
    chunks, pending = [], []
    previous = None

    def size(lines):
        return len("\n".join(lines).encode())

    for first, last, run in runs:
        rendered = [render(line) for line in run]
        adjacent = previous is not None and first is not None and previous + 1 == first
        previous = last if last is not None else previous
        joined = pending + ([GAP] if pending and not adjacent else []) + rendered
        if size(joined) <= limit:
            pending = joined
            continue
        if pending:
            chunks.append(pending)
            pending = []
        for position, line in enumerate(run):
            value = rendered[position]
            ends = [end for start, end in file.symbol_ranges if start == line.new_line]
            if pending and ends:
                symbol = [
                    rendered[i]
                    for i in range(position, len(run))
                    if run[i].new_line is None or run[i].new_line <= max(ends)
                ]
                if size(symbol) <= limit < size(pending + symbol):
                    chunks.append(pending)
                    pending = []
            if pending and size(pending + [value]) > limit:
                chunks.append(pending)
                pending = []
            pending.append(value)
    if pending:
        chunks.append(pending)
    return chunks


def subject(path):
    """The source file a test file exercises, by name: `test_x.py` -> `x`."""
    name = PurePosixPath(path).name
    stem = name.split(".", 1)[0]
    return re.sub(r"^test_|_test$", "", stem)


def is_test(path):
    lower = path.lower()
    name = PurePosixPath(lower).name
    return (
        name.startswith("test_")
        or name.split(".", 1)[0].endswith("_test")
        or re.search(r"\.(test|spec)\.[jt]sx?$", name) is not None
        or any(p in {"test", "tests", "__tests__"} for p in lower.split("/")[:-1])
    )


def ordered(files):
    """Files with each test file placed directly after the source it exercises."""
    sources = {subject(f.path): f.path for f in files if not is_test(f.path)}
    return sorted(
        files,
        key=lambda f: (
            sources.get(subject(f.path), f.path) if is_test(f.path) else f.path,
            is_test(f.path),
            f.path,
        ),
    )


def context_for(file, section, omitted, index, count):
    numbers = [int(n) for n in NUMBERED.findall(section)]
    ranges = []
    for n in numbers:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return {
        "total_lines": file.total_lines,
        "supplied_new_ranges": ranges,
        "complete_file": bool(
            file.total_lines is not None
            and ranges == [[1, file.total_lines]]
            and not omitted
        ),
        "has_more_before": not numbers or numbers[0] > 1,
        "has_more_after": (
            None
            if file.total_lines is None
            else not numbers or numbers[-1] < file.total_lines
        ),
        "chunk_index": index,
        "chunk_count": count,
    }


def coherent_units(bundle, limit, paths=None):
    """Units of whole runs of one file, or several small related files packed.

    `paths` restricts the units to those files (an incremental re-review), so
    packing never pulls an unaffected file back into review. No source program
    is loaded or executed here.
    """
    files = ordered(
        [
            f
            for f in bundle.code.files
            if not f.is_excluded and (paths is None or f.path in paths)
        ]
    )
    units = []
    packed = []  # (file, section) pairs waiting to share one unit

    def flush():
        if not packed:
            return
        content = "\n".join(section for _, section in packed)
        units.append(
            WorkUnit(
                id=f"{packed[0][0].path}:unit-0",
                paths=[f.path for f, _ in packed],
                content=content,
                max_findings=cap(content),
                file_context={
                    f.path: context_for(f, section, False, 0, 1)
                    for f, section in packed
                },
            )
        )
        packed.clear()

    for file in files:
        title = header(file)
        chunks = file_chunks(file, max(1, limit - len(title.encode()) - 1))
        sections = ["\n".join([title] + chunk) for chunk in chunks]
        oversized = [len(section.encode()) > limit for section in sections]
        if len(sections) == 1 and not oversized[0]:
            current = "\n".join(
                section for _, section in packed + [(file, sections[0])]
            )
            if len(current.encode()) > limit:
                flush()
            packed.append((file, sections[0]))
            continue
        flush()
        for part, section in enumerate(sections):
            content = (
                section
                if not oversized[part]
                else title
                + "\nA source line exceeds the unit budget; examination omitted."
            )
            units.append(
                WorkUnit(
                    id=f"{file.path}:unit-{part}",
                    paths=[file.path],
                    content=content,
                    omitted=oversized[part],
                    max_findings=cap(content),
                    file_context={
                        file.path: context_for(
                            file, section, oversized[part], part, len(sections)
                        )
                    },
                )
            )
    flush()
    return units


def partition(bundle, limit=24000, paths=None):
    # UTF-8 bytes form a conservative token ceiling.
    return coherent_units(bundle, max(256, limit), paths)
