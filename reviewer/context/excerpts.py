"""Bounded, numbered source excerpts with explicit continuation information."""


def excerpt(lines, start=1, end=None, limit=16000):
    total = len(lines)
    requested_end = total if end is None else end
    stop = min(total, requested_end)
    selected = []
    size = 0
    for number in range(start, stop + 1):
        value = f"{number}: {lines[number - 1]}"
        size += len(value.encode()) + 1
        if size > limit:
            break
        selected.append(value)
    last = start + len(selected) - 1
    return {
        "code": "\n".join(selected),
        "line_start": start,
        "line_end": last,
        "total_lines": total,
        "has_more_before": start > 1,
        "has_more_after": last < total,
        "complete_file": start == 1 and last == total,
        "range_complete": 1 <= start <= max(1, total)
        and requested_end <= total
        and last == requested_end,
    }


def shorten(value):
    """Halve a rendered excerpt on intact line boundaries, retaining its scope."""
    value = dict(value)
    lines = value["code"].splitlines()
    value["code"] = "\n".join(lines[: len(lines) // 2])
    value["line_end"] = value["line_start"] + len(lines) // 2 - 1
    value.update(complete_file=False, range_complete=False, has_more_after=True)
    return value
