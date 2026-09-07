from hashlib import sha256
from pathlib import Path

from reviewer.findings.models import ValidationResult
from reviewer.telemetry import DISCARDED


def read_lines(root, path):
    try:
        root = Path(root).resolve()
        file = root / path
        if (
            file.is_symlink()
            or not file.resolve().is_relative_to(root)
            or not file.is_file()
            or file.stat().st_size > 2_000_000
        ):
            return None
        return file.read_text(errors="replace").splitlines()
    except (OSError, ValueError):
        return None


def valid_range(lines, start, end):
    return lines is not None and 1 <= start <= end <= len(lines)


async def validate(finding, bundle, symbols, git=None, wt=None, open_fingerprints=()):
    a = finding.anchor
    # Reset every code-owned field even when the model supplied values.
    a.commit_sha = bundle.code.head_sha
    a.context_hash = ""
    a.in_diff = False
    a.introduced_by_this_change = False
    lines = read_lines(bundle.code.worktree_path, a.file)
    if not valid_range(lines, a.line_start, a.line_end):
        finding.status = "discarded"
        finding.validation = ValidationResult(
            valid=False, reasons=["fabricated_anchor"]
        )
        DISCARDED.labels(
            finding.provenance.agent,
            finding.provenance.prompt_version,
            "fabricated_anchor",
        ).inc()
        return finding
    a.context_hash = sha256(
        "\n".join(lines[a.line_start - 1 : a.line_end]).encode()
    ).hexdigest()
    evidence_valid = all(
        valid_range(
            read_lines(bundle.code.worktree_path, e.file), e.line_start, e.line_end
        )
        and any(
            read_lines(bundle.code.worktree_path, e.file)[e.line_start - 1 : e.line_end]
        )
        for e in finding.evidence
    )
    finding.validation = ValidationResult(
        valid=True,
        evidence_valid=evidence_valid,
        reasons=[] if evidence_valid else ["invalid_evidence"],
    )
    if not evidence_valid:
        finding.status = "downgraded"
    if a.symbol and not symbols.resolves(a.file, a.symbol):
        a.symbol = None
    changed = next((f for f in bundle.code.files if f.path == a.file), None)
    if changed:
        anchor_lines = [
            line
            for line in changed.lines
            if line.new_line and a.line_start <= line.new_line <= a.line_end
        ]
        a.in_diff = bool(anchor_lines)
        a.introduced_by_this_change = any(line.kind == "added" for line in anchor_lines)
        if a.introduced_by_this_change and git and wt:
            try:
                blamed = await git.blame_lines(wt, a.file, a.line_start, a.line_end)
                introduced = False
                for line in blamed:
                    ancestor = (
                        (
                            await git.command(
                                "merge-base",
                                bundle.code.merge_base_sha,
                                line.sha,
                                cwd=wt.mirror,
                            )
                        )
                        .decode()
                        .strip()
                    )
                    introduced |= ancestor != line.sha
                a.introduced_by_this_change = introduced
            except Exception:
                # Unknown attribution is non-blocking.
                a.introduced_by_this_change = False
    if finding.fingerprint in open_fingerprints:
        finding.status = "suppressed"
    return finding
