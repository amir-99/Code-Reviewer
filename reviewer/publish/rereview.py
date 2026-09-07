from hashlib import sha256

from reviewer.findings.validator import read_lines


def reanchor(finding, root):
    lines = read_lines(root, finding.anchor.file)
    if lines is None:
        return "fixed", None
    length = finding.anchor.line_end - finding.anchor.line_start + 1
    for offset in range(max(0, len(lines) - length + 1)):
        digest = sha256("\n".join(lines[offset : offset + length]).encode()).hexdigest()
        if digest == finding.anchor.context_hash:
            updated = finding.model_copy(deep=True)
            updated.anchor.line_start = offset + 1
            updated.anchor.line_end = offset + length
            return (
                "unchanged" if offset + 1 == finding.anchor.line_start else "moved"
            ), updated
    # A changed snippet alone is not proof the claim was fixed. Re-run the
    # affected units; only a complete fresh review can classify it as fixed.
    return "invalidated", None


def full_review(previous, config_hash, prompt_hash, merge_base_distance):
    return (
        not previous
        or previous.get("partial", True)
        or previous.get("config_hash") != config_hash
        or previous.get("prompt_hash") != prompt_hash
        or merge_base_distance > previous.get("full_rereview_merge_base_delta", 50)
    )


async def resolve_fixed(
    forge, project, iid, previous_findings, new_findings, discussions, complete, touched
):
    if not complete:
        return []
    fingerprints = {
        f.fingerprint
        for f in new_findings
        if f.status not in {"discarded", "suppressed", "resolved"}
    }
    resolved = []
    for old in previous_findings:
        if old.anchor.file not in touched or old.fingerprint in fingerprints:
            continue
        discussion = discussions.get(old.fingerprint)
        if discussion:
            await forge.reply(
                project,
                iid,
                discussion.id,
                "The affected code was re-reviewed and this finding is no longer present.",
            )
            await forge.resolve_discussion(project, iid, discussion.id)
            resolved.append(old)
    return resolved
