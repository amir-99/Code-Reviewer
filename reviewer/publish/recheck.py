"""Rechecking published comments against a newer head.

A push answers no comment on its own: the threads the reviewer already opened
stay open, and nothing tells the author which of them their fixes settled. This
module takes every unresolved discussion the reviewer owns, decides whether the
new head addresses its claim, and replies in that thread with the verdict and
what changed. Only `fixed` and `obsolete` resolve a thread; every other verdict
leaves it open for a person.

Cheap cases never reach a model: a deleted file, an untouched file whose cited
lines are byte-identical, or a claim a complete fresh re-review of that file no
longer reports are all settled from the diff alone.
"""

from pathlib import Path

import structlog
from pydantic import BaseModel

from reviewer.agents.recheck import judge
from reviewer.findings.models import RECHECK_RESOLVING, Evidence
from reviewer.findings.validator import read_lines
from reviewer.publish.publisher import existing, pending
from reviewer.publish.renderer import recheck_marker, render_recheck
from reviewer.publish.rereview import reanchor

logger = structlog.get_logger()

CONTEXT_LINES = 12
PATCH_LIMIT = 8000
AUTHOR_NOTE_LIMIT = 5
AUTHOR_NOTE_BYTES = 2000


class ThreadVerdict(BaseModel):
    """One published comment, judged against the head being reviewed."""

    fingerprint: str
    finding_id: str
    discussion_id: str
    file: str
    line: int
    claim: str
    severity: str = ""
    verdict: str
    change_summary: str = ""
    reasoning: str = ""
    evidence: list[Evidence] = []
    judged: bool = False


async def collect(forge, store, project, iid, previous_findings, head_sha, draft=False):
    """Unresolved reviewer-owned threads paired with the finding that opened them.

    Threads already answered for this head are dropped, so a redelivered job or
    a second manual recheck neither re-posts nor re-spends tokens. A drafted
    answer counts as an answer: its reply is waiting as a draft note rather than
    in the thread, so the pending drafts are searched for the same marker.
    """
    fingerprints, _ = await existing(forge, project, iid)
    _, drafts = await pending(forge, project, iid) if draft else (None, [])
    bot = await forge.identity()
    known = {f.fingerprint: f for f in previous_findings}
    threads = []
    for fingerprint, discussion in fingerprints.items():
        marker = recheck_marker(fingerprint, head_sha)
        if any(marker in note.body for note in discussion.notes) or any(
            marker in note.body for note in drafts
        ):
            continue
        finding = known.get(fingerprint)
        if finding is None and store is not None:
            finding = await store.finding_by_fingerprint(project, iid, fingerprint)
        if finding is None:
            continue
        notes = [
            note.body[:AUTHOR_NOTE_BYTES]
            for note in discussion.notes
            if note.author_id != bot
        ][:AUTHOR_NOTE_LIMIT]
        threads.append((finding, discussion, notes))
    return threads


def triage(finding, root, touched, absent, reported):
    """Settle what the diff alone settles.

    Returns `(verdict, anchored)`; a `None` verdict means the thread needs a
    judgement. `touched` is the set of paths the new commits changed, or None
    when any path may have changed. `absent` and `reported` are what the review
    running alongside concluded about each claim, which outranks any judgement.
    """
    if finding.fingerprint in reported:
        # This very run still reports the claim; no judgement can overrule that.
        return "not_fixed", None
    if finding.fingerprint in absent:
        return "fixed", None
    lines = read_lines(root, finding.anchor.file)
    if lines is None:
        readable = (Path(root) / finding.anchor.file).is_file()
        return ("unverifiable" if readable else "obsolete"), None
    state, moved = reanchor(finding, root)
    anchored = moved or finding
    if (
        state in {"unchanged", "moved"}
        and touched is not None
        and finding.anchor.file not in touched
    ):
        # Byte-identical cited lines in a file these commits never touched.
        return "not_fixed", anchored
    return None, anchored


def window(lines, start, end):
    """Numbered source around an anchor, so a verdict can cite real lines."""
    if not lines:
        return ""
    first = max(1, start - CONTEXT_LINES)
    last = min(len(lines), end + CONTEXT_LINES)
    return "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))


async def older(git, wt, sha, path, start, end):
    try:
        blob = await git.command(
            "show", f"{sha}:{path}", cwd=wt.mirror, limit=2_000_000
        )
    except Exception:
        return ""
    return window(blob.decode(errors="replace").splitlines(), start, end)


async def patch(git, wt, old_sha, new_sha, path):
    try:
        data = await git.command(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "-M",
            old_sha,
            new_sha,
            "--",
            path,
            cwd=wt.mirror,
            limit=2_000_000,
        )
    except Exception:
        return ""
    return data.decode(errors="replace")[:PATCH_LIMIT]


DETERMINED = {
    "fixed": "A complete re-review of {file} at this head no longer reports this claim.",
    "not_fixed": "The cited lines are unchanged and no commit since the comment "
    "touched {file}.",
    "obsolete": "{file} no longer exists at this head.",
    "unverifiable": "{file} could not be read at this head, so the claim could "
    "not be rechecked.",
}


async def evaluate(
    threads,
    *,
    root,
    touched,
    absent=frozenset(),
    reported=frozenset(),
    git=None,
    wt=None,
    previous_head=None,
    llm=None,
    redactor=None,
    review_id=None,
    limit=15,
):
    """A verdict for every thread: from the diff where possible, judged where not.

    A thread the diff already settles as fixed is still put to the judge, for
    the one thing the diff cannot say — how it was fixed. That judgement may
    only make the answer more conservative: it can withdraw a resolution the
    diff implied, never grant one the diff did not.
    """
    verdicts = []
    judged = 0
    for finding, discussion, notes in threads:
        verdict, anchored = triage(finding, root, touched, absent, reported)
        anchored = anchored or finding
        result = None
        if verdict is None or verdict == "fixed":
            if llm is None or judged >= limit:
                # No judgement available is not evidence of a fix, but it does
                # not withdraw one the diff established either.
                verdict = verdict or "unverifiable"
            else:
                judged += 1
                # Three views of the same code: when the comment was posted,
                # now, and what the commits in between did to the file.
                before = (
                    await older(
                        git,
                        wt,
                        previous_head,
                        finding.anchor.file,
                        finding.anchor.line_start,
                        finding.anchor.line_end,
                    )
                    if previous_head
                    else ""
                )
                after = window(
                    read_lines(root, anchored.anchor.file),
                    anchored.anchor.line_start,
                    anchored.anchor.line_end,
                )
                since = (
                    await patch(
                        git, wt, previous_head, wt.head_sha, finding.anchor.file
                    )
                    if previous_head
                    else ""
                )
                try:
                    result = await judge(
                        finding, before, after, since, notes, llm, redactor, review_id
                    )
                    if verdict is None or result.verdict not in RECHECK_RESOLVING:
                        verdict = result.verdict
                except Exception as exc:
                    logger.warning(
                        "recheck_judgement_failed",
                        fingerprint=finding.fingerprint,
                        error_type=type(exc).__name__,
                    )
                    result = None
                    verdict = verdict or "unverifiable"
        verdicts.append(
            ThreadVerdict(
                fingerprint=finding.fingerprint,
                finding_id=finding.id,
                discussion_id=discussion.id,
                file=anchored.anchor.file,
                line=anchored.anchor.line_start,
                claim=finding.claim,
                severity=str(finding.severity_final or ""),
                verdict=verdict,
                change_summary=result.change_summary if result else "",
                reasoning=result.reasoning
                if result
                else DETERMINED.get(verdict, "").format(file=finding.anchor.file),
                evidence=result.evidence if result else [],
                judged=result is not None,
            )
        )
    return verdicts


async def publish(
    forge, store, project, iid, internal_project, verdicts, head_sha, draft=False
):
    """Reply in each thread, and resolve the ones the new head answered.

    A drafted recheck queues the same replies as GitLab draft notes, each
    carrying the resolution it would apply, so publishing them later answers and
    resolves the threads exactly as an applied run would have.
    """
    posted = []
    for verdict in verdicts:
        body = render_recheck(verdict, head_sha)
        resolving = verdict.verdict in RECHECK_RESOLVING
        if draft:
            await forge.post_draft_note(
                project,
                iid,
                body,
                in_reply_to_discussion_id=verdict.discussion_id,
                resolve_discussion=resolving,
            )
        else:
            await forge.reply(project, iid, verdict.discussion_id, body)
            if resolving:
                await forge.resolve_discussion(project, iid, verdict.discussion_id)
                if store is not None:
                    await store.resolve_comment(verdict.finding_id)
                    await store.outcome(
                        internal_project,
                        iid,
                        verdict.fingerprint,
                        "actioned",
                        f"Recheck at {head_sha}: {verdict.verdict}",
                        "system",
                    )
        posted.append(
            {
                "fingerprint": verdict.fingerprint,
                "discussion_id": verdict.discussion_id,
                "verdict": verdict.verdict,
                "resolved": resolving and not draft,
                "body": body,
            }
        )
    logger.info(
        "recheck_published",
        project_id=project,
        iid=iid,
        head_sha=head_sha,
        threads=len(verdicts),
        resolved=sum(1 for p in posted if p["resolved"]),
        draft=draft,
    )
    return posted
