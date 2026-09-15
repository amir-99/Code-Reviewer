"""Forge/local inventory comparisons tolerate different rename detection."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from reviewer.config.schema import ProjectConfig
from reviewer.context.builder import build
from reviewer.services.forge.gitlab import GitLab, MergeRequestContext, StaleReview
from reviewer.services.git.service import GitService
from reviewer.services.secrets.scanner import FakeSecretScanner
from tests.integration.test_git import git


@pytest.mark.parametrize("local_rename", [False, True])
@pytest.mark.parametrize("mismatch", [None, "missing_old", "missing_new", "extra"])
async def test_context_inventory_ignores_rename_pairing(
    tmp_path, local_rename, mismatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "old.py").write_text("old_value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "change")
    (repo / "old.py").rename(repo / "new.py")
    if not local_rename:
        (repo / "new.py").write_text("completely_different = 42\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "move")
    head = git(repo, "rev-parse", "HEAD")

    # Deliberately use the opposite representation from local Git.
    entries = (
        [
            {"old_path": "old.py", "new_path": "old.py"},
            {"old_path": "new.py", "new_path": "new.py"},
        ]
        if local_rename
        else [{"old_path": "old.py", "new_path": "new.py"}]
    )
    if mismatch == "missing_old":
        entries = [{"old_path": "new.py", "new_path": "new.py"}]
    elif mismatch == "missing_new":
        entries = [{"old_path": "old.py", "new_path": "old.py"}]
    elif mismatch == "extra":
        entries.append({"old_path": "extra.py", "new_path": "extra.py"})

    def handler(request):
        assert request.url.path == "/api/v4/projects/1/merge_requests/1/diffs"
        return httpx.Response(200, json=entries)

    forge = GitLab("https://git.internal", "fake", httpx.MockTransport(handler))
    service = GitService(tmp_path / "cache", allow_local=True)
    mr = MergeRequestContext(project_id=1, iid=1, head_sha=head, target_branch="main")
    review = SimpleNamespace(id=uuid4(), head_sha=head, started_at=datetime.now(UTC))
    try:
        async with service.workspace(1, str(repo), head) as wt:

            async def collect():
                return await build(
                    review,
                    mr,
                    wt,
                    service,
                    forge,
                    None,
                    None,
                    FakeSecretScanner(),
                    ProjectConfig(),
                )

            if mismatch:
                with pytest.raises(StaleReview, match="inventories differ"):
                    await collect()
            else:
                bundle, *_ = await collect()
                changes = bundle.code.files
                if local_rename:
                    assert [(f.path, f.change_type) for f in changes] == [
                        ("new.py", "renamed")
                    ]
                else:
                    assert {(f.path, f.change_type) for f in changes} == {
                        ("old.py", "deleted"),
                        ("new.py", "added"),
                    }
    finally:
        await forge.close()


async def test_inventory_paginates_by_diff_entries_not_expanded_paths():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        entries = (
            [{"old_path": f"old/{i}", "new_path": f"new/{i}"} for i in range(100)]
            if page == 1
            else [{"old_path": "tail.py", "new_path": "tail.py"}]
        )
        return httpx.Response(200, json=entries)

    forge = GitLab("https://git.internal", "fake", httpx.MockTransport(handler))
    try:
        paths = await forge.get_changed_paths(1, 1)
        assert set(paths) == (
            {f"old/{i}" for i in range(100)}
            | {f"new/{i}" for i in range(100)}
            | {"tail.py"}
        )
        assert pages == [1, 2]
    finally:
        await forge.close()
