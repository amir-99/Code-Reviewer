from collections import Counter
from html import escape

SEVERITY_ICON = {
    "BLOCKER": "🛑",
    "REQUIRED": "🔴",
    "SUGGESTION": "💡",
    "NIT": "🔧",
    "QUESTION": "❓",
    "FYI": "ℹ️",
    "PRAISE": "🌟",
}
DECISION_ICON = {"APPROVE": "✅", "COMMENT_ONLY": "💬", "REQUEST_CHANGES": "🚧"}
# Worst first, so the reader meets blockers before praise.
SEVERITY_ORDER = list(SEVERITY_ICON)


def safe(value):
    """Escape reviewed content and defuse @-mentions before it reaches GitLab."""
    return escape(str(value or "")).replace("@", "＠")


def oneline(value):
    """Collapse newlines so escaped text can sit inside a list item."""
    return " ".join(safe(value).split())


def cell(value):
    """Escape for a table cell, where an unescaped pipe would break the row."""
    return oneline(value).replace("|", "\\|")


def icon(severity):
    return SEVERITY_ICON.get(str(severity or ""), "•")


def impact_label(f):
    return str(f.impact_level or "unknown")


def location(anchor):
    span = (
        str(anchor.line_start)
        if anchor.line_end in (None, anchor.line_start)
        else f"{anchor.line_start}-{anchor.line_end}"
    )
    return f"{anchor.file}:{span}"


def render(f, explain_url=""):
    lines = [
        f"### {icon(f.severity_final)} {safe(f.severity_final)} · {safe(f.category)}",
        "",
        f"**{safe(f.claim)}**",
        "",
        "| | |",
        "|---|---|",
        f"| 📍 Location | `{cell(location(f.anchor))}` |",
        f"| 🧭 Confidence | {cell(f.confidence)} |",
        f"| Impact level (advisory) | {cell(impact_label(f))} |",
    ]
    if f.requirement_ref:
        lines.append(f"| 🔗 Requirement | {cell(f.requirement_ref)} |")
    lines += [
        "",
        f"**Why** — {safe(f.reason)}",
        "",
        f"**Impact** — {safe(f.impact)}",
        "",
        "**💥 Failure scenario**",
        "",
        safe(f.failure_scenario) or "_Not specified_",
        "",
        "**🛠️ Suggested direction**",
        "",
        safe(f.suggested_direction),
    ]
    if f.evidence:
        lines += ["", "<details><summary>📎 Evidence</summary>", ""]
        lines += [
            f"- `{cell(e.file)}:{e.line_start}-{e.line_end}` — {oneline(e.note)}"
            for e in f.evidence
        ]
        lines += ["", "</details>"]
    lines += [
        "",
        "<sub>AI review · reply `/ai explain` for evidence · reply `/ai dismiss <reason>` if this is wrong</sub>",
        f"<!-- ai-review:fingerprint={f.fingerprint} -->",
    ]
    return "\n".join(lines)


def summary(bundle, decision, findings, summary_findings, overflow, stage_results):
    def issue_link(key):
        return (
            f"[{key}]({bundle.jira_base_url.rstrip('/')}/browse/{key})"
            if bundle.jira_base_url
            else key
        )

    active = [
        f for f in findings if f.status not in {"suppressed", "discarded", "resolved"}
    ]
    counts = Counter(str(f.severity_final) for f in active)
    partial = "partial" in bundle.degradations
    documents = (
        ", ".join(f"[{cell(d.title)}]({d.url}) v{d.version}" for d in bundle.documents)
        or "_none_"
    )
    tally = (
        " · ".join(
            f"{icon(s)} {counts[s]} {s}" for s in SEVERITY_ORDER if counts.get(s)
        )
        or "none"
    )

    lines = [
        f"## 🤖 AI Code Review · {DECISION_ICON.get(str(decision), '💬')} {safe(decision)}",
        "",
        "| | |",
        "|---|---|",
        f"| 📦 Commit | `{cell(bundle.code.head_sha)}` |",
        f"| 🎫 Issue | {issue_link(cell(bundle.issue.key)) if bundle.issue else '_unlinked_'} |",
        f"| 🗂️ Epic | {issue_link(cell(bundle.epic.key)) if bundle.epic else '_none_'} |",
        f"| 📚 Documentation | {documents} |",
        f"| 🧭 Coverage | {'⚠️ partial' if partial else '✅ complete'} |",
        f"| 🔎 Findings | {tally} |",
    ]

    lines += ["", "### 📋 Acceptance criteria", ""]
    criteria = bundle.issue.acceptance_criteria if bundle.issue else []
    if criteria:
        lines += ["| # | Criterion | Coverage |", "|---|---|---|"]
        lines += [
            f"| {cell(ac.id)} | {cell(ac.text)} | See stage notes below |"
            for ac in criteria
        ]
    elif bundle.issue:
        lines.append("_No acceptance criteria were found on the linked issue._")
    else:
        lines.append(
            "⚠️ **SUGGESTION**: No accessible mapped Jira story was linked; "
            "requirement-dependent review is not verifiable."
        )
        lines += [f"- {oneline(w)}" for w in bundle.linkage.warnings]

    lines += ["", "### 🔎 Findings", ""]
    if active:
        lines += [
            "| Disposition | Impact (advisory) | Category | Location | Claim |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {icon(f.severity_final)} {cell(f.severity_final)} | {cell(impact_label(f))} | {cell(f.category)} "
            f"| `{cell(location(f.anchor))}` | {cell(f.claim)} |"
            for f in sorted(
                active,
                key=lambda f: (
                    SEVERITY_ORDER.index(str(f.severity_final))
                    if str(f.severity_final) in SEVERITY_ORDER
                    else len(SEVERITY_ORDER),
                    f.anchor.file,
                    f.anchor.line_start,
                ),
            )
        ]
    else:
        lines.append("✅ No findings were raised on this change.")

    if summary_findings:
        lines += [
            "",
            "### 💬 Not posted inline",
            "",
            "_Summary-only severities, unpositionable anchors, or findings over the per-MR caps._",
            "",
        ]
        lines += [
            f"- {icon(f.severity_final)} **{safe(f.severity_final)}** "
            f"[impact: {cell(impact_label(f))}, advisory] "
            f"`{cell(location(f.anchor))}` — {oneline(f.claim)} · {oneline(f.reason)}"
            for f in summary_findings
        ]
    if overflow:
        lines.append(
            "\nℹ️ Additional findings above comment caps: "
            + ", ".join(f"{cell(k)}: {v}" for k, v in overflow.items())
        )

    lines += [
        "",
        "### 📊 Stage coverage",
        "",
        "| Stage | Examined | Skipped | Status |",
        "|---|---|---|---|",
    ]
    for stage in stage_results:
        status = (
            "❌ failed"
            if stage.failed
            else ("⚠️ partial" if stage.skipped else "✅ complete")
        )
        lines.append(
            f"| {cell(stage.stage)} | {len(stage.examined)} | {len(stage.skipped)} | {status} |"
        )
    notes = [(s.stage, note) for s in stage_results for note in s.notes]
    if notes:
        lines += ["", "<details><summary>📝 Stage notes</summary>", ""]
        lines += [f"- **{cell(stage)}** — {oneline(note)}" for stage, note in notes]
        lines += ["", "</details>"]

    lines += [
        "",
        "### 🧰 Static analysis",
        "",
        ", ".join(f"`{cell(s.name)}`: {cell(s.status)}" for s in bundle.static)
        or "_none configured_",
        "",
        "### ⚠️ Degradations",
        "",
        ", ".join(f"`{cell(d)}`" for d in bundle.degradations) or "_none_",
        "",
        "<sub>Machine-assisted review. No merge or GitLab approval was performed.</sub>",
        f"<!-- ai-review:summary={bundle.code.head_sha} -->",
    ]
    return "\n".join(lines)


RECHECK_ICON = {
    "fixed": "✅",
    "partially_fixed": "🟡",
    "not_fixed": "🔴",
    "obsolete": "⚪",
    "unverifiable": "❔",
}
RECHECK_LABEL = {
    "fixed": "Fixed",
    "partially_fixed": "Partially fixed",
    "not_fixed": "Still open",
    "obsolete": "No longer applies",
    "unverifiable": "Could not verify",
}


def recheck_marker(fingerprint, head_sha):
    """Identifies a recheck reply by finding and by the head it judged.

    Delivery is at-least-once and a push may arrive while a run is in flight, so
    the marker is the record that this thread was already answered for this
    commit. A later head produces a different marker and a fresh reply.
    """
    return f"<!-- ai-review:recheck={fingerprint}@{head_sha} -->"


def render_recheck(verdict, head_sha):
    """The threaded reply that reports whether a pushed change answered a comment."""
    from reviewer.findings.models import RECHECK_RESOLVING

    lines = [
        f"### {RECHECK_ICON.get(verdict.verdict, '•')} Recheck · "
        f"{safe(RECHECK_LABEL.get(verdict.verdict, verdict.verdict))}",
        "",
        f"Rechecked at `{cell(head_sha[:12])}`"
        + (" · judged by model" if verdict.judged else " · determined from the diff"),
        "",
    ]
    if verdict.change_summary:
        lines += [f"**What changed** — {safe(verdict.change_summary)}", ""]
    if verdict.reasoning:
        lines += [f"**Assessment** — {safe(verdict.reasoning)}", ""]
    if verdict.evidence:
        lines += ["<details><summary>📎 Evidence at this head</summary>", ""]
        lines += [
            f"- `{cell(e.file)}:{e.line_start}-{e.line_end}` — {oneline(e.note)}"
            for e in verdict.evidence
        ]
        lines += ["", "</details>", ""]
    lines.append(
        "_Resolving this thread._"
        if verdict.verdict in RECHECK_RESOLVING
        else "_Leaving this thread open._"
    )
    lines += [
        "",
        "<sub>AI review · reply `/ai recheck` after another push · "
        "`/ai dismiss <reason>` if this is wrong</sub>",
        recheck_marker(verdict.fingerprint, head_sha),
    ]
    return "\n".join(lines)
