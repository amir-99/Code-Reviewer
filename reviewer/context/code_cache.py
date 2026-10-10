"""A review-local cache of scanned code from one SHA-pinned worktree."""

import asyncio

from reviewer.context.models import ChangedFile, DiffLine
from reviewer.context.redaction import Redactor, redact_source


def scannable(path, text):
    """A whole file as the scanner's input: every line as unchanged context."""
    return ChangedFile(
        path=path,
        change_type="modified",
        lines=[
            DiffLine(text=line, new_line=n, old_line=n, kind="context")
            for n, line in enumerate(text.splitlines(), 1)
        ],
    )


class ScannedCodeCache:
    def __init__(self, git, worktree, scanner, redactor):
        self.git, self.worktree = git, worktree
        self.scanner, self.redactor = scanner, redactor
        self.locks = {}
        self.content = {}

    async def read_many(self, paths):
        """Read several files with one scan; unreadable files are left out.

        Used before any concurrent reader exists, so per-path locks are not
        needed; an entry another reader cached first is kept.
        """
        texts = {}
        for path in dict.fromkeys(paths):
            if path in self.content:
                continue
            try:
                texts[path] = await self.git.read_file(self.worktree, path)
            except Exception:
                continue
        if texts:
            matches = await self.scanner.scan(
                [scannable(path, text) for path, text in texts.items()]
            )
            self.redactor.secrets.update(Redactor(matches).secrets)
            for path, text in texts.items():
                self.content.setdefault(path, redact_source(text, self.redactor))
        return {
            path: redact_source(self.content[path], self.redactor)
            for path in paths
            if path in self.content
        }

    async def read(self, path):
        # A per-path lock coalesces concurrent requests without detached tasks.
        # Errors and cancellations never populate the cache.
        async with self.locks.setdefault(path, asyncio.Lock()):
            if path not in self.content:
                text = await self.git.read_file(self.worktree, path)
                matches = await self.scanner.scan([scannable(path, text)])
                self.redactor.secrets.update(Redactor(matches).secrets)
                self.content[path] = redact_source(text, self.redactor)
            # Later scans can discover secrets in already-cached files.
            return redact_source(self.content[path], self.redactor)
