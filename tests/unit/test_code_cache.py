import asyncio
from types import SimpleNamespace

import pytest

from reviewer.context.code_cache import ScannedCodeCache
from reviewer.context.redaction import Redactor


async def test_concurrent_reads_scan_once_and_reapply_new_redactions():
    reads, scans = [], []

    async def read_file(wt, path):
        reads.append(path)
        await asyncio.sleep(0)
        return "known-secret later-secret\n" + "x" * 6000 + "tail-secret"

    async def scan(files):
        scans.append(files)
        return [{"Secret": "known-secret", "RuleID": "test"}]

    redactor = Redactor()
    cache = ScannedCodeCache(
        SimpleNamespace(read_file=read_file),
        object(),
        SimpleNamespace(scan=scan),
        redactor,
    )
    results = await asyncio.gather(*(cache.read("a.py") for _ in range(5)))
    assert reads == ["a.py"] and len(scans) == 1
    assert "tail-secret" in scans[0][0].lines[-1].text
    assert all("known-secret" not in value for value in results)
    redactor.secrets["later-secret"] = "later"
    assert "later-secret" not in await cache.read("a.py")
    # A new pinned worktree/review must scan again.
    fresh = ScannedCodeCache(
        SimpleNamespace(read_file=read_file),
        object(),
        SimpleNamespace(scan=scan),
        redactor,
    )
    await fresh.read("a.py")
    assert len(scans) == 2


async def test_failed_scan_is_not_cached_or_exposed():
    scans = 0

    async def read_file(*args):
        return "private"

    async def scan(files):
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("unavailable")
        return [{"Secret": "private", "RuleID": "test"}]

    cache = ScannedCodeCache(
        SimpleNamespace(read_file=read_file),
        object(),
        SimpleNamespace(scan=scan),
        Redactor(),
    )
    with pytest.raises(RuntimeError):
        await cache.read("a.py")
    assert await cache.read("a.py") == "[REDACTED:test]"
    assert scans == 2
