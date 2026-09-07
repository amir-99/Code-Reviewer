"""Sanitize storage XHTML before Markdown conversion; strip transclusions."""

import re

from bs4 import BeautifulSoup
from markdownify import markdownify


def markdown(html):
    soup = BeautifulSoup(html, "html.parser")
    for node in list(
        soup.find_all(
            ["script", "style", "img", "iframe", "object", "ac:image", "ri:attachment"]
        )
    ):
        node.decompose()
    for node in list(soup.find_all("ac:structured-macro")):
        if node.attrs is None:
            continue
        kind = node.get("ac:name", "")
        if kind in {"code", "panel", "info", "note", "warning"}:
            node.name = "pre" if kind == "code" else "blockquote"
            for param in node.find_all("ac:parameter"):
                param.decompose()
        else:
            node.decompose()
    return markdownify(str(soup), heading_style="ATX").strip()


def jira_wiki(value):
    value = re.sub(r"!.*?!", "", value)
    value = re.sub(
        r"^h([1-6])\.\s*", lambda m: "#" * int(m[1]) + " ", value, flags=re.M
    )
    return re.sub(r"^\*\s+", "- ", value, flags=re.M)


def criteria(text, heading):
    from reviewer.services.issues.jira import AcceptanceCriterion

    active = False
    result = []
    for line in text.splitlines():
        if line.strip().lstrip("#").strip().strip("*").casefold() == heading.casefold():
            active = True
            continue
        if active and re.match(r"^#{1,6} ", line):
            break
        match = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+)", line)
        if active and match:
            result.append(
                AcceptanceCriterion(id=f"AC-{len(result) + 1}", text=match[1])
            )
    return result
