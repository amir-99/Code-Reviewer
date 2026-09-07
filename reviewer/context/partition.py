import re
from pathlib import Path

from pydantic import BaseModel


class WorkUnit(BaseModel):
    id: str
    paths: list[str]
    content: str


def partition(bundle, kind="file_group", limit=6000):
    # UTF-8 bytes form a conservative token ceiling.
    limit = max(256, limit)
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
