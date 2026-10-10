"""A throwaway copy of the reviewed head, built and tested in the sandbox.

The reviewed code is untrusted, so it never runs in the worker: every command
is a `docker run` on the operator's sandbox daemon, with no network, a
read-only root filesystem, no capabilities, an unprivileged user and resource
limits. The only writable place is the workspace bind-mounted at /work, a copy
of the pinned checkout made for this review and deleted afterwards. Only the
fetch phase has a network, and it runs a fixed download command code chooses.
"""

import asyncio
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from reviewer.telemetry.activity import activity

OUTPUT_TAIL = 8000

# Where each ecosystem keeps its caches inside the workspace, so the fetch
# phase's downloads are what the offline build and test commands find.
ENV = {
    "go": {
        "GOCACHE": "/work/cache/go-build",
        "GOMODCACHE": "/work/cache/mod",
        "GOPATH": "/work/gopath",
        "GOFLAGS": "-buildvcs=false",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
    },
    "node": {
        "npm_config_cache": "/work/cache/npm",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
    },
    "python": {
        "UV_CACHE_DIR": "/work/cache/uv",
        "PIP_CACHE_DIR": "/work/cache/pip",
        "PYTHONDONTWRITEBYTECODE": "1",
    },
}
# With no network a downloader must fail at once instead of hanging on DNS.
OFFLINE = {"go": {"GOPROXY": "off"}, "python": {"UV_OFFLINE": "1"}}

# The fetch phase: fixed per ecosystem, downloads only, no lifecycle scripts.
FETCH = {
    "go": [["go", "mod", "download"]],
    "node": [["npm", "ci", "--ignore-scripts"]],
    "python": [["uv", "sync", "--frozen", "--no-install-project", "--no-build"]],
}
MARKERS = {
    "go": ("go.mod",),
    "node": ("package-lock.json",),
    "python": ("uv.lock",),
}


@dataclass
class RunResult:
    argv: list[str]
    ecosystem: str
    exit_code: int | None
    output: str
    duration_s: float
    status: str  # passed, failed, errored


def detect(src):
    """Ecosystems whose lockfile sits at the repository root."""
    return [
        e for e, files in MARKERS.items() if any((src / f).is_file() for f in files)
    ]


def _force_remove(function, path, _exc):
    # Go's module cache is read-only by design; make it writable to delete it.
    parent = os.path.dirname(path)
    for target in (parent, path):
        try:
            os.chmod(target, os.stat(target).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
    try:
        function(path)
    except OSError:
        pass


class BuildSandbox:
    def __init__(self, enabled, parent, config):
        self.enabled = enabled
        self.parent = Path(parent)
        self.config = config
        self.root = None

    @property
    def src(self):
        return self.root / "src"

    async def __aenter__(self):
        self.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="review-build-", dir=self.parent))
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def copy(self, worktree):
        """Copy the pinned checkout without its Git metadata."""
        await asyncio.to_thread(
            shutil.copytree,
            Path(worktree),
            self.src,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
        )
        for name in ("cache", "home", "gopath"):
            (self.root / name).mkdir(exist_ok=True)

    async def close(self):
        if self.root is not None:
            root, self.root = self.root, None
            await asyncio.to_thread(shutil.rmtree, root, onexc=_force_remove)

    def argv(self, image, command, ecosystem, network, name):
        env = {
            "HOME": "/work/home",
            **ENV.get(ecosystem, {}),
            **(self.config.fetch_env if network else OFFLINE.get(ecosystem, {})),
        }
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            network or "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--memory",
            self.config.memory,
            "--cpus",
            self.config.cpus,
            # The worker's own unprivileged identity, so it can delete the
            # workspace afterwards.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            # Tests execute what they compile, so /tmp must allow exec.
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=1g",
            "--mount",
            f"type=bind,source={self.root.resolve()},target=/work",
            "--workdir",
            "/work/src",
            *(arg for key, value in env.items() for arg in ("--env", f"{key}={value}")),
            image,
            *command,
        ]

    @activity("tool", "Sandbox command")
    async def run(self, ecosystem, command, timeout_s, network=""):
        start = monotonic()
        image = self.config.images.get(ecosystem)
        if not self.enabled or not image or self.root is None:
            return RunResult(list(command), ecosystem, None, "", 0.0, "errored")
        name = "review-build-" + uuid4().hex
        output, code, status, process = "", None, "errored", None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv(image, command, ecosystem, network, name),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(timeout_s):
                while chunk := await process.stdout.read(8192):
                    output = (output + chunk.decode(errors="replace"))[-OUTPUT_TAIL:]
                code = await process.wait()
            # 125-127: the sandbox could not start the command at all.
            status = (
                "passed"
                if code == 0
                else "errored"
                if code in {125, 126, 127}
                else "failed"
            )
        except TimeoutError:
            # A hung command is not evidence about the code; fail open.
            output = (output + "\n[command timed out]")[-OUTPUT_TAIL:]
        except Exception:
            output = "Sandbox unavailable"
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            try:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await cleanup.wait()
            except Exception:
                pass
        return RunResult(
            list(command), ecosystem, code, output, monotonic() - start, status
        )

    async def fetch(self, ecosystem):
        """Download dependencies through the operator's proxy network, if any."""
        if not self.config.fetch_network:
            return []
        return [
            await self.run(
                ecosystem,
                command,
                self.config.command_timeout_s,
                network=self.config.fetch_network,
            )
            for command in FETCH[ecosystem]
        ]
