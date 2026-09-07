import asyncio
import json
import tempfile
from pathlib import Path


class ScanError(RuntimeError):
    pass


class SecretScanner:
    def __init__(self, binary="gitleaks"):
        self.binary = binary

    async def scan(self, files):
        with tempfile.TemporaryDirectory(prefix="review-scan-") as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            mapping = {}
            for index, file in enumerate(files):
                if not file.lines:
                    continue
                # Flat generated filenames prevent path traversal; preserve head line
                # numbers for scanner anchors. Removed lines cannot introduce a leak.
                lines = {
                    line.new_line: line.text
                    for line in file.lines
                    if line.new_line is not None
                }
                if not lines:
                    continue
                if max(lines) > 200000:
                    raise ScanError("Secret scan input exceeds budget")
                name = f"file-{index}.txt"
                mapping[name] = file.path
                (source / name).write_text(
                    "\n".join(lines.get(n, "") for n in range(1, max(lines) + 1))
                )
            report = root / "report.json"
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "detect",
                "--no-git",
                "--source",
                str(source),
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--no-banner",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(60):
                    await process.wait()
                if process.returncode not in {0, 1}:
                    raise ScanError("Secret scanner unavailable")
                data = json.loads(report.read_text()) if report.exists() else []
                for item in data:
                    item["File"] = mapping.get(Path(item["File"]).name, "")
                return data
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


class FakeSecretScanner:
    def __init__(self, matches=()):
        self.matches = list(matches)

    async def scan(self, files):
        return self.matches


def findings(matches, bundle):
    from hashlib import sha256
    from uuid import uuid4

    from reviewer.findings.dedup import fingerprint
    from reviewer.findings.models import (
        Anchor,
        Evidence,
        Finding,
        Provenance,
        VerificationResult,
    )

    output = []
    for m in matches:
        path = m["File"]
        line = int(m["StartLine"])
        rule = m.get("RuleID", "secret")
        # Only newly added lines introduce a credential. Context matches are
        # still redacted but cannot become a blocking introduced finding.
        changed = next((f for f in bundle.code.files if f.path == path), None)
        if not changed or not any(
            diff_line.new_line == line and diff_line.kind == "added"
            for diff_line in changed.lines
        ):
            continue
        output.append(
            Finding(
                id=str(uuid4()),
                fingerprint=fingerprint(
                    bundle.mr.project_id,
                    path,
                    "security",
                    "Credential introduced in source code",
                ),
                stage="secrets",
                provenance=Provenance(
                    agent="gitleaks",
                    prompt_version="deterministic-v1",
                    model="none",
                    run_id=str(bundle.review_id),
                    context_bundle_hash=bundle.content_hash(),
                ),
                anchor=Anchor(
                    file=path,
                    line_start=line,
                    line_end=line,
                    commit_sha=bundle.code.head_sha,
                    context_hash=sha256(
                        next(
                            (d.text for d in changed.lines if d.new_line == line), ""
                        ).encode()
                    ).hexdigest(),
                    in_diff=True,
                    introduced_by_this_change=True,
                ),
                category="security",
                severity_proposed="BLOCKER",
                severity_final="BLOCKER",
                claim="Credential introduced in source code",
                reason=f"Secret scanner matched rule {rule}.",
                impact="The credential may grant unauthorized access.",
                failure_scenario="A reader uses the exposed credential.",
                evidence=[
                    Evidence(
                        file=path,
                        line_start=line,
                        line_end=line,
                        note="Secret value withheld.",
                    )
                ],
                suggested_direction="Remove the credential and rotate it.",
                confidence="high",
                verification=VerificationResult(
                    verdict="confirmed",
                    counterargument="Deterministic scanner result.",
                    reasoning="Credential match on an added line.",
                ),
                status="verified",
            )
        )
    return output
