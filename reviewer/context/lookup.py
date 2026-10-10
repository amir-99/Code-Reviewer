"""Answer a reviewer's context requests from the whole pinned checkout.

A diff-only reviewer misjudges most often where it cannot see a callee's or a
caller's contract, and those usually live in files the change did not touch.
Lookups search the reviewed SHA with `git grep` (a fixed whole word, never a
pattern or an option) and return numbered excerpts read through the scanned
code cache, so every line a model sees has passed secret scanning first.
"""

import re
from pathlib import PurePosixPath

from reviewer.context.excerpts import excerpt
from reviewer.services.symbols.index import SUFFIXES, definitions

IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,127}")
MAX_FILES = 3
HITS_PER_FILE = 2
SCOPE_LINES = 60
WINDOW = 6
QUALIFIERS = (
    r"(?:(?:export|default|public|private|protected|static|async|readonly"
    r"|abstract|override|declare)\s+)*"
)


def is_test(path):
    lower = path.lower()
    name = PurePosixPath(lower).name
    return (
        any(part in {"test", "tests", "__tests__", "spec"} for part in lower.split("/"))
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
        or re.search(r"\.(test|spec)\.[jt]sx?$", name) is not None
    )


def defines(text, name):
    """Whether a source line plausibly defines `name` (confirmed by parsing)."""
    word = re.escape(name)
    return bool(
        re.search(
            rf"\b(?:def|class|func|function|type|interface|enum|const|let|var)\s+"
            rf"(?:\([^)]*\)\s*)?{word}\b",
            text,
        )
        or re.match(rf"\s*{QUALIFIERS}{word}\s*[(<]", text)
    )


CODE = SUFFIXES + (".js", ".jsx", ".mjs", ".cjs")


def ranked(paths):
    """Distinct paths: source, then tests, then anything else; first-seen order."""
    unique = list(dict.fromkeys(paths))
    return sorted(unique, key=lambda p: (not p.endswith(CODE), is_test(p)))


class CodeLookup:
    def __init__(self, git, wt, code_cache, symbols):
        self.git, self.wt = git, wt
        self.code_cache, self.symbols = code_cache, symbols

    async def file(self, path, start=None, end=None):
        source = await self.code_cache.read(path)
        return {path: excerpt(source.splitlines(), start or 1, end)}

    async def scoped(self, path, line):
        """The enclosing definition of a line when small, otherwise a window."""
        source = await self.code_cache.read(path)
        lines = source.splitlines()
        scope = next(
            (
                s
                for s in sorted(
                    definitions(path, source), key=lambda s: s["end"] - s["start"]
                )
                if s["start"] <= line <= s["end"]
                and s["end"] - s["start"] < SCOPE_LINES
            ),
            None,
        )
        start, end = (
            (scope["start"], scope["end"])
            if scope
            else (max(1, line - WINDOW), min(len(lines), line + WINDOW))
        )
        return excerpt(lines, start, end, limit=4000)

    async def symbol(self, name):
        output = {}
        # Changed files are indexed already; search the rest only when needed.
        paths = [
            path
            for path, found in self.symbols.symbols.items()
            if any(s["name"] == name for s in found)
        ]
        if not paths and IDENTIFIER.fullmatch(name):
            hits = await self.git.grep(self.wt, name)
            paths = [
                path
                for path, _, text in hits
                if path.endswith(SUFFIXES) and defines(text, name)
            ]
        for path in ranked(paths)[:MAX_FILES]:
            source = await self.code_cache.read(path)
            match = next(
                (s for s in definitions(path, source) if s["name"] == name), None
            )
            if match is None:
                continue
            output[path] = excerpt(
                source.splitlines(), match["start"], match["end"], limit=12000
            )
        return output

    async def callers(self, name):
        if not IDENTIFIER.fullmatch(name):
            return {}
        hits = [
            (path, line)
            for path, line, text in await self.git.grep(self.wt, name)
            if not defines(text, name)
        ]
        return await self.windows(hits)

    async def tests_for(self, target):
        if IDENTIFIER.fullmatch(target):
            hits = [
                (path, line)
                for path, line, _ in await self.git.grep(self.wt, target)
                if is_test(path)
            ]
            return await self.windows(hits)
        stem = PurePosixPath(target).stem.lower()
        if not stem:
            return {}
        tests = [
            path
            for path in await self.git.ls_files(self.wt)
            if is_test(path) and stem in PurePosixPath(path).name.lower()
        ]
        output = {}
        for path in tests[:2]:
            source = await self.code_cache.read(path)
            output[path] = excerpt(source.splitlines(), 1, None, limit=8000)
        return output

    async def windows(self, hits):
        by_file = {}
        for path, line in hits:
            by_file.setdefault(path, []).append(line)
        output = {}
        for path in ranked(by_file)[:MAX_FILES]:
            covered = []
            for line in by_file[path]:
                if len(covered) >= HITS_PER_FILE:
                    break
                if any(start <= line <= end for start, end in covered):
                    continue
                value = await self.scoped(path, line)
                covered.append((value["line_start"], value["line_end"]))
                output[f"{path}:{line}"] = value | {"file": path}
        return output
