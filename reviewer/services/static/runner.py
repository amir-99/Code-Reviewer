import asyncio
from time import monotonic
from uuid import uuid4

from reviewer.context.models import StaticAnalysisResult
from reviewer.telemetry.activity import activity


class StaticRunner:
    def __init__(self, enabled=False):
        self.enabled = enabled

    def argv(self, tool, path, name):
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--mount",
            f"type=bind,source={path.resolve()},target=/src,readonly",
            "--workdir",
            "/src",
            tool.image,
            *tool.command,
        ]

    @activity("tool", "Static analyzer")
    async def run(self, tool, path):
        start = monotonic()
        code = None
        output = ""
        status = "skipped"
        name = "review-static-" + uuid4().hex
        if self.enabled:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.argv(tool, path, name),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                async with asyncio.timeout(tool.timeout_s):
                    while chunk := await process.stdout.read(8192):
                        output = (output + chunk.decode(errors="replace"))[-16000:]
                    code = await process.wait()
                status = "passed" if code == 0 else "failed"
                if code in {125, 126, 127}:
                    status = "errored"
            except Exception:
                status = "errored"
                output = "Static tool unavailable or timed out"
            finally:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await cleanup.wait()
        return StaticAnalysisResult(
            name=tool.name,
            required=tool.required,
            exit_code=code,
            stdout_tail=output,
            duration_s=monotonic() - start,
            status=status,
        )
