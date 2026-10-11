/* Static analyzers and build commands: streamed from `check` activity while a
   review runs, then as the finished run recorded them. */

const TONES = {passed: 'tone-good', failed: 'tone-bad', errored: 'tone-warn', skipped: 'tone-warn'};
export const checkTone = status => TONES[status] || '';
// Lines kept per streamed check in the page; the server already bounds them.
const KEEP_LINES = 400;

export class CheckLog {
  constructor() { this.clear(); }

  clear() { this.live = new Map(); this.final = []; }

  // One `check` activity row: the first opens a check, later ones add lines
  // and, at the end, its status, exit code and duration.
  accept(item) {
    if (item?.kind !== 'check') return false;
    const data = item.data || {};
    if (!data.activity_id) return false;
    const check = this.live.get(data.activity_id)
      || {id: data.activity_id, name: '', status: 'running', lines: [], exit_code: null, duration_ms: null, truncated: 0};
    if (data.name) check.name = data.name;
    if (data.status) check.status = data.status;
    check.lines.push(...(data.lines || []));
    if (check.lines.length > KEEP_LINES) check.lines.splice(0, check.lines.length - KEEP_LINES);
    for (const key of ['exit_code', 'duration_ms', 'truncated']) if (data[key] !== undefined) check[key] = data[key];
    this.live.set(data.activity_id, check);
    return true;
  }

  load(checks) { this.final = Array.isArray(checks) ? checks : []; }

  // A finished run's record replaces what streamed: it holds every check's tail.
  get list() { return this.final.length ? this.final : [...this.live.values()]; }

  get counts() {
    const counts = {running: 0, passed: 0, failed: 0, other: 0};
    for (const check of this.list) {
      if (check.status in counts) counts[check.status]++; else counts.other++;
    }
    return counts;
  }
}
