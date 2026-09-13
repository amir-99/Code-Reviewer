import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel


class WorkUnit(BaseModel):
    id: str
    paths: list[str]
    content: str
    omitted: bool = False
    representation: str = "diff_excerpt"
    file_context: dict[str, dict] = {}


def _partition(bundle, kind="file_group", limit=6000):
    # UTF-8 bytes form a conservative token ceiling.
    limit = max(256, limit)
    if kind == "hunks":
        return coherent_units(bundle, limit)
    files = [f for f in bundle.code.files if not f.is_excluded]
    if kind == "whole_change":
        outline = "\n".join(
            f"{f.path} ({f.change_type}): " + "; ".join(h.header for h in f.hunks)
            for f in files
        )
        if len(outline.encode()) > limit:
            outline = "\n".join(
                f"{f.path} ({f.change_type}, {len(f.hunks)} hunks)" for f in files
            )
        if len(outline.encode()) > limit:
            bundle.degradations.append("whole_change_summary_truncated")
            outline = outline.encode()[:limit].decode(errors="ignore")
        return (
            [
                WorkUnit(
                    id="change:unit-0", paths=[f.path for f in files], content=outline
                )
            ]
            if files
            else []
        )
    return coherent_units(bundle, limit, collaborators=kind == "file_group")


def coherent_units(bundle, limit, collaborators=True):
    """Pack whole hunks, splitting oversized hunks only between complete lines.

    Diff context already carries the configured surrounding lines. Related changed
    tests/imports are optional evidence, attached only when they fit the unit.
    No source program is loaded or executed here.
    """
    files = [f for f in bundle.code.files if not f.is_excluded]
    units = []
    for file in files:
        lines = [
            f"old={line.old_line} new={line.new_line} {line.kind}: {line.text}"
            for line in file.lines
        ]
        hunks = []
        offset = 0
        for hunk in file.hunks:
            old_left, new_left = hunk.old_lines or 0, hunk.new_lines or 0
            start = offset
            while offset < len(lines) and (old_left > 0 or new_left > 0):
                line = file.lines[offset]
                old_left -= line.kind != "added"
                new_left -= line.kind != "removed"
                offset += 1
            hunks.append(lines[start:offset])
        if offset < len(lines):
            hunks.append(lines[offset:])
        if not lines:
            hunks = [[f"{file.path}: binary or rename-only change; no textual hunks"]]
        chunks, pending = [], []
        for hunk in hunks:
            if len("\n".join(pending + hunk).encode()) <= limit:
                pending.extend(hunk)
                continue
            if pending:
                chunks.append(pending)
                pending = []
            for position, line in enumerate(hunk):
                match = re.match(r"old=\S+ new=(\d+) ", line)
                number = int(match[1]) if match else None
                # Start a fitting complete symbol in a fresh chunk rather than
                # splitting it because preceding code consumed the allowance.
                ends = [end for start, end in file.symbol_ranges if start == number]
                if pending and ends:
                    symbol = []
                    for candidate in hunk[position:]:
                        found = re.match(r"old=\S+ new=(\d+) ", candidate)
                        if found and int(found[1]) > max(ends):
                            break
                        symbol.append(candidate)
                    if (
                        len("\n".join(symbol).encode()) <= limit
                        and len("\n".join(pending + symbol).encode()) > limit
                    ):
                        chunks.append(pending)
                        pending = []
                if pending and len("\n".join(pending + [line]).encode()) > limit:
                    chunks.append(pending)
                    pending = []
                pending.append(line)
        if pending:
            chunks.append(pending)
        source = "\n".join(lines)
        peers = []
        for candidate in files if collaborators else []:
            if candidate.path == file.path:
                continue
            stem = Path(candidate.path).stem
            if (
                "test" in candidate.path.lower()
                and Path(file.path).stem in candidate.path
            ) or re.search(
                r"(?:from|import|require).*\b" + re.escape(stem) + r"\b", source
            ):
                peers.append(candidate)
        for part, chunk in enumerate(chunks):
            content = "\n".join(chunk)
            paths = [file.path]
            oversized = len(content.encode()) > limit
            for peer in peers[:2]:
                # A bounded prefix made of intact numbered lines, clearly labelled.
                context = ["\nCollaborator " + peer.path + " (diff excerpt):"]
                for line in peer.lines:
                    value = f"old={line.old_line} new={line.new_line} {line.kind}: {line.text}"
                    if len("\n".join(context + [value]).encode()) > 1500:
                        break
                    context.append(value)
                extra = "\n".join(context)
                if len(context) > 1 and len((content + extra).encode()) <= limit:
                    content += extra
                    paths.append(peer.path)
            units.append(
                WorkUnit(
                    id=f"{file.path}:unit-{part}",
                    paths=paths,
                    content=content
                    if not oversized
                    else "A source line exceeds the unit budget; examination omitted.",
                    omitted=oversized,
                )
            )
    return units


def partition(bundle, kind="file_group", limit=6000):
    units = _partition(bundle, kind, limit)
    files = {f.path: f for f in bundle.code.files}
    counts = Counter(u.paths[0] for u in units)
    for unit in units:
        if kind == "whole_change":
            unit.representation = "outline"
        for path in unit.paths:
            file = files[path]
            # Primary and collaborator excerpts have separate numbered ranges.
            content = unit.content.split("\nCollaborator ", 1)[0]
            if path != unit.paths[0]:
                marker = "\nCollaborator " + path + " (diff excerpt):"
                content = (
                    unit.content.split(marker, 1)[1].split("\nCollaborator ", 1)[0]
                    if marker in unit.content
                    else ""
                )
            numbers = [
                int(n) for n in re.findall(r"^old=\S+ new=(\d+) ", content, re.M)
            ]
            ranges = []
            for n in numbers:
                if ranges and n == ranges[-1][1] + 1:
                    ranges[-1][1] = n
                else:
                    ranges.append([n, n])
            unit.file_context[path] = {
                "total_lines": file.total_lines,
                "supplied_new_ranges": ranges,
                "complete_file": bool(
                    file.total_lines is not None
                    and ranges == [[1, file.total_lines]]
                    and not unit.omitted
                ),
                "has_more_before": not numbers or numbers[0] > 1,
                "has_more_after": (
                    None
                    if file.total_lines is None
                    else not numbers or numbers[-1] < file.total_lines
                ),
                "chunk_index": int(unit.id.rsplit("-", 1)[1]),
                "chunk_count": counts[unit.paths[0]],
            }
    return units
