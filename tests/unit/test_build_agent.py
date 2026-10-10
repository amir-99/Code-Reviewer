import os
from types import SimpleNamespace

import pytest
from test_stages import bundle

from reviewer.agents.build_agent import BuildAgent, BuildStep
from reviewer.config.schema import BuildConfig, ProjectConfig
from reviewer.context.redaction import Redactor
from reviewer.services.build import sandbox
from reviewer.services.build.sandbox import BuildSandbox, RunResult

IMAGE = "golang@sha256:" + "a" * 64
COMPILE_ERROR = (
    "# example.com/memory/internal/repository/qdrant\n"
    "internal/repository/qdrant/collection.go:64:18: c.tracer undefined "
    "(type *Client has no field or method tracer)\n"
)


def config(**build):
    return ProjectConfig(
        build={"enabled": True, "images": {"go": IMAGE}, **build},
    )


def repository(tmp_path):
    root = tmp_path / "checkout"
    (root / "internal/repository/qdrant").mkdir(parents=True)
    (root / "go.mod").write_text("module example.com/memory\n")
    (root / "internal/repository/qdrant/collection.go").write_text(
        "package qdrant\n" * 80
    )
    (root / ".git").write_text("gitdir: /elsewhere\n")
    return root


def settings(tmp_path):
    return SimpleNamespace(static_enabled=True, static_workspace_root=tmp_path / "ws")


class Steps:
    """A scripted build agent: one BuildStep per call."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.prompts = []
        self.specs = {"build_agent": SimpleNamespace(context_tokens=100000)}

    async def complete(self, **kwargs):
        self.prompts.append(kwargs["user"])
        step = self.steps.pop(0) if self.steps else {"action": "finish"}
        return BuildStep.model_validate(
            {
                "thought": "",
                "path": None,
                "line_start": None,
                "line_end": None,
                "ecosystem": None,
                "argv": [],
                "purpose": None,
                "failures": [],
                "summary": "",
                **step,
            }
        )


@pytest.fixture
def commands(monkeypatch):
    """Sandbox commands, answered from a table instead of a Docker daemon."""
    seen = []
    table = {}

    async def run(self, ecosystem, command, timeout_s, network=""):
        # The command sees the copy, never the checkout's Git metadata.
        assert (self.src / "go.mod").is_file() and not (self.src / ".git").exists()
        seen.append((list(command), network))
        code, output = table.get(" ".join(command), (0, ""))
        status = "passed" if code == 0 else "failed"
        return RunResult(list(command), ecosystem, code, output, 0.1, status)

    monkeypatch.setattr(BuildSandbox, "run", run)
    return SimpleNamespace(seen=seen, table=table)


async def test_baseline_build_runs_and_compiler_errors_become_findings(
    tmp_path, commands
):
    commands.table["go build ./..."] = (1, COMPILE_ERROR)
    b = bundle(tmp_path, 1)
    agent = BuildAgent(settings(tmp_path), config(), Redactor([]))
    outcome = await agent.run(b, SimpleNamespace(path=repository(tmp_path)), Steps())

    # The agent finished at once; code still compiled the change.
    assert [c for c, _ in commands.seen] == [["go", "build", "./..."]]
    assert [(s.name, s.status) for s in outcome.static] == [
        ("build: go build ./...", "failed")
    ]
    (finding,) = outcome.diagnostics
    assert finding.anchor.file == "internal/repository/qdrant/collection.go"
    assert finding.anchor.line_start == 64
    assert "c.tracer undefined" in finding.claim
    # The temporary copy is gone after the review.
    assert os.listdir(tmp_path / "ws" / "builds") == []


async def test_agent_commands_are_limited_and_attributions_checked(tmp_path, commands):
    commands.table["go test -short ./internal/..."] = (1, "--- FAIL: TestSearch")
    steps = Steps(
        {"action": "list", "path": "../"},
        {"action": "run", "ecosystem": "go", "argv": ["curl", "http://x"]},
        {"action": "run", "ecosystem": "node", "argv": ["npm", "test"]},
        {
            "action": "run",
            "ecosystem": "go",
            "argv": ["go", "test", "-short", "./internal/..."],
            "purpose": "test",
        },
        {
            "action": "finish",
            "failures": [
                # Run 2 failed and names a real file: proposed for verification.
                {
                    "run": 2,
                    "file": "internal/repository/qdrant/collection.go",
                    "line": 12,
                    "message": "TestSearch fails",
                },
                # Run 1 passed; a missing file; an out-of-range run.
                {"run": 1, "file": "go.mod", "line": 1, "message": "no"},
                {"run": 2, "file": "missing.go", "line": 1, "message": "no"},
                {"run": 9, "file": "go.mod", "line": 1, "message": "no"},
            ],
        },
    )
    b = bundle(tmp_path, 1)
    agent = BuildAgent(settings(tmp_path), config(required=True), Redactor([]))
    outcome = await agent.run(b, SimpleNamespace(path=repository(tmp_path)), steps)

    assert [c for c, _ in commands.seen] == [
        ["go", "build", "./..."],
        ["go", "test", "-short", "./internal/..."],
    ]
    history = steps.prompts[-1]
    assert history.count("rejected:") == 3
    (proposal,) = outcome.proposals
    assert proposal.anchor.line_start == 12 and proposal.confidence == "medium"
    assert not outcome.diagnostics
    # Pass and fail come from the exit status; required applies to build/test.
    assert [(s.status, s.required) for s in outcome.static] == [
        ("passed", True),
        ("failed", True),
    ]


async def test_fetch_is_the_only_networked_step(tmp_path, commands):
    agent = BuildAgent(
        settings(tmp_path),
        config(fetch_network="deps-proxy", fetch_env={"GOPROXY": "https://proxy"}),
        Redactor([]),
    )
    outcome = await agent.run(
        bundle(tmp_path, 1), SimpleNamespace(path=repository(tmp_path)), Steps()
    )
    assert commands.seen == [
        (["go", "mod", "download"], "deps-proxy"),
        (["go", "build", "./..."], ""),
    ]
    assert not outcome.static[0].required


async def test_build_is_off_unless_enabled_and_sandboxed(tmp_path, commands):
    b = bundle(tmp_path, 1)
    wt = SimpleNamespace(path=repository(tmp_path))
    off = ProjectConfig()
    outcome = await BuildAgent(settings(tmp_path), off, Redactor([])).run(
        b, wt, Steps()
    )
    assert outcome.static == [] and "build_unavailable" not in b.degradations
    unsandboxed = SimpleNamespace(
        static_enabled=False, static_workspace_root=tmp_path / "ws"
    )
    outcome = await BuildAgent(unsandboxed, config(), Redactor([])).run(b, wt, Steps())
    assert outcome.static == [] and "build_unavailable" in b.degradations
    assert not commands.seen


def test_sandbox_command_isolation(tmp_path):
    box = BuildSandbox(True, tmp_path, BuildConfig(images={"go": IMAGE}))
    box.root = tmp_path / "review-build-x"
    argv = box.argv(IMAGE, ["go", "test", "./..."], "go", "", "name")
    assert argv[argv.index("--network") + 1] == "none"
    assert {"--read-only", "--rm"} <= set(argv)
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "GOPROXY=off" in argv
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts == [f"type=bind,source={box.root.resolve()},target=/work"]
    assert not any("docker.sock" in a for a in argv)
    assert argv[-4:] == [IMAGE, "go", "test", "./..."]
    fetch = box.argv(IMAGE, ["go", "mod", "download"], "go", "deps", "name")
    assert fetch[fetch.index("--network") + 1] == "deps"


def test_build_images_must_be_pinned_and_programs_bare():
    with pytest.raises(ValueError):
        BuildConfig(images={"go": "golang:1.23"})
    with pytest.raises(ValueError):
        BuildConfig(programs=["/bin/sh"])


async def test_workspace_with_read_only_module_cache_is_deleted(tmp_path):
    box = BuildSandbox(True, tmp_path, BuildConfig())
    async with box:
        root = box.root
        cache = root / "cache" / "mod" / "pkg"
        cache.mkdir(parents=True)
        (cache / "go.mod").write_text("module x\n")
        os.chmod(cache / "go.mod", 0o444)
        os.chmod(cache, 0o555)
    assert not root.exists()


def test_detected_ecosystems_follow_root_lockfiles(tmp_path):
    (tmp_path / "go.mod").write_text("")
    (tmp_path / "package-lock.json").write_text("{}")
    assert sandbox.detect(tmp_path) == ["go", "node"]


def test_a_verbose_but_valid_finish_step_is_accepted():
    # The live agent's finish step on MR !12 ran just past the first limits
    # and failed the whole step; prose length must not discard its result.
    step = BuildStep.model_validate(
        {
            "thought": "The whole-module build failed. " * 12,
            "action": "finish",
            "path": None,
            "line_start": None,
            "line_end": None,
            "ecosystem": None,
            "argv": [],
            "purpose": None,
            "failures": [
                {"run": 2, "file": "a.go", "line": 1, "message": "undefined " * 40}
            ],
            "summary": "go build ./... failed in the qdrant package. " * 12,
        }
    )
    assert step.action == "finish"
