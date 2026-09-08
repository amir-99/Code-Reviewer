import re

import structlog

from reviewer.publish.publisher import existing

logger = structlog.get_logger()


async def command(ctx, project, iid, user, note, discussion_id=None, note_id=None):
    forge = ctx["forge"]
    store = ctx["store"]
    if await forge.role(project, user) < 30:
        logger.warning(
            "ai_command_forbidden",
            project_id=project,
            iid=iid,
            user_id=user,
        )
        return "forbidden"
    match = re.fullmatch(
        r"/ai (review|recheck|explain|dismiss)(?:\s+(.{1,1000}))?", note.strip(), re.S
    )
    if not match:
        return "ignored"
    action, reason = match.groups()
    logger.info(
        "ai_command_received",
        project_id=project,
        iid=iid,
        user_id=user,
        action=action,
        note_id=note_id,
    )
    if action == "recheck":
        # Answers the existing threads only; it never supersedes a running review.
        await ctx["redis"].enqueue_job(
            "recheck_review", project, iid, _job_id=f"recheck:{note_id}"
        )
        return "queued"
    if action == "review":
        from reviewer.worker import replay_review

        await replay_review(ctx, project, iid, f"command:{note_id}")
        return "queued"
    if action == "dismiss" and not reason:
        return "reason_required"
    fingerprints, discussions = await existing(forge, project, iid)
    target = next(
        (
            d
            for d in discussions
            if d.id == discussion_id or any(n.id == note_id for n in d.notes)
        ),
        None,
    )
    if not target:
        return "unknown_discussion"
    fp = next((key for key, d in fingerprints.items() if d.id == target.id), None)
    if not fp:
        return "not_our_finding"
    finding = await store.finding_by_fingerprint(project, iid, fp)
    if not finding:
        return "unknown_finding"
    if action == "explain":
        from html import escape

        body = (
            escape(finding.reason)
            + "\n\nEvidence: "
            + ", ".join(
                f"{escape(e.file)}:{e.line_start}-{e.line_end}"
                for e in finding.evidence
            )
        )
        body += (
            "\n\nPrompt "
            + finding.provenance.prompt_version
            + " · model "
            + finding.provenance.model
        )
        await forge.reply(project, iid, target.id, body)
    else:
        await forge.resolve_discussion(project, iid, target.id)
        from sqlalchemy import select

        from reviewer.store.models import Project

        async with store.sessions() as session:
            internal = await session.scalar(
                select(Project.id).where(Project.gitlab_project_id == project)
            )
        await store.outcome(internal, iid, fp, "dismissed", reason, user)
    return "done"
