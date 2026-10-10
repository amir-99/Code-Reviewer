from types import SimpleNamespace

from test_stages import bundle

from reviewer.agents.base import TemplateAgent, unit_response_model
from reviewer.config.schema import ProjectConfig
from reviewer.context.builder import attach_scopes
from reviewer.context.models import ChangedFile, DiffLine, Hunk
from reviewer.context.partition import GAP, partition
from reviewer.findings.models import Coverage, StageEnvelope
from reviewer.orchestrator.stages import execute
from reviewer.services.symbols.index import SymbolIndex


def changed(path, numbers, kind="added"):
    return ChangedFile(
        path=path,
        change_type="modified",
        total_lines=100,
        lines=[
            DiffLine(text=f"line {n}", new_line=n, old_line=n, kind=kind)
            for n in numbers
        ],
    )


def test_scope_lines_widen_a_hunk_and_gaps_are_marked(tmp_path):
    b = bundle(tmp_path, 0)
    file = changed("svc.py", [10, 30])
    file.hunks = [
        Hunk(old_start=9, old_lines=0, new_start=10, new_lines=1, header=""),
        Hunk(old_start=29, old_lines=0, new_start=30, new_lines=1, header=""),
    ]
    file.scope = {n: f"scope {n}" for n in (8, 9, 11, 12)}
    b.code.files = [file]
    (unit,) = partition(b, "hunks", 24000)
    lines = unit.content.splitlines()
    assert lines[0] == "=== svc.py ==="
    assert lines[1:6] == [
        "    8   scope 8",
        "    9   scope 9",
        "   10 + line 10",
        "   11   scope 11",
        "   12   scope 12",
    ]
    assert lines[6] == GAP and lines[7] == "   30 + line 30"
    assert unit.file_context["svc.py"]["supplied_new_ranges"] == [[8, 12], [30, 30]]


def test_small_files_pack_into_one_unit_with_tests_beside_their_source(tmp_path):
    b = bundle(tmp_path, 0)
    b.code.files = [
        changed("pkg/a.py", [1]),
        changed("pkg/z.py", [1]),
        changed("tests/test_a.py", [1]),
    ]
    units = partition(b, "hunks", 24000)
    assert [u.paths for u in units] == [["pkg/a.py", "tests/test_a.py", "pkg/z.py"]]
    assert units[0].id == "pkg/a.py:unit-0"
    assert set(units[0].file_context) == {"pkg/a.py", "tests/test_a.py", "pkg/z.py"}
    # Packing is a Defect Review behaviour; other kinds keep one file per unit.
    assert len(partition(b, "file_group", 24000)) == 3


def test_packing_respects_the_limit_and_the_cap_grows_with_the_unit(tmp_path):
    b = bundle(tmp_path, 0)
    b.code.files = [changed(f"m{n}.py", range(1, 300)) for n in range(4)]
    units = partition(b, "hunks", 2000)
    assert len(units) > 1
    assert all(len(u.content.encode()) <= 2000 for u in units)
    big = partition(b, "hunks", 60000)
    assert len(big) == 1 and big[0].max_findings > units[0].max_findings == 3
    schema = unit_response_model(big[0].id, True, big[0].max_findings)
    assert (
        schema.model_json_schema()["properties"]["findings"]["maxItems"]
        == big[0].max_findings
    )


def test_prompt_puts_shared_context_first_and_code_last_verbatim(tmp_path):
    b = bundle(tmp_path, 0)
    b.code.files = [changed("a.py", [1]), changed("b.py", [1])]
    b.code.files[0].lines[0].text = 'if a < b && c: print("x")'
    first, second = partition(b, "file_group", 24000)
    agent = TemplateAgent("defect_review", unit_kind="hunks")
    _, one = agent.build_prompt(b, first)
    _, two = agent.build_prompt(b, second)
    order = [
        one.index(f'source="{s}"')
        for s in ("review-context", "review-unit", "review-diff")
    ]
    assert order == sorted(order)
    shared = one[: one.index('source="review-unit"')]
    assert two.startswith(shared)
    assert '    1 + if a < b && c: print("x")' in one
    assert agent.build_prompt(b, first)[1] is one


async def test_explicit_skip_is_not_retried(tmp_path):
    calls = []

    class Skipping:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=[],
                    units_skipped=["f0.py:unit-0"],
                    skip_reason="Needs the caller",
                ),
            )

    result = await execute(
        "tests_", bundle(tmp_path, 1), Skipping(), ProjectConfig(analysis_mode="deep")
    )
    assert len(calls) == 1
    assert result.skipped == ["f0.py:unit-0"] and result.partial


async def test_attach_scopes_reads_only_needed_lines_through_the_scanned_cache(
    tmp_path,
):
    source = "\n".join(
        ["import os", "", "def f(x):", "    a = 1", "    b = 2", "    return a", ""]
        + [f"# filler {n}" for n in range(300)]
        + ["def big():"]
        + ["    pass"] * 200
    )
    symbols = SymbolIndex()
    symbols.add("svc.py", source)
    b = bundle(tmp_path, 0)
    file = changed("svc.py", [5], kind="added")
    file.lines[0].text = "    b = 2"
    big_line = source.splitlines().index("def big():") + 3
    file.lines.append(
        DiffLine(text="    pass", new_line=big_line, old_line=None, kind="added")
    )
    b.code.files = [file]
    reads = []

    async def read_many(paths):
        reads.append(paths)
        return {"svc.py": source.replace("a = 1", "a = [REDACTED:test]")}

    await attach_scopes(b, symbols, SimpleNamespace(read_many=read_many))
    assert reads == [["svc.py"]]
    assert sorted(file.scope) == [3, 4, 6]
    assert file.scope[4] == "    a = [REDACTED:test]"
