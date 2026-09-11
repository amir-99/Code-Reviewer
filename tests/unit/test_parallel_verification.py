import asyncio
from types import SimpleNamespace

import pytest
from test_findings import finding

from reviewer.findings.models import VerificationResult
from reviewer.orchestrator.budget import BudgetExhausted
from reviewer.orchestrator.verification import verify_findings


@pytest.mark.parametrize("concurrency", [1, 2, 4])
async def test_verifiers_overlap_with_bounded_workers_and_preserve_order(
    monkeypatch, concurrency
):
    findings = [finding(n) for n in range(1, 6)]
    identities = [f.id for f in findings]
    scanned = []
    active = 0
    peak = 0
    full = asyncio.Event()
    release = asyncio.Event()

    async def scan(requests):
        scanned.append(requests)

    async def verify(f, *args):
        nonlocal active, peak
        assert len(scanned) == len(findings)
        active += 1
        peak = max(peak, active)
        if active == concurrency:
            full.set()
        try:
            await release.wait()
            f.verification = VerificationResult(
                verdict="confirmed", counterargument="none", reasoning="code"
            )
        finally:
            active -= 1

    monkeypatch.setattr("reviewer.orchestrator.verification.verify", verify)
    task = asyncio.create_task(
        verify_findings(
            findings, SimpleNamespace(degradations=[]), None, None, scan, concurrency
        )
    )
    try:
        await asyncio.wait_for(full.wait(), 1)
        assert peak == concurrency
        release.set()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [f.id for f in findings] == identities
    assert all(f.verification.verdict == "confirmed" for f in findings)
    assert active == 0


async def test_individual_scan_and_verifier_failures_remain_uncertain(monkeypatch):
    findings = [finding(n) for n in range(1, 5)]
    scans = 0

    async def scan(requests):
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("private failure")

    async def verify(f, *args):
        if f.anchor.line_start in {2, 3}:
            raise BudgetExhausted("exhausted")
        f.verification = VerificationResult(
            verdict="confirmed", counterargument="none", reasoning="code"
        )

    monkeypatch.setattr("reviewer.orchestrator.verification.verify", verify)
    bundle = SimpleNamespace(degradations=[])
    await verify_findings(findings, bundle, None, None, scan, 2)
    assert [f.verification.verdict for f in findings] == [
        "uncertain",
        "uncertain",
        "uncertain",
        "confirmed",
    ]
    assert bundle.degradations == ["budget_exhausted"]
    assert "private failure" not in str(findings)


async def test_cancellation_drains_verifiers_before_return(monkeypatch):
    active = 0
    entered = asyncio.Event()
    calls = []

    async def scan(requests):
        pass

    async def verify(f, *args):
        nonlocal active
        calls.append(f.id)
        active += 1
        if active == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr("reviewer.orchestrator.verification.verify", verify)
    task = asyncio.create_task(
        verify_findings(
            [finding(n) for n in range(4)],
            SimpleNamespace(degradations=[]),
            None,
            None,
            scan,
            2,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0 and len(calls) == 2


async def test_all_evidence_is_scanned_before_any_verifier_sees_it(monkeypatch):
    from reviewer.findings.models import Evidence

    f = finding()
    f.evidence = [
        Evidence(file=f"source{n}.py", line_start=1, line_end=1, note="code")
        for n in range(8)
    ]
    scanned = set()

    async def scan(requests):
        # Mirror the provider's real request cap.
        scanned.update(r.target for r in requests[:5])

    async def verify(f, *args):
        assert scanned == {f.anchor.file, *(e.file for e in f.evidence)}

    monkeypatch.setattr("reviewer.orchestrator.verification.verify", verify)
    await verify_findings([f], SimpleNamespace(degradations=[]), None, None, scan, 2)
    assert len(scanned) == 9
    assert f.verification is None  # No assertion was swallowed as an unavailable judge.
