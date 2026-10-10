"""Requirements condensed once per review for every Defect Review prompt.

Every unit's prompt carries the requirements, so a large set of linked pages
was paid for on every call even when most of it said nothing about the change.
The issue, its acceptance criteria and the epic are kept whole: they are short
and they are what findings cite. Confluence pages are split at their headings
and the sections most related to the change are kept within a token share;
the headings of omitted sections are listed so the model knows they exist.
Selection is deterministic, lexical and never invents or rewrites text.
"""

import math
import re
from collections import Counter
from pathlib import PurePosixPath

from reviewer.services.llm.tokens import estimate

HEADING = re.compile(r"(?=^#{1,6} )", re.M)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")
# Words too common in specifications and code to say what a section is about.
COMMON = frozenset(
    """
    the and for with that this from have will must should shall when then than
    into onto each such also only more most other which while where there their
    they them been being does done make made used uses using user users value
    values data true false none null self return returns import from class def
    func function const type string number list dict page section note
    """.split()
)
# Every page keeps its opening, which usually states its scope.
OPENING_BYTES = 600


def terms(text):
    """Lower-case words of a text, splitting camelCase and snake_case."""
    found = []
    for word in WORD.findall(text):
        for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", word):
            part = part.lower()
            if len(part) >= 4 and part not in COMMON:
                found.append(part)
    return found


def change_terms(bundle):
    """What the change is about: its paths, added code and the issue's words."""
    words = Counter()
    for file in bundle.code.files:
        if file.is_excluded:
            continue
        path = PurePosixPath(file.path)
        words.update(terms(" ".join(path.parts[:-1] + (path.stem,))) * 3)
        words.update(
            terms(" ".join(line.text for line in file.lines if line.kind == "added"))
        )
    if bundle.issue:
        words.update(terms(bundle.issue.summary) * 2)
        for criterion in bundle.issue.acceptance_criteria:
            words.update(terms(criterion.text) * 2)
    words.update(terms(bundle.mr.title or "") * 2)
    return words


def sections(text):
    parts = [part for part in HEADING.split(text) if part.strip()]
    return parts or [text]


def title(section):
    first = section.lstrip().splitlines()[0] if section.strip() else ""
    return first.lstrip("#").strip() if first.startswith("#") else ""


def condense(bundle, document_tokens):
    """Requirements for the prompt, and whether any document text was omitted."""
    requirements = {
        "issue": bundle.issue.model_dump() if bundle.issue else None,
        "epic": bundle.epic.model_dump() if bundle.epic else None,
        "documents": [],
        "mr_title": bundle.mr.title,
        "mr_description": bundle.mr.description,
    }
    pages = [(page, sections(page.text_md)) for page in bundle.documents]
    if sum(estimate(page.text_md) for page in bundle.documents) <= document_tokens:
        requirements["documents"] = [page.model_dump() for page in bundle.documents]
        return requirements, False

    wanted = change_terms(bundle)
    every = [set(terms(s)) for _, parts in pages for s in parts]
    frequency = Counter(word for words in every for word in words)

    def score(section):
        found = set(terms(section))
        return sum(
            min(wanted[word], 5) / math.log(2 + frequency[word])
            for word in found
            if word in wanted
        )

    # Openings first, then the remaining sections by relevance, across pages.
    budget = document_tokens
    kept = {}
    for p, (page, parts) in enumerate(pages):
        opening = parts[0].encode()[:OPENING_BYTES].decode(errors="ignore")
        kept[(p, 0)] = opening
        budget -= estimate(opening)
    ranked = sorted(
        (
            (score(s), p, i)
            for p, (_, parts) in enumerate(pages)
            for i, s in enumerate(parts)
            if i
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    for relevance, p, i in ranked:
        section = pages[p][1][i]
        if relevance <= 0 or estimate(section) > budget:
            continue
        kept[(p, i)] = section
        budget -= estimate(section)
    omitted_any = False
    for p, (page, parts) in enumerate(pages):
        chosen = [kept[(p, i)] for i in range(len(parts)) if (p, i) in kept]
        omitted = [
            title(parts[i]) or f"section {i + 1}"
            for i in range(1, len(parts))
            if (p, i) not in kept
        ]
        if kept[(p, 0)] != parts[0]:
            omitted.insert(0, (title(parts[0]) or "opening") + " (remainder)")
        omitted_any |= bool(omitted)
        data = page.model_dump(exclude={"text_md"})
        data["text_md"] = "\n".join(chosen)
        if omitted:
            data["omitted_sections"] = omitted
        requirements["documents"].append(data)
    return requirements, omitted_any
