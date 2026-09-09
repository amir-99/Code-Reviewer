import json

import httpx
import pytest
from conftest import FakeQueue
from test_git import git
from test_git import history as history

from reviewer import worker
from reviewer.config.schema import DEFAULT_ROLE_MODELS, Settings
from reviewer.findings.models import (
    Anchor,
    Coverage,
    Evidence,
    ProposedFinding,
    RecheckResult,
    StageEnvelope,
    VerificationResult,
)
from reviewer.main import create_app
from reviewer.orchestrator.pipeline import Pipeline
from reviewer.services.docs.confluence import FakeDocumentService
from reviewer.services.forge.gitlab import FakeForge, MergeRequestContext
from reviewer.services.git.service import GitService
from reviewer.services.issues.jira import FakeIssueService
from reviewer.services.secrets.scanner import FakeSecretScanner
from reviewer.services.static.runner import StaticRunner


class Echo:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        import re
        from html import unescape

        self.calls.append(kwargs)
        ids = re.findall(r'"id": "([^"]+:unit-\d+)"', unescape(kwargs["user"]))
        return StageEnvelope(
            findings=[],
            coverage=Coverage(units_examined=ids, units_skipped=[], skip_reason=None),
        )


class Reviewing:
    """Reports one correctness finding on the original `new0.py`, and judges rechecks."""

    def __init__(self):
        self.recheck_calls = []

    async def complete(self, **kwargs):
        import re
        from html import unescape

        model = kwargs["response_model"]
        if model is VerificationResult:
            return VerificationResult(
                counterargument="The value could be checked elsewhere",
                reasoning="The cited line assigns without a check",
                verdict="confirmed",
            )
        if model is RecheckResult:
            self.recheck_calls.append(kwargs)
            return RecheckResult(
                change_summary="The assignment is now guarded by a range check.",
                reasoning="The input the claim described can no longer reach it.",
                verdict="fixed",
            )
        user = unescape(kwargs["user"])
        ids = re.findall(r'"id": "([^"]+:unit-\d+)"', user)
        findings = []
        if kwargs["stage"] == "correctness" and "added: x = 0" in user:
            findings = [
                ProposedFinding(
                    anchor=Anchor(file="new0.py", line_start=1, line_end=1),
                    category="correctness",
                    severity_proposed="REQUIRED",
                    claim="The assigned value is never checked",
                    reason="Nothing validates the assignment",
                    impact="A wrong value reaches the caller",
                    failure_scenario="The value is out of range",
                    evidence=[
                        Evidence(
                            file="new0.py",
                            line_start=1,
                            line_end=1,
                            note="assignment",
                        )
                    ],
                    suggested_direction="Validate the value",
                    confidence="high",
                )
            ]
        return StageEnvelope(
            findings=findings,
            coverage=Coverage(units_examined=ids, units_skipped=[], skip_reason=None),
        )


def pipeline(store, forge, tmp_path, scanner=None, llm=None):
    settings = Settings(milestone="M9", config_path=tmp_path / "config.json")
    return Pipeline(
        store,
        forge,
        settings,
        GitService(tmp_path / "cache", allow_local=True),
        FakeIssueService(),
        FakeDocumentService(),
        scanner or FakeSecretScanner(),
        StaticRunner(),
        lambda *args: llm or Echo(),
    )


@pytest.mark.parametrize(
    "project_ids,webhook_secrets",
    [([], {}), ([88], {}), ([], {88: "hook-secret"}), ([88, 88], {88: "hook-secret"})],
)
async def test_onboarding_manual_review_and_publication(
    store, history, tmp_path, monkeypatch, project_ids, webhook_secrets
):
    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=88,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.projects["group/proj"] = 88
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    settings = Settings(
        _env_file=None,
        project_ids=project_ids,
        webhook_secrets=webhook_secrets,
        admin_token="admin",
        milestone="M0",
        otlp_endpoint="",
    )
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "Store", lambda *_: store)
    monkeypatch.setattr(worker, "GitLab", lambda *_: forge)
    queue = FakeQueue()
    ctx = {"redis": queue}
    assert not await store.is_configured(88)
    await worker.startup(ctx)
    assert await store.is_configured(88) == bool(project_ids or webhook_secrets)
    app = create_app(settings, store, queue, forge)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        if not webhook_secrets:
            # Onboarding alone must never authorize incoming webhook requests.
            for headers in ({}, {"X-Gitlab-Token": "anything"}):
                response = await client.post(
                    "/webhooks/gitlab", json={"project": {"id": 88}}, headers=headers
                )
                assert response.status_code == 401
            assert queue.jobs == []
        response = await client.post(
            "/admin/reviews",
            json={
                "merge_request_url": f"{settings.gitlab_base_url}/group/proj/-/merge_requests/2"
            },
            headers={"Authorization": "Bearer admin"},
        )
    assert response.status_code == 202
    assert response.json()["report_mode"] == "applied"
    # The API only validates and enqueues; provisioning belongs to the worker.
    assert await store.is_configured(88) == bool(project_ids or webhook_secrets)
    ctx["machine"] = pipeline(store, forge, tmp_path, llm=Reviewing())
    await worker.receive_event(ctx, queue.jobs[0][1][0])
    assert await store.is_configured(88)
    await worker.receive_event(ctx, queue.jobs[0][1][0])
    assert len(queue.jobs) == 2
    review_id = queue.jobs[1][1][0]
    assert await ctx["machine"].run(review_id) == "PUBLISHED"
    assert len(forge.comments) >= 2  # Inline finding and summary.
    assert forge.statuses[-1]["state"] == "success"


async def test_complete_pipeline_with_real_git_and_fake_external_services(
    store, history, tmp_path
):
    repo, base, head = history
    mr = MergeRequestContext(
        project_id=7,
        iid=2,
        head_sha=head,
        target_branch="main",
        repository_url=str(repo),
    )
    forge = FakeForge(mr)
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    review = await store.accept(7, 2, head, "pipeline")
    state = await pipeline(store, forge, tmp_path).run(review.id)
    assert state == "PUBLISHED"
    assert forge.statuses[-1]["state"] == "success"
    assert len(forge.comments) == 1
    saved = await store.snapshot(review.id)
    assert not saved["partial"]
    assert len(await store.stages(review.id)) == 7
    events = await store.events(review.id, limit=2000)
    agents = [e["data"] for e in events if e["kind"] == "agent"]
    assert {e["name"] for e in agents if e["status"] == "completed"} == {
        "purpose",
        "design",
        "correctness",
        "complexity",
        "tests_",
        "line_review",
        "system_context",
    }
    assert any(e["kind"] == "unit" and e["data"]["parent_id"] for e in events)
    assert any(
        e["kind"] == "tool" and e["data"]["name"] == "Secret scanner" for e in events
    )
    assert any(
        e["kind"] == "state" and e["data"]["state"] == "PUBLISHED" for e in events
    )


async def test_secret_short_circuit_no_model_call(store, history, tmp_path):
    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    scanner = FakeSecretScanner(
        [
            {
                "File": "new0.py",
                "StartLine": 1,
                "EndLine": 1,
                "Secret": "x = 0",
                "RuleID": "fixture",
            }
        ]
    )
    llm = Echo()
    review = await store.accept(7, 2, head, "secret")
    state = await pipeline(store, forge, tmp_path, scanner, llm).run(review.id)
    assert state == "TERMINATED_EARLY"
    assert not llm.calls
    assert "x = 0" not in json.dumps(await store.snapshot(review.id))
    assert len(forge.comments) == 2


async def test_failed_stage_never_blocks_gating_review(store, history, tmp_path):
    repo, base, head = history
    (tmp_path / "config.json").write_text('{"defaults":{"enforcement":"gating"}}')
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()

    class Broken:
        async def complete(self, **kwargs):
            raise TimeoutError()

    review = await store.accept(7, 2, head, "failed-stage")
    assert (
        await pipeline(store, forge, tmp_path, llm=Broken()).run(review.id)
        == "PUBLISHED"
    )
    assert forge.statuses[-1]["state"] == "success"
    assert (await store.get(review.id)).partial


async def test_git_failure_is_context_failure_and_passes(store, tmp_path):
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha="a" * 40,
            target_branch="main",
            repository_url=str(tmp_path / "missing"),
        )
    )
    review = await store.accept(7, 2, "a" * 40, "clone-failed")
    assert await pipeline(store, forge, tmp_path).run(review.id) == "FAILED_CONTEXT"
    assert forge.statuses[-1]["state"] == "success"
    assert len(forge.comments) == 1 and "could not collect" in forge.comments[0].body


async def test_incremental_push_only_dispatches_affected_files(
    store, history, tmp_path
):
    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    machine = pipeline(store, forge, tmp_path)
    first = await store.accept(7, 2, head, "first-push")
    assert await machine.run(first.id) == "PUBLISHED"
    (repo / "new0.py").write_text("x = 4\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "ordinary push")
    newhead = git(repo, "rev-parse", "HEAD")
    forge.mr.head_sha = newhead
    second = await store.accept(7, 2, newhead, "next-push")
    assert await machine.run(second.id) == "PUBLISHED"
    stages = await store.stages(second.id)
    assert stages["correctness"].examined == ["new0.py:unit-0"]
    assert not stages["system_context"].examined


async def test_verified_purpose_blocker_stops_before_design(store, history, tmp_path):
    from reviewer.findings.models import ProposedFinding, VerificationResult

    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()

    class Blocker(Echo):
        async def complete(self, **kwargs):
            if kwargs["stage"] == "verification":
                self.calls.append(kwargs)
                return VerificationResult(
                    verdict="confirmed",
                    counterargument="Fixture",
                    reasoning="Fixture evidence",
                )
            result = await super().complete(**kwargs)
            if kwargs["stage"] == "purpose":
                result.findings = [
                    ProposedFinding.model_validate(
                        {
                            "anchor": {
                                "file": "new0.py",
                                "line_start": 1,
                                "line_end": 1,
                            },
                            "category": "security",
                            "severity_proposed": "BLOCKER",
                            "claim": "Fixture security blocker",
                            "reason": "Fixture",
                            "impact": "Fixture",
                            "evidence": [
                                {
                                    "file": "new0.py",
                                    "line_start": 1,
                                    "line_end": 1,
                                    "note": "Fixture",
                                }
                            ],
                            "suggested_direction": "Fixture",
                            "confidence": "high",
                        }
                    )
                ]
            return result

    llm = Blocker()
    review = await store.accept(7, 2, head, "purpose-blocker")
    assert (
        await pipeline(store, forge, tmp_path, llm=llm).run(review.id)
        == "TERMINATED_EARLY"
    )
    assert [c["stage"] for c in llm.calls] == ["purpose", "verification"]
    assert "DESIGN_REVIEW" not in (await store.get(review.id)).history


async def test_recheck_answers_open_comments_after_a_push(store, history, tmp_path):
    """A second push replies in the thread the first review opened."""

    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    llm = Reviewing()
    machine = pipeline(store, forge, tmp_path, llm=llm)
    first = await store.accept(7, 2, head, "first-push")
    assert await machine.run(first.id) == "PUBLISHED"
    inline = next(d for d in forge.discussions if d.file == "new0.py")

    (repo / "new0.py").write_text("x = 40 if y else 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "guard the value")
    newhead = git(repo, "rev-parse", "HEAD")
    forge.mr.head_sha = newhead
    forge.paths = git(repo, "diff", "--name-only", base, newhead).splitlines()
    second = await store.accept(7, 2, newhead, "second-push")
    assert await machine.run(second.id) == "PUBLISHED"

    # The re-review no longer reports the claim, so the thread is answered as
    # fixed; the judge supplies the part the diff cannot — how it was fixed.
    assert len(llm.recheck_calls) == 1
    replies = [body for discussion, body in forge.replies if discussion == inline.id]
    assert len(replies) == 1 and "Fixed" in replies[0]
    assert "now guarded by a range check" in replies[0]
    assert inline.resolved
    recheck = (await store.snapshot(second.id))["recheck"]
    assert [v["verdict"] for v in recheck["verdicts"]] == ["fixed"]


async def test_recheck_command_answers_threads_without_running_a_review(
    store, history, tmp_path
):
    """`/ai recheck` judges the open threads at the current head, admitting nothing."""
    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    llm = Reviewing()
    machine = pipeline(store, forge, tmp_path, llm=llm)
    review = await store.accept(7, 2, head, "first-push")
    assert await machine.run(review.id) == "PUBLISHED"
    inline = next(d for d in forge.discussions if d.file == "new0.py")
    notes = len(forge.comments)

    (repo / "new0.py").write_text("x = 40 if y else 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "guard the value")
    forge.mr.head_sha = git(repo, "rev-parse", "HEAD")
    result = await machine.recheck_now(7, 2)

    assert [p["verdict"] for p in result["posted"]] == ["fixed"]
    assert len(llm.recheck_calls) == 1 and inline.resolved
    assert "now guarded by a range check" in forge.replies[0][1]
    # One reply, no summary note, no second review.
    assert len(forge.comments) == notes + 1
    assert len(await store.recent()) == 1
    # The dashboard reads a manual recheck back from the review it answered for.
    snapshot = await store.snapshot(review.id)
    assert [v["verdict"] for v in snapshot["recheck"]["verdicts"]] == ["fixed"]
    assert snapshot["recheck"]["at"] and snapshot["findings"]
    # Nothing is left to answer at this head.
    assert (await machine.recheck_now(7, 2)) is None


async def test_a_run_resolves_its_models_once_and_records_what_it_used(
    store, history, tmp_path
):
    """Every stage's role is resolved before the first one runs, and reported."""
    repo, base, head = history
    forge = FakeForge(
        MergeRequestContext(
            project_id=7,
            iid=2,
            head_sha=head,
            target_branch="main",
            repository_url=str(repo),
        )
    )
    forge.paths = git(repo, "diff", "--name-only", base, head).splitlines()
    # The operator moved one role for this run only; the rest keep the defaults.
    review = await store.accept(
        7,
        2,
        head,
        "models",
        overrides={"requested_by": "admin", "models": {"correctness": "vendor/strong"}},
    )
    tiers = []

    class Recording(Echo):
        async def complete(self, **kwargs):
            tiers.append(kwargs["tier"])
            return await super().complete(**kwargs)

    assert await pipeline(store, forge, tmp_path, llm=Recording()).run(review.id) == (
        "PUBLISHED"
    )
    events = await store.events(review.id, limit=2000)
    announced = [e for e in events if e["kind"] == "models"]
    assert len(announced) == 1, "one selection per run, before any stage starts"
    selection = announced[0]["data"]
    assert selection["correctness"] == "vendor/strong"
    assert selection["line_review"] == DEFAULT_ROLE_MODELS["line_review"]
    assert selection["verification"] == DEFAULT_ROLE_MODELS["verification"]
    first_stage = next(i for i, e in enumerate(events) if e["kind"] == "agent")
    assert events.index(announced[0]) < first_stage
    # Stages ask for their own role rather than a shared tier, so an operator can
    # price them separately.
    assert {"purpose", "design", "correctness", "line_review"} <= set(tiers)
    assert "strong" not in tiers and "fast" not in tiers
    # The run's own record survives its events: the snapshot carries it too.
    saved = await store.snapshot(review.id)
    assert saved["bundle"]["budget"]["model_tier"] == selection
