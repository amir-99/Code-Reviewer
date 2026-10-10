import subprocess
from types import SimpleNamespace

import pytest

from reviewer.context.lookup import CodeLookup, defines, is_test
from reviewer.services.git.service import GitService
from reviewer.services.symbols.index import SymbolIndex


class PlainCache:
    """Stands in for the scanned code cache: reads, and records each read."""

    def __init__(self, root):
        self.root, self.reads = root, []

    async def read(self, path):
        self.reads.append(path)
        return (self.root / path).read_text()


@pytest.fixture
def repo(tmp_path):
    files = {
        "store/accounts.py": "def balance(account):\n"
        "    if account is None:\n"
        "        raise ValueError('missing')\n"
        "    return account.total\n",
        "api/views.py": "from store.accounts import balance\n\n\n"
        "def show(request):\n"
        "    return balance(request.account)\n",
        "tests/test_accounts.py": "from store.accounts import balance\n\n\n"
        "def test_balance():\n"
        "    assert balance(None) == 0\n",
        "README.md": "balance is documented here\n",
    }
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    return tmp_path


def lookup(repo, symbols=None):
    cache = PlainCache(repo)
    git = GitService(repo.parent / "cache", allow_local=True)
    wt = SimpleNamespace(path=repo, mirror=repo, head_sha="")
    return CodeLookup(git, wt, cache, symbols or SymbolIndex()), cache


async def test_symbol_in_an_unchanged_file_returns_its_complete_definition(repo):
    finder, _ = lookup(repo)
    found = await finder.symbol("balance")
    assert list(found) == ["store/accounts.py"]
    value = found["store/accounts.py"]
    assert (value["line_start"], value["line_end"]) == (1, 4)
    assert "raise ValueError" in value["code"]


async def test_callers_are_call_sites_not_definitions_or_arbitrary_tests(repo):
    finder, _ = lookup(repo)
    found = await finder.callers("balance")
    files = [value["file"] for value in found.values()]
    assert "store/accounts.py" not in files
    # Source callers rank before tests; prose files are not read as code scope.
    assert files[0] == "api/views.py"
    view = next(v for v in found.values() if v["file"] == "api/views.py")
    assert "def show" in view["code"]


async def test_tests_for_a_path_or_a_symbol(repo):
    finder, _ = lookup(repo)
    by_path = await finder.tests_for("store/accounts.py")
    assert list(by_path) == ["tests/test_accounts.py"]
    by_symbol = await finder.tests_for("balance")
    assert {v["file"] for v in by_symbol.values()} == {"tests/test_accounts.py"}


async def test_option_like_or_pattern_targets_search_nothing(repo):
    finder, cache = lookup(repo)
    assert await finder.callers("--open-files-in-pager=sh") == {}
    assert await finder.callers(".*") == {}
    assert await finder.symbol("--help") == {}
    assert cache.reads == []


def test_definition_and_test_path_heuristics():
    assert defines("func (s *Store) Get() {", "Get")
    assert defines("  async fetchAll(id: string) {", "fetchAll")
    assert defines("export const handler = () => {", "handler")
    assert not defines("    return balance(x)", "balance")
    assert is_test("pkg/store_test.go") and is_test("src/a.spec.ts")
    assert is_test("tests/unit/helpers.py") and not is_test("src/contest.py")
