import json

from test_git import git
from test_git import history as history

from reviewer.config.schema import Settings
from reviewer.findings.models import Coverage, StageEnvelope
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
