"""SHA-pinned worktrees. Repository programs, hooks and filters never run here."""

import asyncio
import os
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import structlog

from reviewer.context.models import ChangedFile, DiffLine, Hunk
from reviewer.telemetry.activity import activity

logger = structlog.get_logger()


class GitError(RuntimeError):
    pass


@dataclass
class WorktreeHandle:
    path: Path
    mirror: Path
    head_sha: str


@dataclass
class BlameLine:
    sha: str
    line: int


class GitService:
    def __init__(
        self,
        cache: Path,
        token="",
        quota_bytes=10_000_000_000,
        timeout=60,
        allow_local=False,
    ):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.token, self.quota, self.timeout = token, quota_bytes, timeout
        self.allow_local = allow_local
        self.locks = {}
        self.active = set()

    @activity("tool", "Git read operation")
    async def command(self, *args, cwd=None, limit=20_000_000):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_TERMINAL_PROMPT="0",
            GIT_LFS_SKIP_SMUDGE="1",
            LC_ALL="C.UTF-8",
            REVIEW_GIT_TOKEN=self.token,
        )
        helper = '!f() { test "$1" = get && printf "username=oauth2\\npassword=%s\\n" "$REVIEW_GIT_TOKEN"; }; f'
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            f"protocol.file.allow={'always' if self.allow_local else 'never'}",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={helper}",
            *map(str, args),
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(self.timeout):
                chunks = []
                size = 0
                while chunk := await proc.stdout.read(65536):
                    size += len(chunk)
                    if size > limit:
                        raise GitError("Git output exceeds budget")
                    chunks.append(chunk)
                await proc.wait()
            if proc.returncode:
                raise GitError("Git operation failed")
            return b"".join(chunks)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def sync_mirror(self, project_id, url):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if not self.allow_local and (
            parsed.scheme != "https" or parsed.username or parsed.password
        ):
            raise GitError("Repository requires credential-free HTTPS URL")
        mirror = self.cache / f"{int(project_id)}.git"
        if not mirror.exists():
            temporary = self.cache / f".clone-{uuid4()}"
            try:
                await self.command(
                    "clone", "--bare", "--filter=blob:none", "--", url, temporary
                )
                temporary.rename(mirror)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        else:
            await self.command("remote", "set-url", "origin", url, cwd=mirror)
        await self.command(
            "fetch",
            "--prune",
            "origin",
            "+refs/heads/*:refs/heads/*",
            "+refs/merge-requests/*/head:refs/merge-requests/*/head",
            cwd=mirror,
        )
        os.utime(mirror, None)
        return mirror

    def evict(self):
        mirrors = sorted(self.cache.glob("*.git"), key=lambda p: p.stat().st_mtime)
        sizes = {
            p: sum(
                f.stat().st_size
                for f in p.rglob("*")
                if f.is_file() and not f.is_symlink()
            )
            for p in mirrors
        }
        total = sum(sizes.values())
        for path in mirrors:
            if total <= self.quota:
                break
            if path in self.active:
                continue
            shutil.rmtree(path)
            total -= sizes[path]
        if total > self.quota:
            raise GitError("Repository cache quota exhausted")

    @asynccontextmanager
    async def workspace(self, project_id, url, sha):
        if not re.fullmatch("[0-9a-f]{40,64}", sha):
            raise GitError("Invalid SHA")
        lock = self.locks.setdefault(project_id, asyncio.Lock())
        # Hold project lock through the lease: fetch/eviction cannot invalidate it.
        async with lock:
            mirror = await self.sync_mirror(project_id, url)
            self.active.add(mirror)
            path = self.cache / f"work-{uuid4()}"
            try:
                self.evict()
                logger.info("git_worktree_create", project_id=project_id, sha=sha)
                await self.command("worktree", "add", "--detach", path, sha, cwd=mirror)
                yield WorktreeHandle(path, mirror, sha)
            finally:
                logger.info("git_worktree_cleanup", project_id=project_id, sha=sha)
                try:
                    await self.command(
                        "worktree", "remove", "--force", path, cwd=mirror
                    )
                except GitError:
                    shutil.rmtree(path, ignore_errors=True)
                self.active.discard(mirror)

    async def merge_base(self, wt, target, head):
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", target) or target.startswith("-"):
            raise GitError("Invalid target branch")
        return (
            (
                await self.command(
                    "merge-base", f"refs/heads/{target}", head, cwd=wt.mirror
                )
            )
            .decode()
            .strip()
        )

    async def read_file(self, wt, path):
        root = wt.path.resolve()
        file = root / path
        if (
            file.is_symlink()
            or not file.resolve().is_relative_to(root)
            or not file.is_file()
        ):
            raise GitError("Unavailable file")
        if file.stat().st_size > 2_000_000:
            raise GitError("File exceeds budget")
        return file.read_text(errors="replace")

    async def diff(self, wt, base, head, context_lines=3):
        names = (
            (
                await self.command(
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-status",
                    "-z",
                    "-M",
                    base,
                    head,
                    "--",
                    cwd=wt.mirror,
                )
            )
            .decode()
            .split("\0")
        )
        result = []
        i = 0
        while i < len(names) - 1:
            status = names[i]
            old = names[i + 1]
            i += 2
            path = names[i] if status.startswith(("R", "C")) else old
            if status.startswith(("R", "C")):
                i += 1
            change = ChangedFile(
                path=path,
                old_path=old,
                change_type={"A": "added", "D": "deleted", "R": "renamed"}.get(
                    status[0], "modified"
                ),
                language=Path(path).suffix.lstrip(".") or None,
            )
            patch = (
                await self.command(
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    f"--unified={max(0, min(100, context_lines))}",
                    "-M",
                    base,
                    head,
                    "--",
                    old,
                    path,
                    cwd=wt.mirror,
                )
            ).decode(errors="replace")
            oldline = newline = None
            for line in patch.splitlines():
                match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
                if match:
                    a, b, c, d = match.groups()
                    oldline = int(a)
                    newline = int(c)
                    change.hunks.append(
                        Hunk(
                            old_start=int(a),
                            old_lines=int(b or 1),
                            new_start=int(c),
                            new_lines=int(d or 1),
                            header=line,
                        )
                    )
                elif oldline is not None and line.startswith(("+", "-", " ")):
                    kind = {"+": "added", "-": "removed", " ": "context"}[line[0]]
                    change.lines.append(
                        DiffLine(
                            text=line[1:],
                            old_line=oldline if kind != "added" else None,
                            new_line=newline if kind != "removed" else None,
                            kind=kind,
                        )
                    )
                    oldline += kind != "added"
                    newline += kind != "removed"
            file = wt.path / path
            change.size_bytes = (
                file.stat().st_size if file.exists() and not file.is_symlink() else 0
            )
            result.append(change)
        return result

    async def blame_lines(self, wt, path, start, end, sha=None):
        data = (
            await self.command(
                "blame",
                "--line-porcelain",
                f"-L{int(start)},{int(end)}",
                sha or wt.head_sha,
                "--",
                path,
                cwd=wt.path,
            )
        ).decode(errors="replace")
        return [
            BlameLine(m[1], int(m[2]))
            for m in re.finditer(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$", data, re.M)
        ]
