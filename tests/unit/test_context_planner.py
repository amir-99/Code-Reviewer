from types import SimpleNamespace

from test_stages import bundle

from reviewer.agents.base import DefectReviewAgent
from reviewer.agents.context_planner import (
    ContextPlan,
    ContextPlanner,
    UnitPlan,
    sections,
)
from reviewer.config.schema import ProjectConfig
from reviewer.context.excerpts import excerpt
from reviewer.context.models import ChangedFile, DiffLine
from reviewer.context.partition import partition
from reviewer.services.symbols.index import SymbolIndex

SEARCH = 'package service\n\nfunc Search() {\n\tversion := "v1"\n}\n'
MOCK = 'package embedder\n\nfunc Embed() string {\n\treturn "mock-v1"\n}\n'
SPEC = (
    "# Cache\n\nIntro.\n\n## Staleness\n\nResults are stale for at most 72 s.\n"
    "```\n# not a heading\n```\n\n## Keys\n\nEvery key carries pid_h.\n"
)
FILES = {
    "internal/service/search.go": SEARCH,
    "internal/embedder/mock.go": MOCK,
    "docs/contracts/04-cache.md": SPEC,
    "README.md": "# Readme\n\nNothing.\n",
}


class Lookup:
    def __init__(self):
        self.symbols = SymbolIndex()
        for path in ("internal/service/search.go", "internal/embedder/mock.go"):
            self.symbols.add(path, FILES[path])
        self.read = []

        async def ls_files(wt):
            return list(FILES)

        self.git = SimpleNamespace(ls_files=ls_files)
        self.wt = None

        async def read_many(paths):
            self.read.extend(paths)
            return {p: FILES[p] for p in paths if p in FILES}

        self.code_cache = SimpleNamespace(read_many=read_many)

    async def file(self, path, start=None, end=None):
        return {path: excerpt(FILES[path].splitlines(), start or 1, end)}


class Planner:
    def __init__(self, plan):
        self.plan = plan
        self.prompts = []
        self.specs = {"context_planner": SimpleNamespace(context_tokens=100000)}

    async def complete(self, **kwargs):
        self.prompts.append(kwargs)
        return ContextPlan.model_validate(self.plan(kwargs["user"]))


def change(tmp_path):
    b = bundle(tmp_path, 0)
    b.code.files = [
        ChangedFile(
            path=path,
            change_type="added",
            total_lines=len(FILES[path].splitlines()),
            lines=[
                DiffLine(text=text, new_line=n, old_line=None, kind="added")
                for n, text in enumerate(FILES[path].splitlines(), 1)
            ],
        )
        for path in ("internal/embedder/mock.go", "internal/service/search.go")
    ]
    return b


def provider_with(lookup):
    async def provider(requests):
        return {}

    provider.lookup = lookup
    return provider


def test_markdown_sections_split_at_headings_outside_code_fences():
    found = sections("docs/c.md", SPEC)
    assert [s["title"] for s in found] == ["Cache", "Staleness", "Keys"]
    assert found[1]["start"] == 5 and found[1]["end"] == 11


async def test_planned_context_is_chosen_from_menus_and_reaches_the_prompt(tmp_path):
    b = change(tmp_path)
    units = partition(b, 120)
    assert [u.paths for u in units] == [
        ["internal/embedder/mock.go"],
        ["internal/service/search.go"],
    ]
    search, mock = units[1].id, units[0].id

    def plan(user):
        return ContextPlan(
            units=[
                UnitPlan(
                    unit_id=search,
                    sections=["docs/contracts/04-cache.md#2", "nowhere.md#1"],
                    definitions=[
                        "internal/embedder/mock.go::Embed@3",
                        # The unit's own code is already in its diff.
                        "internal/service/search.go::Search@3",
                        "../etc/passwd::x@1",
                    ],
                    reason="Search must agree with the embedder's version.",
                ),
                UnitPlan(
                    unit_id="invented:unit-0", sections=[], definitions=[], reason=""
                ),
                UnitPlan(unit_id=mock, sections=[], definitions=[], reason=""),
            ]
        ).model_dump()

    lookup = Lookup()
    llm = Planner(plan)
    config = ProjectConfig()
    await ContextPlanner().plan(b, units, llm, config, provider_with(lookup))

    menu = llm.prompts[0]["user"]
    assert "docs/contracts/04-cache.md#2" in menu and "Staleness" in menu
    # Contract-like paths rank ahead of other Markdown.
    assert lookup.read.index("docs/contracts/04-cache.md") < lookup.read.index(
        "README.md"
    )
    assert set(units[1].planned) == {
        "spec:docs/contracts/04-cache.md#Staleness",
        "code:internal/embedder/mock.go::Embed",
    }
    assert (
        "72 s" in units[1].planned["spec:docs/contracts/04-cache.md#Staleness"]["code"]
    )
    assert units[0].planned == {}

    _, user = DefectReviewAgent().compose(b, units[1])
    assert user.index('source="planned-context"') < user.index('source="review-diff"')
    assert '"mock-v1"' in user.replace('\\"', '"')


async def test_planner_without_a_configured_role_changes_nothing(tmp_path):
    b = change(tmp_path)
    units = partition(b, 120)
    llm = Planner(lambda user: {"units": []})
    llm.specs = {}
    await ContextPlanner().plan(b, units, llm, ProjectConfig(), provider_with(Lookup()))
    assert not llm.prompts and all(u.planned == {} for u in units)


async def test_planner_failure_degrades_without_failing_the_review(tmp_path):
    b = change(tmp_path)
    units = partition(b, 120)

    def broken(user):
        raise RuntimeError("gateway down")

    await ContextPlanner().plan(
        b, units, Planner(broken), ProjectConfig(), provider_with(Lookup())
    )
    assert "context_plan_unavailable" in b.degradations
    assert all(u.planned == {} for u in units)


async def test_planned_context_respects_its_byte_budget(tmp_path):
    b = change(tmp_path)
    units = partition(b, 120)
    search = units[1].id
    config = ProjectConfig(review={"planned_context_bytes": 0})

    def plan(user):
        return {
            "units": [
                {
                    "unit_id": search,
                    "sections": ["docs/contracts/04-cache.md#2"],
                    "definitions": [],
                    "reason": "",
                }
            ]
        }

    await ContextPlanner().plan(
        b, units, Planner(plan), config, provider_with(Lookup())
    )
    assert units[1].planned == {}
