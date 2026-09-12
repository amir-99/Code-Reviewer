"""Deterministic unit identity and breadth-first scheduling of every chunk."""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, is_dataclass

from reviewer.context.partition import partition


def plan(bundle, config, kind, stage, only_paths):
    units = partition(bundle, kind, config.review.unit_tokens)
    if only_paths is not None:
        units = [u for u in units if set(u.paths) & set(only_paths)]
    if kind == "whole_change":
        return units, {u.id for u in units}
    # Visit one chunk per file before visiting a second chunk. Large files cannot
    # monopolize the remaining budget. Prioritize source over non-code changes.
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
    ordered = []
    for part in range(max((len(v) for v in groups.values()), default=0)):
        for path in paths:
            if part < len(groups[path]):
                ordered.append(groups[path][part])
    return ordered, {u.id for u in ordered}


def identity(agent, bundle, unit, llm, config):
    system, user = agent.build_prompt(bundle, unit)
    spec = getattr(llm, "specs", {}).get(agent.name)
    config_data = config.model_dump(mode="json")
    if config.analysis_mode == "deep":
        # Preserve checkpoint identities from before analysis_mode was explicit.
        config_data.pop("analysis_mode", None)
    data = dict(
        schema=2,
        stage=agent.name,
        system=system,
        user=user,
        head=bundle.code.head_sha,
        base=bundle.code.merge_base_sha,
        model=asdict(spec)
        if is_dataclass(spec)
        else getattr(llm, "models", {}).get(agent.name),
        config=config_data,
    )
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
