import re

from reviewer.findings.noise import select
from reviewer.publish.renderer import render, summary
from reviewer.services.forge.gitlab import Position, PositionError, StaleReview


async def existing(forge, project, iid):
    bot = await forge.identity()
    discussions = await forge.list_discussions(project, iid)
    fingerprints = {}
    for d in discussions:
        for n in d.notes:
            if n.author_id == bot and not d.resolved:
                for fp in re.findall(
                    r"<!-- ai-review:fingerprint=([a-f0-9]{32}) -->", n.body
                ):
                    fingerprints[fp] = d
    return fingerprints, discussions


class Publisher:
    def __init__(self, forge, store=None):
        self.forge, self.store = forge, store

    async def publish(
        self, bundle, findings, decision, config, stages, report_mode=None
    ):
        """Render the report and, unless drafting, post it to the merge request.

        `report_mode` is the operator's per-run choice: "applied" posts, "draft"
        renders and returns without writing to GitLab, "none" produces nothing.
        Silent enforcement is an operator setting rather than a per-run one, so a
        manual override can downgrade writes to a draft but never introduce them.
        """
        writes_allowed = config.enforcement != "silent"
        mode = report_mode or ("applied" if writes_allowed else "none")
        if mode == "applied" and not writes_allowed:
            mode = "none"
        if mode == "none":
            return None
        draft = mode == "draft"
        p, i = bundle.mr.project_id, bundle.mr.iid
        refs = await self.forge.get_diff_refs(p, i)
        if refs.head_sha != bundle.code.head_sha:
            raise StaleReview()
        open_findings, discussions = await existing(self.forge, p, i)
        inline, summarized, overflow = select(
            findings, config.review.max_inline, config.review.max_per_file
        )
        slots = max(0, config.review.max_inline - len(open_findings))
        from collections import Counter

        per_file = Counter(d.file for d in open_findings.values() if d.file)
        posted = []
        for f in inline:
            if f.fingerprint in open_findings:
                continue
            if not slots or per_file[f.anchor.file] >= config.review.max_per_file:
                overflow[f.category] = overflow.get(f.category, 0) + 1
                continue
            if (
                not draft
                and (await self.forge.get_merge_request(p, i)).head_sha
                != bundle.code.head_sha
            ):
                raise StaleReview()
            file = next(x for x in bundle.code.files if x.path == f.anchor.file)
            line = next(
                (line for line in file.lines if line.new_line == f.anchor.line_start),
                None,
            )
            if not line:
                summarized.append(f)
                continue
            position = position_for_line(file, line, refs)
            body = render(f)
            if draft:
                posted.append(comment(f, position, body))
                slots -= 1
                per_file[f.anchor.file] += 1
                continue
            try:
                discussion = await self.forge.post_inline_discussion(
                    p, i, body, position
                )
                f.status = "published"
                slots -= 1
                per_file[f.anchor.file] += 1
                posted.append(comment(f, position, body))
                if self.store:
                    await self.store.record_comment(f.id, discussion)
            except PositionError:
                summarized.append(f)
        body = summary(bundle, decision, findings, summarized, overflow, stages)
        marker = f"<!-- ai-review:summary={bundle.code.head_sha} -->"
        bot = await self.forge.identity()
        if not draft and not any(
            marker in n.body and n.author_id == bot
            for d in discussions
            for n in d.notes
        ):
            if (
                await self.forge.get_merge_request(p, i)
            ).head_sha != bundle.code.head_sha:
                raise StaleReview()
            await self.forge.post_note(p, i, body)
        return {"mode": mode, "summary": body, "inline": posted}


def comment(finding, position, body):
    return {
        "fingerprint": finding.fingerprint,
        "severity": str(finding.severity_final or ""),
        "file": position.new_path,
        "line": position.new_line or position.old_line,
        "body": body,
    }


def position_for_line(file, line, refs):
    return Position(
        base_sha=refs.base_sha,
        start_sha=refs.start_sha,
        head_sha=refs.head_sha,
        old_path=file.old_path or file.path,
        new_path=file.path,
        new_line=line.new_line if line.kind != "removed" else None,
        old_line=line.old_line if line.kind != "added" else None,
    )
