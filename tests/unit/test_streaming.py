import asyncio
from types import SimpleNamespace

from test_findings import finding

from reviewer.findings.models import ProposedFinding
from reviewer.orchestrator.streaming import FileStream


def proposal(file, claim="Fails"):
    f = finding()
    f.anchor.file, f.claim = file, claim
    return ProposedFinding(**{k: getattr(f, k) for k in ProposedFinding.model_fields})


def unit(*paths):
    return SimpleNamespace(paths=list(paths))


async def test_a_file_starts_when_its_last_unit_finishes_and_is_reused():
    started = []

    async def prepare(items):
        started.append([p.anchor.file for p in items])
        return [p.claim for p in items], []

    async with asyncio.TaskGroup() as group:
        stream = FileStream(group, prepare)
        a1, a2, b = unit("a.py"), unit("a.py"), unit("b.py")
        stream.planned([a1, a2, b])
        first, second = proposal("a.py", "one"), proposal("a.py", "two")
        stream.finished(a1, SimpleNamespace(findings=[first]))
        await asyncio.sleep(0)
        assert started == []  # a.py still has a unit pending
        stream.finished(a2, SimpleNamespace(findings=[second]))
        await asyncio.sleep(0)
        assert started == [["a.py", "a.py"]]
        stream.finished(b, SimpleNamespace(findings=[]))
        findings, _ = await stream.finish([first, second])
    assert findings == ["one", "two"] and len(started) == 1


async def test_late_or_changed_inputs_are_processed_from_the_stage_result():
    started = []

    async def prepare(items):
        started.append(sorted(p.claim for p in items))
        return [p.claim for p in items], []

    async with asyncio.TaskGroup() as group:
        stream = FileStream(group, prepare)
        a, b = unit("a.py"), unit("b.py")
        stream.planned([a, b])
        early = proposal("a.py", "early")
        stream.finished(a, SimpleNamespace(findings=[early]))
        # A finding for a.py arrives from another unit after a.py started, and
        # one anchored outside every unit waits for the end.
        late, outside = proposal("a.py", "late"), proposal("other.py", "outside")
        stream.finished(b, SimpleNamespace(findings=[late, outside]))
        findings, _ = await stream.finish([early, late, outside])
    assert findings == ["early", "late", "outside"]
    assert started == [["early"], ["early", "late"], ["outside"]]
