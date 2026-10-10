"""Process each file's findings as soon as no pending unit can add to them.

Verification used to start only after every Defect Review unit had finished,
so the slowest unit decided when the first verifier could run. Deduplication
only ever merges findings in the same file, so a file whose units are all done
can be validated, deduplicated and verified while other units still run.

The stage result stays authoritative: `finish` receives its findings and
reuses a file's early work only when that file's inputs match exactly;
anything else (a recovered stage, a finding that arrived after its file was
started) is processed again from the stage result.
"""

import json
from collections import Counter, defaultdict


def key(proposal):
    return json.dumps(proposal.model_dump(mode="json"), sort_keys=True)


class FileStream:
    def __init__(self, group, prepare, clean=lambda proposal: proposal):
        """`prepare(proposals)` returns (findings, discarded) for one file;
        `clean` turns a unit's raw proposal into its persisted (redacted) form."""
        self.group, self.prepare, self.clean = group, prepare, clean
        self.pending = Counter()
        self.planned_paths = set()
        self.inputs = defaultdict(list)
        self.started = {}

    def planned(self, units):
        for unit in units:
            for path in unit.paths:
                self.pending[path] += 1
                self.planned_paths.add(path)

    def finished(self, unit, result):
        for proposal in result.findings:
            proposal = self.clean(proposal)
            self.inputs[proposal.anchor.file].append(proposal)
        for path in unit.paths:
            self.pending[path] -= 1
        # A finding anchored outside every unit's files waits for `finish`: no
        # unit count says when nothing more can arrive for it.
        for path, proposals in self.inputs.items():
            if (
                path in self.planned_paths
                and self.pending[path] <= 0
                and path not in self.started
            ):
                self.started[path] = (
                    Counter(map(key, proposals)),
                    self.group.create_task(self.prepare(list(proposals))),
                )

    async def finish(self, proposals):
        by_file = defaultdict(list)
        for proposal in proposals:
            by_file[proposal.anchor.file].append(proposal)
        findings, discarded = [], []
        for path, items in by_file.items():
            early = self.started.pop(path, None)
            if early and early[0] == Counter(map(key, items)):
                values, dropped = await early[1]
            else:
                if early:
                    await early[1]
                values, dropped = await self.prepare(items)
            findings.extend(values)
            discarded.extend(dropped)
        # Early work for a file the stage result no longer names is not used,
        # but it is still awaited so nothing outlives the worktree.
        for _, task in self.started.values():
            await task
        self.started.clear()
        return findings, discarded
