"""Choose, per unit, the related code and specification each reviewer sees.

Units are reviewed independently, so an invariant split across two units, or
stated only in the repository's own contracts, is invisible to both reviewers.
One cheap call per review picks from menus code builds: specification sections
split by heading, and changed definitions from other units. The model names
menu IDs only; code reads every excerpt through the scanned lookup, discards
anything off the menu, and bounds what each unit receives.
"""

import hashlib
import json
import re
from fnmatch import fnmatch

from pydantic import Field

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import Schema
from reviewer.services.llm import tokens
from reviewer.telemetry.activity import activity, record

ROLE = "context_planner"
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
# Paths that usually hold a repository's contracts rank first in the menu.
SPEC_WORDS = ("contract", "spec", "adr", "design", "architecture", "prd", "docs/")
MAX_SECTIONS = 400
MAX_DEFINITIONS = 400
PER_UNIT = 4


class UnitPlan(Schema):
    unit_id: str
    sections: list[str] = Field(max_length=PER_UNIT)
    definitions: list[str] = Field(max_length=PER_UNIT)
    reason: str = Field(max_length=200)


class ContextPlan(Schema):
    units: list[UnitPlan]


def matches(path, patterns):
    return any(fnmatch(path, p) or fnmatch("/" + path, p) for p in patterns)


def sections(path, text):
    """Markdown split at level 1-3 headings outside fenced code."""
    found, fenced = [], False
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        heading = None if fenced else HEADING.match(line)
        if heading:
            if found:
                found[-1]["end"] = number - 1
            found.append(
                {
                    "id": f"{path}#{len(found) + 1}",
                    "path": path,
                    "title": heading.group(2)[:120],
                    "start": number,
                    "end": len(lines),
                }
            )
    return [s for s in found if s["end"] > s["start"]]


async def spec_menu(lookup, config):
    review = config.review
    if not review.spec_paths or not review.spec_files:
        return []
    paths = [
        p
        for p in await lookup.git.ls_files(lookup.wt)
        if p.endswith(".md")
        and matches(p, review.spec_paths)
        and not matches(p, review.exclude)
    ]
    paths = sorted(
        paths, key=lambda p: (not any(w in p.lower() for w in SPEC_WORDS), p)
    )[: review.spec_files]
    texts = await lookup.code_cache.read_many(paths)
    menu = []
    for path in paths:
        if path in texts:
            menu.extend(sections(path, texts[path]))
    return menu[:MAX_SECTIONS]


def changed_lines(bundle):
    return {
        f.path: {line.new_line for line in f.lines if line.kind == "added"}
        for f in bundle.code.files
        if not f.is_excluded
    }


def definition_menu(symbols, bundle):
    """Changed definitions: those overlapping an added line of a reviewed file."""
    added = changed_lines(bundle)
    menu = []
    for path, lines in added.items():
        for s in symbols.symbols.get(path, []):
            if any(s["start"] <= n <= s["end"] for n in lines):
                menu.append(
                    {
                        "id": f"{path}::{s['name']}@{s['start']}",
                        "path": path,
                        "name": s["name"],
                        "start": s["start"],
                        "end": s["end"],
                    }
                )
    return menu[:MAX_DEFINITIONS]


def unit_summary(unit, definitions):
    return {
        "id": unit.id,
        "paths": unit.paths,
        "changes": sorted({d["name"] for d in definitions if d["path"] in unit.paths})[
            :30
        ],
    }


class ContextPlanner:
    name = ROLE
    tier = ROLE

    @property
    def prompt_version(self):
        return PROMPTS[ROLE][0]

    def prompt(self, bundle, units, specs, definitions, input_limit):
        system = PROMPTS[ROLE][1] + "\n" + INJECTION_RULE

        def render(specs):
            payload = {
                "mr_title": bundle.mr.title,
                "units": [unit_summary(u, definitions) for u in units],
                "sections": [
                    {"id": s["id"], "path": s["path"], "heading": s["title"]}
                    for s in specs
                ],
                "definitions": [
                    {"id": d["id"], "path": d["path"], "name": d["name"]}
                    for d in definitions
                ],
            }
            return frame(json.dumps(payload, ensure_ascii=False), "planning-menu")

        user = render(specs)
        # A large repository's menu is cut from the end, where the paths that
        # least resemble specifications were ranked.
        while input_limit and specs and tokens.messages(system, user) > input_limit:
            specs = specs[: len(specs) * 3 // 4]
            user = render(specs)
        return system, user

    @activity("phase", "Plan unit context")
    async def plan(self, bundle, units, llm, config, context_provider, store=None):
        """Attach planned excerpts to `units` in place; never fails the review."""
        lookup = getattr(context_provider, "lookup", None)
        spec = getattr(llm, "specs", {}).get(ROLE)
        # Runs only where its role resolved to a model: an installation without
        # one reviews exactly as before.
        if (
            not config.review.context_planner
            or not units
            or llm is None
            or lookup is None
            or spec is None
        ):
            return
        try:
            specs = await spec_menu(lookup, config)
            definitions = definition_menu(lookup.symbols, bundle)
            if not specs and not definitions:
                return
            limit = (spec.context_tokens or 0) * 3 // 4 or None
            system, user = self.prompt(bundle, units, specs, definitions, limit)
            key = hashlib.sha256(
                json.dumps(
                    [
                        system,
                        user,
                        bundle.code.head_sha,
                        config.review.planned_context_bytes,
                    ]
                ).encode()
            ).hexdigest()
            cached = (await store.unit_results(bundle.review_id, ROLE)) if store else {}
            if key in cached:
                planned = cached[key]
            else:
                result = await llm.complete(
                    stage=ROLE,
                    tier=ROLE,
                    system=system,
                    user=user,
                    response_model=ContextPlan,
                    review_id=bundle.review_id,
                    timeout_s=120,
                    prompt_version=self.prompt_version,
                )
                planned = await self.fetch(
                    result, units, specs, definitions, lookup, config
                )
                if store:
                    # Persisted so a resumed run rebuilds identical unit prompts
                    # and recovers its finished units instead of redoing them.
                    await store.save_unit(bundle.review_id, ROLE, key, planned)
        except Exception:
            bundle.degradations.append("context_plan_unavailable")
            return
        for unit in units:
            unit.planned = planned.get(unit.id, {})
        await record(
            "context_plan",
            dict(
                name=ROLE,
                units=sum(bool(v) for v in planned.values()),
                sections=sum(
                    k.startswith("spec:") for v in planned.values() for k in v
                ),
                definitions=sum(
                    k.startswith("code:") for v in planned.values() for k in v
                ),
            ),
        )

    async def fetch(self, result, units, specs, definitions, lookup, config):
        by_section = {s["id"]: s for s in specs}
        by_definition = {d["id"]: d for d in definitions}
        known = {u.id: u for u in units}
        budget = config.review.planned_context_bytes
        planned = {}
        for choice in result.units:
            unit = known.get(choice.unit_id)
            if unit is None or unit.id in planned:
                continue
            wanted = [
                ("spec:" + s["path"] + "#" + s["title"], s)
                for s in (by_section.get(i) for i in choice.sections)
                if s
            ] + [
                ("code:" + d["path"] + "::" + d["name"], d)
                for d in (by_definition.get(i) for i in choice.definitions)
                # Its own code is already in the unit's diff.
                if d and d["path"] not in unit.paths
            ]
            excerpts, size = {}, 0
            for label, item in wanted:
                found = await lookup.file(item["path"], item["start"], item["end"])
                value = found.get(item["path"])
                if not value or not value.get("code"):
                    continue
                remaining = budget - size
                if remaining < 400:
                    break
                if len(value["code"].encode()) > remaining:
                    # Trimmed on whole lines, and marked as a partial range.
                    value = dict(value)
                    lines, kept, used = value["code"].splitlines(), [], 0
                    for line in lines:
                        used += len(line.encode()) + 1
                        if used > remaining:
                            break
                        kept.append(line)
                    value.update(
                        code="\n".join(kept),
                        line_end=value["line_start"] + len(kept) - 1,
                        range_complete=False,
                        has_more_after=True,
                    )
                excerpts[label] = value
                size += len(value["code"].encode())
            if excerpts:
                planned[unit.id] = excerpts
        return planned
