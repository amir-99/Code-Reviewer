from collections import Counter
from html import escape


def render(f, explain_url=""):
    def safe(value):
        return escape(value or "").replace("@", "＠")

    return f"""**{f.severity_final}** · {safe(f.category)}

{safe(f.claim)}

{safe(f.reason)} {safe(f.impact)}

**Failure scenario:** {safe(f.failure_scenario) or "Not specified"}

**Suggested direction:** {safe(f.suggested_direction)}

<sub>AI review · reply `/ai explain` for evidence · reply `/ai dismiss <reason>` if this is wrong</sub>
<!-- ai-review:fingerprint={f.fingerprint} -->"""


def summary(bundle, decision, findings, summary_findings, overflow, stage_results):
    def issue_link(key):
        return (
            f"[{key}]({bundle.jira_base_url.rstrip('/')}/browse/{key})"
            if bundle.jira_base_url
            else key
        )

    lines = [
        f"## AI review · {decision}",
        f"Commit: `{bundle.code.head_sha}`",
        f"Issue: {bundle.issue.key if bundle.issue else 'unlinked'} · Epic: {bundle.epic.key if bundle.epic else 'none'}",
    ]
    lines.append(
        "Documentation: "
        + (
            ", ".join(
                f"[{escape(d.title)}]({d.url}) v{d.version}" for d in bundle.documents
            )
            or "none"
        )
    )
    lines += ["", "| Acceptance criterion | Coverage |", "|---|---|"]
    for ac in bundle.issue.acceptance_criteria if bundle.issue else []:
        lines.append(
            f"| {ac.id}: {escape(ac.text).replace('|', '/')} | See purpose/test coverage below |"
        )
    if not bundle.issue:
        lines.append("| Requirements | not_verifiable: unlinked |")
        lines.append(
            "\n**SUGGESTION**: No accessible mapped Jira story was linked; requirement-dependent review is not verifiable."
        )
        lines.extend(bundle.linkage.warnings)
    for stage in stage_results:
        lines.append(
            f"\n{stage.stage}: {len(stage.examined)} units examined; {len(stage.skipped)} skipped"
            + (" (failed)" if stage.failed else "")
        )
        lines.extend(escape(note) for note in stage.notes)
    counts = Counter(
        str(f.severity_final)
        for f in findings
        if f.status not in {"suppressed", "discarded", "resolved"}
    )
    lines.append(
        "\nFinding counts: "
        + (", ".join(f"{k}: {v}" for k, v in counts.items()) or "none")
    )
    lines.append(
        "Static analysis: "
        + (
            ", ".join(f"{s.name}: {s.status}" for s in bundle.static)
            or "none configured"
        )
    )
    lines.append("Degradations: " + (", ".join(bundle.degradations) or "none"))
    for f in summary_findings:
        lines.append(
            f"\n- **{f.severity_final}** {escape(f.claim)} ({escape(f.anchor.file)}:{f.anchor.line_start}) — {escape(f.reason)}"
        )
    if overflow:
        lines.append(
            "Additional findings above comment caps: "
            + ", ".join(f"{k}: {v}" for k, v in overflow.items())
        )
    lines += [
        "",
        "<sub>Machine-assisted review. No merge or GitLab approval was performed.</sub>",
        f"<!-- ai-review:summary={bundle.code.head_sha} -->",
    ]
    return "\n".join(lines)
