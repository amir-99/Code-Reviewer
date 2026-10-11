/* A recheck as it runs: how many open threads, how many judged, and what each
   one came to, read from its `recheck` activity rows. */

export class RecheckProgress {
  constructor() { this.clear(); }

  clear() { this.pass = null; this.threads = new Map(); }

  accept(item) {
    if (item?.kind !== 'recheck') return false;
    const data = item.data || {};
    if (data.index === undefined) {
      // The pass itself: a new one starts the count again.
      if (data.status === 'started') {
        this.pass = {id: data.activity_id, total: Number(data.total) || 0, active: true};
        this.threads.clear();
      } else if (this.pass?.id === data.activity_id) this.pass = {...this.pass, active: false};
      return true;
    }
    if (!this.pass || data.parent_id !== this.pass.id) return false;
    this.threads.set(data.activity_id, {name: data.name, status: data.status, verdict: data.verdict ?? null});
    return true;
  }

  get view() {
    if (!this.pass) return null;
    const threads = [...this.threads.values()];
    const done = threads.filter(thread => thread.status === 'completed');
    const tally = {};
    for (const thread of done) tally[thread.verdict] = (tally[thread.verdict] || 0) + 1;
    const judging = threads.find(thread => thread.status === 'started');
    return {active: this.pass.active, total: this.pass.total, done: done.length, tally, current: judging?.name ?? null};
  }
}

export function describe(view, verdictLabel = verdict => verdict) {
  if (!view) return '';
  const counts = Object.entries(view.tally).map(([verdict, n]) => `${n} ${verdictLabel(verdict).toLowerCase()}`);
  const head = view.active
    ? `Judging ${Math.min(view.done + 1, view.total)} of ${view.total} thread${view.total === 1 ? '' : 's'}`
    : `Judged ${view.done} of ${view.total} thread${view.total === 1 ? '' : 's'}`;
  return [head + (view.active && view.current ? ` — ${view.current}` : ''), ...counts].join(' · ');
}
