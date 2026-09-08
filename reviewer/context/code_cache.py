"""A review-local cache of scanned code from one SHA-pinned worktree."""

import asyncio

from reviewer.context.models import ChangedFile, DiffLine
from reviewer.context.redaction import Redactor


class ScannedCodeCache:
    def __init__(self, git, worktree, scanner, redactor):
        self.git, self.worktree = git, worktree
        self.scanner, self.redactor = scanner, redactor
        self.locks = {}
        self.content = {}

    async def read(self, path):
        # A per-path lock coalesces concurrent requests without detached tasks.
        # Errors and cancellations never populate the cache.
        async with self.locks.setdefault(path, asyncio.Lock()):
            if path not in self.content:
                text = await self.git.read_file(self.worktree, path)
                matches = await self.scanner.scan(
                    [
                        ChangedFile(
                            path=path,
                            change_type="modified",
                            lines=[
                                DiffLine(
                                    text=line, new_line=n, old_line=n, kind="context"
                                )
                                for n, line in enumerate(text.splitlines(), 1)
                            ],
                        )
                    ]
                )
                self.redactor.secrets.update(Redactor(matches).secrets)
                self.content[path] = self.redactor.text(text[:6000])
            # Later scans can discover secrets in already-cached files.
            return self.redactor.text(self.content[path])
