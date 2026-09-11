"""Deterministic unit identity and bounded, breadth-first triage selection."""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime

from reviewer.context.partition import partition
from reviewer.orchestrator.deadlines import cutoff


def plan(bundle, config, kind, stage, only_paths):
    triage = "triage_mode" in bundle.degradations and kind != "whole_change"
    limit = config.triage_unit_tokens if triage else config.review.unit_tokens
    units = partition(bundle, kind, limit)
    if only_paths is not None:
        units = [u for u in units if set(u.paths) & set(only_paths)]
    if not triage:
        return units, {u.id for u in units}
    # Visit one chunk per file before visiting a second chunk. Large files cannot
    # monopolize triage. Prioritize executable source over non-code changes.
    groups = defaultdict(list)
    changed = {
        f.path: sum(line.kind != "context" for line in f.lines)
        for f in bundle.code.files
    }
    for unit in units:
        groups[unit.paths[0]].append(unit)
    paths = sorted(
        groups,
        key=lambda p: (
            not p.endswith((".py", ".go", ".ts", ".tsx", ".js", ".jsx")),
            -changed.get(p, 0),
            p,
        ),
    )
    remaining = max(
        0, (cutoff(bundle, config, stage) - datetime.now(UTC)).total_seconds()
    )
    # A conservative unit estimate prevents dispatching a plan that cannot fit.
    capacity = min(
        config.triage_max_units,
        int(remaining * config.unit_concurrency / config.unit_timeout_s),
    )
    selected = []
    for part in range(max((len(v) for v in groups.values()), default=0)):
        for path in paths:
            if part < len(groups[path]) and len(selected) < capacity:
                selected.append(groups[path][part].id)
    return units, set(selected)


def identity(agent, bundle, unit, llm, config):
    system, user = agent.build_prompt(bundle, unit)
    spec = getattr(llm, "specs", {}).get(agent.name)
    data = dict(
        schema=1,
        stage=agent.name,
        system=system,
        user=user,
        head=bundle.code.head_sha,
        base=bundle.code.merge_base_sha,
        model=asdict(spec)
        if is_dataclass(spec)
        else getattr(llm, "models", {}).get(agent.name),
        config=config.model_dump(mode="json"),
    )
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
