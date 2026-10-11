import asyncio
from time import monotonic
from uuid import uuid4

from reviewer.context.models import StaticAnalysisResult
from reviewer.telemetry.activity import activity
from reviewer.telemetry.checks import CheckOutput


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

    @activity("tool", lambda self, tool, *_, **__: f"Static analyzer: {tool.name}")
    async def run(self, tool, path, redact=None):
        """Run one analyzer; its output streams to activity through `redact`."""
        start = monotonic()
        code = None
        output = ""
        status = "skipped"
        name = "review-static-" + uuid4().hex
        check = CheckOutput(tool.name, redact)
        await check.start()
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
                        text = chunk.decode(errors="replace")
                        output = (output + text)[-16000:]
                        await check.feed(text)
                    code = await process.wait()
                status = "passed" if code == 0 else "failed"
                if code in {125, 126, 127}:
                    status = "errored"
            except Exception:
                status = "errored"
                output = "Static tool unavailable or timed out"
                await check.feed("\n" + output + "\n")
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
        await check.finish(status, code, monotonic() - start)
        return StaticAnalysisResult(
            name=tool.name,
            required=tool.required,
            exit_code=code,
            stdout_tail=output,
            duration_s=monotonic() - start,
            status=status,
        )
