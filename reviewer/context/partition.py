import re
from pathlib import Path

from pydantic import BaseModel


class WorkUnit(BaseModel):
    id: str
    paths: list[str]
    content: str
    omitted: bool = False


def partition(bundle, kind="file_group", limit=6000):
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
    result = []
    for file in files:
        paths = [file.path]
        content = "\n".join(
            f"old={line.old_line} new={line.new_line} {line.kind}: {line.text}"
            for line in file.lines
        )
        if kind == "file_group":
            collaborators = []
            for candidate in files:
                if candidate.path == file.path:
                    continue
                stem = Path(candidate.path).stem
                is_test = (
                    "test" in candidate.path.lower()
                    and Path(file.path).stem in candidate.path
                )
                imported = bool(
                    re.search(
                        r"(?:from|import|require).*\b" + re.escape(stem) + r"\b",
                        content,
                    )
                )
                if is_test or imported:
                    collaborators.append(candidate)
            for peer in collaborators[:3]:
                paths.append(peer.path)
                content += (
                    "\nCollaborator "
                    + peer.path
                    + "\n"
                    + "\n".join(f"{line.new_line}: {line.text}" for line in peer.lines)[
                        :1500
                    ]
                )
        if not content:
            content = f"{file.path}: binary or rename-only change; no textual hunks"
        encoded = content.encode()
        offset = 0
        part = 0
        while offset < len(encoded):
            # Ignore incomplete UTF-8 tails to keep strict byte-budget guarantees.
            chunk = encoded[offset : offset + limit].decode(errors="ignore")
            result.append(
                WorkUnit(id=f"{file.path}:unit-{part}", paths=paths, content=chunk)
            )
            offset += limit
            part += 1
    return result


def coherent_units(bundle, limit):
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
            for line in hunk:
                if pending and len("\n".join(pending + [line]).encode()) > limit:
                    chunks.append(pending)
                    pending = []
                pending.append(line)
        if pending:
            chunks.append(pending)
        source = "\n".join(lines)
        peers = []
        for candidate in files:
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
