"""Render document findings as Confluence storage-format comments.

Storage format is XHTML. Confluence keeps HTML comments in a comment body, so
the same fingerprint, summary and owner markers the GitLab publisher relies on
survive a round trip and reconciliation works the same way. Bodies are built
from escaped text; no reviewed content is ever placed in the markup unescaped.
"""

import re
from html import escape

from reviewer.publish.renderer import SEVERITY_ORDER, icon

FOOTER = "AI review · machine-assisted · document review"
FINGERPRINT = re.compile(r"<!-- ai-review:fingerprint=([a-f0-9]{32}) -->")
SUMMARY = re.compile(r"<!-- ai-review:summary=(\d+)@(\d+) -->")


def text(value):
    return escape(str(value or ""), quote=False).replace("@", "＠")


def paragraph(value):
    return f"<p>{text(value)}</p>"


def fingerprint_marker(fingerprint):
    return f"<!-- ai-review:fingerprint={fingerprint} -->"


def summary_marker(page_id, version):
    return f"<!-- ai-review:summary={int(page_id)}@{int(version)} -->"


def footer():
    return f"<p><sub>{FOOTER}</sub></p>"


def finding_body(f):
    """One comment per finding: the claim, the passage, why, impact, direction."""
    parts = [
        f"<p><strong>{text(icon(f.severity_final))} {text(f.severity_final)} · "
        f"{text(f.category)}</strong></p>",
        f"<p><strong>{text(f.claim)}</strong></p>",
        f"<p><em>Section:</em> {text(f.anchor.heading_path)}</p>",
        f"<blockquote><p>{text(f.anchor.quote)}</p></blockquote>",
        f"<p><strong>Why</strong> — {text(f.reason)}</p>",
        f"<p><strong>Impact</strong> — {text(f.impact)}"
        f" <em>(advisory level: {text(f.impact_level or 'unknown')})</em></p>",
        f"<p><strong>Suggested direction</strong> — {text(f.suggested_direction)}</p>",
    ]
    if f.related:
        items = "".join(
            f"<li>{text(r.heading_path)}"
            + (
                f" <em>(page {text(r.page_id)})</em>"
                if r.page_id != f.anchor.page_id
                else ""
            )
            + f": <q>{text(r.quote)}</q>"
            + (f" — {text(r.note)}" if r.note else "")
            + "</li>"
            for r in f.related
        )
        parts.append(f"<p><strong>Compared with</strong></p><ul>{items}</ul>")
    if f.verification and f.verification.verdict != "confirmed":
        parts.append(
            f"<p><em>Verifier:</em> {text(f.verification.verdict)} — "
            f"{text(f.verification.reasoning)}</p>"
        )
    parts.append(footer())
    parts.append(fingerprint_marker(f.fingerprint))
    return "\n".join(parts)


def summary_body(subject, findings, inline, overflow, degradations, partial, decision):
    """The page-level report comment: counts, then every summary-only finding."""
    counts = {}
    for f in findings:
        counts[str(f.severity_final)] = counts.get(str(f.severity_final), 0) + 1
    parts = [
        f"<p><strong>AI document review</strong> of "
        f"<em>{text(subject.get('title'))}</em> (version {text(subject.get('version'))})"
        + (" — <strong>partial</strong>" if partial else "")
        + "</p>",
        paragraph(f"Outcome: {decision}. {len(findings)} finding(s)."),
    ]
    if counts:
        items = "".join(
            f"<li>{text(icon(s))} {text(s)}: {counts[s]}</li>"
            for s in SEVERITY_ORDER
            if s in counts
        )
        parts.append(f"<ul>{items}</ul>")
    posted = {f.fingerprint for f in inline}
    rest = [f for f in findings if f.fingerprint not in posted]
    if rest:
        parts.append("<p><strong>Findings not posted as separate comments</strong></p>")
        items = []
        for f in rest:
            items.append(
                f"<li>{text(icon(f.severity_final))} <strong>{text(f.severity_final)}</strong> "
                f"· {text(f.category)} · <em>{text(f.anchor.heading_path)}</em>: "
                f"{text(f.claim)} — {text(f.suggested_direction)}</li>"
            )
        parts.append("<ul>" + "".join(items) + "</ul>")
    if overflow:
        parts.append(paragraph(f"{overflow} further finding(s) omitted from comments."))
    if degradations:
        parts.append(paragraph("Limitations: " + ", ".join(sorted(degradations))))
    parts.append(footer())
    parts.append(summary_marker(subject["page_id"], subject["version"]))
    return "\n".join(parts)


def summary_markdown(subject, findings, degradations, partial, decision):
    """The same report for the dashboard, as Markdown."""
    lines = [
        f"## AI document review — {subject.get('title')} (v{subject.get('version')})"
        + (" — partial" if partial else ""),
        "",
        f"Outcome: **{decision}** · {len(findings)} finding(s)",
        "",
    ]
    for severity in SEVERITY_ORDER:
        group = [f for f in findings if str(f.severity_final) == severity]
        if not group:
            continue
        lines.append(f"### {icon(severity)} {severity} ({len(group)})")
        lines.append("")
        for f in group:
            lines.append(
                f"- **{f.claim}** — _{f.anchor.heading_path}_ · {f.category}\n"
                f"  > {f.anchor.quote}\n\n  {f.suggested_direction}"
            )
        lines.append("")
    if degradations:
        lines += ["Limitations: " + ", ".join(sorted(degradations)), ""]
    return "\n".join(lines).strip()
