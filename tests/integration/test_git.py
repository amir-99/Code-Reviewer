import subprocess

import pytest

from reviewer.services.git.service import GitError, GitService


def git(path, *args):
    return subprocess.check_output(["git", *args], cwd=path).decode().strip()


@pytest.fixture
def history(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    git(p, "init", "-b", "main")
    git(p, "config", "user.name", "Fixture")
    git(p, "config", "user.email", "fixture@example.invalid")
    (p / "a.py").write_text("x = 1\ny = 2\n")
    (p / "gone.txt").write_text("gone\n")
    git(p, "add", ".")
    git(p, "commit", "-m", "base")
    base = git(p, "rev-parse", "HEAD")
    git(p, "checkout", "-b", "PAY-12/change")
    for n in range(3):
        (p / f"new{n}.py").write_text(f"x = {n}\n")
        git(p, "add", ".")
        git(p, "commit", "-m", f"add {n}")
    git(p, "mv", "a.py", "renamed.py")
    git(p, "commit", "-m", "rename")
    git(p, "rm", "gone.txt")
    git(p, "commit", "-m", "delete")
    return p, base, git(p, "rev-parse", "HEAD")


async def test_five_commits_rename_deletion_and_cleanup(history, tmp_path):
    repo, base, head = history
    service = GitService(tmp_path / "cache", allow_local=True)
    async with service.workspace(1, str(repo), head) as wt:
        assert await service.merge_base(wt, "main", head) == base
        changes = await service.diff(wt, base, head)
        assert {x.path for x in changes} == set(
            git(repo, "diff", "--name-only", base, head).splitlines()
        )
        assert (
            next(x for x in changes if x.path == "renamed.py").change_type == "renamed"
        )
        deleted = next(x for x in changes if x.path == "gone.txt")
        assert deleted.lines[0].old_line == 1 and deleted.lines[0].new_line is None
        for change in changes:
            patch = git(
                repo,
                "diff",
                "--no-color",
                "-M",
                base,
                head,
                "--",
                change.old_path,
                change.path,
            )
            assert [h.header for h in change.hunks] == [
                s for s in patch.splitlines() if s.startswith("@@")
            ]
        with pytest.raises(GitError):
            await service.read_file(wt, "../outside")
        path = wt.path
    assert not path.exists()
