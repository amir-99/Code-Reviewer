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


def follow(number, file):
    """Map an old-file line through a diff to its new-file line (approximate).

    `file` is the ChangedFile of the diff between the two heads, or None when
    the file was not changed. A removed line maps to where it used to be.
    """
    if file is None:
        return number
    for line in file.lines:
        if line.kind == "context" and line.old_line == number:
            return line.new_line
    offset = 0
    for hunk in file.hunks:
        if hunk.old_start is None or hunk.new_start is None:
            continue
        # First line after the hunk on each side. A zero-length side names the
        # line before the change, so the next line follows it directly.
        old_after = hunk.old_start + (hunk.old_lines or 1)
        new_after = hunk.new_start + (hunk.new_lines or 1)
        if number >= old_after:
            offset = new_after - old_after
    return max(1, number + offset)


def adopt_identities(findings, previous, root, delta=None, tolerance=3):
    """Give a re-reported claim the identity it was already published under.

    A fingerprint hashes the claim's wording, so the same defect worded
    differently on a later push would otherwise be posted as a new comment
    while its open thread looked "absent", and so fixed. A fresh finding that
    names the same file and category at the previously reported location
    (followed through the commits since) inherits that fingerprint. Keeping an
    old thread open is the conservative error; it never resolves one.
    """
    changed = {}
    for file in delta or []:
        changed[file.old_path or file.path] = file
    known = {o.fingerprint for o in previous}
    taken = {f.fingerprint for f in findings}
    for old in previous:
        if old.fingerprint in taken:
            continue
        _, moved = reanchor(old, root)
        if moved:
            path, start, end = (
                moved.anchor.file,
                moved.anchor.line_start,
                moved.anchor.line_end,
            )
        else:
            file = changed.get(old.anchor.file)
            path = file.path if file else old.anchor.file
            start = follow(old.anchor.line_start, file)
            end = max(start, follow(old.anchor.line_end, file))
        for new in findings:
            if (
                new.fingerprint not in known
                and new.anchor.file == path
                and new.category == old.category
                and new.anchor.line_start <= end + tolerance
                and start <= new.anchor.line_end + tolerance
            ):
                new.fingerprint = old.fingerprint
                taken.add(old.fingerprint)
                break
    return findings


def full_review(previous, config_hash, prompt_hash, merge_base_distance):
    return (
        not previous
        or previous.get("partial", True)
        or previous.get("config_hash") != config_hash
        or previous.get("prompt_hash") != prompt_hash
        or merge_base_distance > previous.get("full_rereview_merge_base_delta", 50)
    )
