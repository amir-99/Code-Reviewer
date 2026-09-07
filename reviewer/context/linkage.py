import re

from reviewer.context.models import Linkage

ISSUE_KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9}-[0-9]+)(?![0-9])")


def resolve(mr, project_keys, commits=()):
    found = []
    for source, text in [
        ("branch", mr.source_branch),
        ("title", mr.title),
        ("description", mr.description),
        *[("commit", x) for x in commits],
    ]:
        for key in ISSUE_KEY_RE.findall(text):
            if key.split("-")[0] in project_keys and key not in [k for k, _ in found]:
                found.append((key, source))
    return (
        Linkage(
            issue_key=found[0][0],
            resolved_from=found[0][1],
            secondary_keys=[k for k, _ in found[1:]],
        )
        if found
        else Linkage()
    )
