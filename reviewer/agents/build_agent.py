"""Build and test the reviewed head in the sandbox, steered by a small agent.

Code owns everything that decides an outcome. It detects the ecosystems,
picks each image, downloads dependencies with a fixed script-free command, and
always runs a baseline build, so a merge request's text cannot talk the review
out of compiling it. The agent then chooses further commands (static checks,
the changed packages' unit tests) from a list of allowed programs, inside the
same no-network sandbox. Pass and fail come from exit statuses. Compiler
diagnostics are parsed by code into findings that need no judge; failures the
agent attributes to code are proposals that go through validation and the
independent verifier like any other.
"""

import asyncio
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.context.models import StaticAnalysisResult
from reviewer.findings.models import (
    Anchor,
    Confidence,
    Evidence,
    ProposedFinding,
    Schema,
    Severity,
)
from reviewer.services.build.sandbox import BuildSandbox, detect
from reviewer.telemetry.activity import activity, record

ROLE = "build_agent"
# Runs code makes whatever the agent decides: the build floor per ecosystem.
BASELINE = {
    "go": [(["go", "build", "./..."], "build")],
    "python": [(["python", "-m", "compileall", "-q", "."], "build")],
    "node": [],
}
OBSERVATION = 3000
LISTING = 150
ARGUMENT = 300
# `path:line[:col]: message`, as Go, gcc-style tools and most linters print it,
# and `path(line,col): error TSxxxx: message` from tsc.
DIAGNOSTIC = re.compile(
    r"^\s*(?:/work/src/|\./)?(?P<file>[\w@.+/-]+\.(?:go|py|ts|tsx|js|jsx|mjs|cjs))"
    r"(?::(?P<line>\d+)(?::\d+)?:\s+|\((?P<tsline>\d+),\d+\):\s+error\s+)(?P<message>.+)$"
)
MAX_DIAGNOSTICS = 10


class Failure(Schema):
    run: int = Field(ge=1)
    file: str = Field(max_length=300)
    line: int = Field(ge=1)
    message: str = Field(max_length=1000)


# Prose fields are bounded generously: a schema cap a model overshoots by a few
# words fails the whole step, and these fields never reach a prompt unbounded.
class BuildStep(Schema):
    thought: str = Field(max_length=2000)
    action: Literal["list", "read", "run", "finish"]
    path: str | None = Field(max_length=300)
    line_start: int | None = Field(ge=1)
    line_end: int | None = Field(ge=1)
    ecosystem: Literal["go", "node", "python"] | None
    argv: list[str] = Field(max_length=24)
    purpose: Literal["build", "vet", "test", "lint", "other"] | None
    failures: list[Failure] = Field(max_length=10)
    summary: str = Field(max_length=4000)


class Run:
    def __init__(self, result, purpose, baseline):
        self.result, self.purpose, self.baseline = result, purpose, baseline

    def view(self, index):
        return {
            "run": index,
            "argv": self.result.argv,
            "ecosystem": self.result.ecosystem,
            "purpose": self.purpose,
            "exit_code": self.result.exit_code,
            "status": self.result.status,
            "output_tail": self.result.output[-OBSERVATION:],
        }


def inside(root, path):
    """`path` resolved under `root` without leaving it or crossing a symlink."""
    if path is None or "\x00" in path or PurePosixPath(path).is_absolute():
        return None
    parts = [p for p in PurePosixPath(path).parts if p not in {"", "."}]
    if ".." in parts or ".git" in parts:
        return None
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            return None
    return target


def diagnostics(runs, src):
    """Compiler and checker diagnostics of failed build/vet runs, deduplicated."""
    found = {}
    for index, run in enumerate(runs, 1):
        # Test output also prints assertion locations; only the agent may
        # attribute those, and the verifier then judges the attribution.
        if run.result.status != "failed" or run.purpose not in {"build", "vet"}:
            continue
        for raw in run.result.output.splitlines():
            match = DIAGNOSTIC.match(raw)
            if not match:
                continue
            file = match["file"]
            line = int(match["line"] or match["tsline"])
            target = inside(src, file)
            if target is None or not target.is_file():
                continue
            found.setdefault((file, line), (index, run, match["message"].strip()))
            if len(found) >= MAX_DIAGNOSTICS:
                return found
    return found


def command(run):
    return " ".join(run.result.argv)[:200]


def deterministic(found):
    """Findings code establishes from a command's own diagnostic output."""
    proposals = []
    for (file, line), (_, run, message) in found.items():
        tool = "Build" if run.purpose == "build" else "Check"
        proposals.append(
            ProposedFinding(
                anchor=Anchor(file=file, line_start=line, line_end=line),
                category="correctness",
                severity_proposed=Severity.REQUIRED,
                claim=f"{tool} fails: {message}"[:200],
                reason=f"`{command(run)}` exited {run.result.exit_code}: "
                f"{file}:{line}: {message}"[:600],
                impact="The reviewed head does not pass this check, so what "
                "depends on it cannot be built or tested."[:600],
                failure_scenario=f"`{command(run)}` at the reviewed head reports "
                f"{file}:{line}: {message}"[:300],
                evidence=[
                    Evidence(
                        file=file, line_start=line, line_end=line, note=message[:200]
                    )
                ],
                suggested_direction="Fix the reported error so the command passes.",
                confidence=Confidence.HIGH,
            )
        )
    return proposals


def attributed(failures, runs, src, known):
    """The agent's attributions that name a failed run and a real file."""
    proposals = []
    for failure in failures:
        if not 1 <= failure.run <= len(runs):
            continue
        run = runs[failure.run - 1]
        target = inside(src, failure.file)
        if (
            run.result.status != "failed"
            or (failure.file, failure.line) in known
            or target is None
            or not target.is_file()
        ):
            continue
        known.add((failure.file, failure.line))
        kind = "Test" if run.purpose == "test" else "Check"
        proposals.append(
            ProposedFinding(
                anchor=Anchor(
                    file=failure.file, line_start=failure.line, line_end=failure.line
                ),
                category="correctness",
                severity_proposed=Severity.REQUIRED,
                claim=f"{kind} fails: {failure.message}"[:200],
                reason=f"`{command(run)}` exited {run.result.exit_code}."[:600],
                impact="The reviewed head fails this check in the sandbox.",
                failure_scenario=f"`{command(run)}` fails: {failure.message}"[:300],
                evidence=[
                    Evidence(
                        file=failure.file,
                        line_start=failure.line,
                        line_end=failure.line,
                        note=failure.message[:200],
                    )
                ],
                suggested_direction="Fix the failure the command reports.",
                confidence=Confidence.MEDIUM,
            )
        )
    return proposals


class BuildOutcome:
    def __init__(self):
        self.static = []
        # Diagnostics code parsed itself, and the agent's attributions.
        self.diagnostics = []
        self.proposals = []
        self.summary = ""


class BuildAgent:
    name = ROLE
    tier = ROLE

    def __init__(self, settings, config, redactor, context_provider=None):
        self.settings, self.config = settings, config
        self.build = config.build
        self.redactor = redactor
        self.lookup = getattr(context_provider, "lookup", None)

    @property
    def prompt_version(self):
        return PROMPTS[ROLE][0]

    def static(self, run, index):
        name = f"{'fetch' if run.purpose == 'fetch' else 'build'}: {command(run)}"
        return StaticAnalysisResult(
            name=name[:120],
            required=self.build.required and run.purpose in {"build", "test"},
            exit_code=run.result.exit_code,
            stdout_tail=self.redactor.text(run.result.output),
            duration_s=run.result.duration_s,
            status=run.result.status,
        )

    @activity("phase", "Build and test")
    async def run(self, bundle, wt, llm):
        outcome = BuildOutcome()
        spec = getattr(llm, "specs", {}).get(ROLE)
        if not self.build.enabled:
            return outcome
        if (
            not self.settings.static_enabled
            or not self.build.images
            or llm is None
            or spec is None
        ):
            bundle.degradations.append("build_unavailable")
            return outcome
        runs = []
        try:
            async with asyncio.timeout(self.build.timeout_s):
                async with BuildSandbox(
                    True,
                    Path(self.settings.static_workspace_root) / "builds",
                    self.build,
                ) as box:
                    await box.copy(wt.path)
                    ecosystems = [e for e in detect(box.src) if e in self.build.images]
                    if not ecosystems:
                        bundle.degradations.append("build_unavailable")
                        return outcome
                    for ecosystem in ecosystems:
                        for result in await box.fetch(ecosystem):
                            runs.append(Run(result, "fetch", True))
                        for argv, purpose in BASELINE[ecosystem]:
                            result = await box.run(
                                ecosystem, argv, self.build.command_timeout_s
                            )
                            runs.append(Run(result, purpose, True))
                    try:
                        failures = await self.explore(
                            bundle, box, llm, ecosystems, runs
                        )
                    except Exception:
                        # The baseline stands without the agent's choices.
                        failures = []
                        bundle.degradations.append("build_agent_unavailable")
                    found = diagnostics(runs, box.src)
                    outcome.diagnostics = deterministic(found)
                    outcome.proposals = attributed(failures, runs, box.src, set(found))
        except TimeoutError:
            bundle.degradations.append("build_timeout")
        except Exception:
            bundle.degradations.append("build_unavailable")
        outcome.static = [self.static(run, i) for i, run in enumerate(runs, 1)]
        if any(r.status == "errored" for r in outcome.static):
            bundle.degradations.append("build_unavailable")
        await record(
            "build_checks",
            dict(
                name=ROLE,
                runs=len(runs),
                failed=sum(r.status == "failed" for r in outcome.static),
                diagnostics=len(outcome.diagnostics),
                attributed=len(outcome.proposals),
            ),
        )
        return outcome

    def state(self, bundle, ecosystems, runs, history):
        changed = [f.path for f in bundle.code.files if not f.is_excluded]
        return {
            "ecosystems": ecosystems,
            "programs": self.build.programs,
            "runs_left": self.build.max_runs - sum(not r.baseline for r in runs),
            "changed_paths": changed[:80],
            "runs": [run.view(i) for i, run in enumerate(runs, 1)],
            "history": history,
        }

    async def observe(self, step, box, ecosystems, runs):
        """Carry out one action; return what the agent is shown."""
        if step.action == "list":
            target = inside(box.src, step.path or "")
            if target is None or not target.is_dir():
                return "rejected: not a directory inside the repository"
            entries = sorted(
                e.name + ("/" if e.is_dir(follow_symlinks=False) else "")
                for e in os.scandir(target)
                if e.name != ".git"
            )
            return "\n".join(entries[:LISTING]) + (
                f"\n... {len(entries) - LISTING} more" if len(entries) > LISTING else ""
            )
        if step.action == "read":
            if self.lookup is None or inside(box.src, step.path) is None:
                return "rejected: file reads are unavailable"
            found = await self.lookup.file(step.path, step.line_start, step.line_end)
            value = found.get(step.path) or {}
            return (value.get("code") or "unavailable")[:OBSERVATION]
        if step.action == "run":
            if sum(not r.baseline for r in runs) >= self.build.max_runs:
                return "rejected: no runs left; finish"
            if step.ecosystem not in ecosystems:
                return "rejected: ecosystem must be one of state.ecosystems"
            argv = step.argv
            if (
                not argv
                or argv[0] not in self.build.programs
                or any("\x00" in a or len(a) > ARGUMENT for a in argv)
            ):
                return "rejected: argv[0] must be one of state.programs"
            result = await box.run(step.ecosystem, argv, self.build.command_timeout_s)
            runs.append(Run(result, step.purpose or "other", False))
            return (
                f"run {len(runs)} finished: {result.status} (exit {result.exit_code})"
            )
        return "rejected: unknown action"

    async def explore(self, bundle, box, llm, ecosystems, runs):
        system = PROMPTS[ROLE][1] + "\n" + INJECTION_RULE
        history = []
        for _ in range(self.build.max_steps):
            user = frame(
                self.redactor.text(
                    json.dumps(
                        self.state(bundle, ecosystems, runs, history),
                        ensure_ascii=False,
                    )
                ),
                "build-state",
            )
            step = await llm.complete(
                stage=ROLE,
                tier=ROLE,
                system=system,
                user=user,
                response_model=BuildStep,
                review_id=bundle.review_id,
                timeout_s=120,
                prompt_version=self.prompt_version,
            )
            if step.action == "finish":
                return step.failures
            observation = await self.observe(step, box, ecosystems, runs)
            # Older observations shrink so the state stays bounded.
            for item in history:
                item["observation"] = item["observation"][:300]
            history.append(
                {
                    "action": step.action,
                    "path": step.path,
                    "argv": step.argv,
                    "observation": observation,
                }
            )
        return []
