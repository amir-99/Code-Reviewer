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

    async def publish(self, bundle, findings, decision, config, stages):
        if config.enforcement == "silent":
            return
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
        for f in inline:
            if f.fingerprint in open_findings:
                continue
            if not slots or per_file[f.anchor.file] >= config.review.max_per_file:
                overflow[f.category] = overflow.get(f.category, 0) + 1
                continue
            if (
                await self.forge.get_merge_request(p, i)
            ).head_sha != bundle.code.head_sha:
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
            try:
                discussion = await self.forge.post_inline_discussion(
                    p, i, render(f), position
                )
                f.status = "published"
                slots -= 1
                per_file[f.anchor.file] += 1
                if self.store:
                    await self.store.record_comment(f.id, discussion)
            except PositionError:
                summarized.append(f)
        marker = f"<!-- ai-review:summary={bundle.code.head_sha} -->"
        bot = await self.forge.identity()
        if not any(
            marker in n.body and n.author_id == bot
            for d in discussions
            for n in d.notes
        ):
            if (
                await self.forge.get_merge_request(p, i)
            ).head_sha != bundle.code.head_sha:
                raise StaleReview()
            await self.forge.post_note(
                p, i, summary(bundle, decision, findings, summarized, overflow, stages)
            )


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
