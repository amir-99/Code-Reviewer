/* The review pipeline as a flow, and the pure reading of where a review is in
   it. Nothing here touches the DOM: app.js paints what `walk` returns. */

export const FAILED = new Set(['FAILED_CONTEXT', 'FAILED_INTERNAL']);
export const HALTED = new Set(['TERMINATED_EARLY', 'CANCELLED', 'SUPERSEDED']);
export const TERMINAL = new Set([...FAILED, ...HALTED, 'PUBLISHED']);

export const label = value => String(value ?? '').replaceAll('_', ' ').trim().toLowerCase();

// The states the orchestrator walks, drawn as the flow it actually is.
// `stage` is the agent a step reports its work under.
export const FLOW = [
  {state: 'INIT', label: 'Admit', note: 'admission'},
  {state: 'CONTEXT_COLLECTION', label: 'Context', note: 'repository, requirements'},
  {state: 'STATIC_ANALYSIS', label: 'Static analysis', note: 'scanners'},
  {state: 'DEFECT_REVIEW', label: 'Defect review', stage: 'defect_review', note: 'concurrent units'},
  {state: 'DOCUMENT_REVIEW', label: 'Document review', stage: 'document_review', note: 'page sections'},
  {state: 'EVIDENCE_VALIDATION', label: 'Evidence', note: 'mechanical checks'},
  {state: 'FINDING_VERIFICATION', label: 'Verification', note: 'independent'},
  {state: 'FINALIZATION', label: 'Finalize', note: 'dedup, severity'},
  {state: 'DECISION', label: 'Decision', note: 'deterministic'},
  {state: 'PUBLISHED', label: 'Publish', note: 'report, status'},
];

// The removed seven-stage deep mode. Reviews it ran still carry its states and
// stage activity; they are read as the analysis step that replaced them.
const LEGACY_STATES = new Set(['PURPOSE_REVIEW', 'DESIGN_REVIEW', 'ANALYSIS_FAN_OUT', 'SYSTEM_CONTEXT_REVIEW']);
const LEGACY_STAGES = ['purpose', 'design', 'correctness', 'complexity', 'tests_', 'line_review', 'system_context'];
const legacy = state => LEGACY_STATES.has(state) ? 'DEFECT_REVIEW' : state;

export const PIPELINE = FLOW.map(step => step.state);

// A stage refines the step it ran under: partial, failed or unreported work is
// not a step the review may be read as having completed, whatever state the
// pipeline moved on to afterwards.
const TONE = {started: 'active', completed: null, partial: 'warn'};
const stageTone = status => status == null ? null : (status in TONE ? TONE[status] : 'failed');

// Each state's own time is the distance to the state that replaced it.
export function spans(history = [], at = new Map()) {
  const took = new Map();
  for (let index = 0; index < history.length - 1; index++) {
    const from = at.get(history[index]), to = at.get(history[index + 1]);
    if (Number.isFinite(from) && Number.isFinite(to)) took.set(history[index], to - from);
  }
  return took;
}

// The worst of several stage reports, so one partial or failed legacy stage
// keeps the analysis step off "done".
const RANK = {completed: 0, started: 1, partial: 2};
const worst = reports => reports.reduce((a, b) =>
  (RANK[b.status] ?? 3) > (RANK[a.status] ?? 3) ? b : a);

/* A deep-mode review read as the flow it would run today: its legacy states
   become the Defect review step (entered when the first of them was) and its
   stage reports become that step's report. */
function fold(state, history, stages, at) {
  if (!LEGACY_STATES.has(state) && !history.some(s => LEGACY_STATES.has(s))) {
    return {state, history, stages, at};
  }
  const folded = [];
  for (const entry of history.map(legacy)) if (folded.at(-1) !== entry) folded.push(entry);
  const times = new Map();
  for (const [entry, when] of at) {
    const key = legacy(entry);
    if (!times.has(key) || when < times.get(key)) times.set(key, when);
  }
  const reports = LEGACY_STAGES.map(name => stages.get(name)).filter(Boolean);
  const merged = new Map(stages);
  if (!merged.has('defect_review') && reports.length) {
    const starts = reports.map(r => r.at).filter(Number.isFinite);
    merged.set('defect_review', {
      status: worst(reports).status,
      at: starts.length ? Math.min(...starts) : null,
      took: null,
    });
  }
  return {state: legacy(state), history: folded, stages: merged, at: times};
}

/* Where the review stands, step by step.

   `stages` maps an agent name to {status, at, took} as its activity reported it,
   and `at` maps a state to when the pipeline entered it. A step reached but
   never walked was skipped outright — by a milestone short circuit — and is not
   a step still to come.

   `early` maps a state to when its work first started, which can be before the
   pipeline enters it: a file's findings are verified as soon as its units
   finish, while the Defect review step is still running. Such a step runs
   alongside the current one and its time counts from that first start. */
export function walk(state, history = [], stages = new Map(), at = new Map(), early = new Map()) {
  ({state, history, stages, at} = fold(state, history, stages, at));
  const walked = history.length ? history : [state];
  const seen = new Set(walked);
  // A document review reads pages, not a repository: no static analysis and a
  // single review stage in place of the code stages.
  const document = seen.has('DOCUMENT_REVIEW') || state === 'DOCUMENT_REVIEW' || stages.has('document_review');
  const codeStates = new Set(['STATIC_ANALYSIS', 'DEFECT_REVIEW']);
  const flow = FLOW.filter(s => document ? !codeStates.has(s.state) : s.state !== 'DOCUMENT_REVIEW');
  const pipeline = flow.map(s => s.state);
  const reached = Math.max(0, ...[...walked, state].map(s => pipeline.indexOf(s)));
  const terminal = TERMINAL.has(state);
  const took = spans(walked, at);

  const steps = flow.map((spec, index) => {
    let status = index < reached ? (seen.has(spec.state) ? 'done' : 'skipped')
      : index > reached ? (terminal ? 'skipped' : 'pending')
      : !terminal ? 'active'
      : state === 'PUBLISHED' ? 'done'
      : FAILED.has(state) ? 'failed' : 'stopped';
    const ahead = early.get(spec.state);
    const overlap = Number.isFinite(ahead) && !(at.get(spec.state) <= ahead);
    if (status === 'pending' && overlap && !terminal) status = 'active';
    const reported = spec.stage ? stages.get(spec.stage) : null;
    const tone = stageTone(reported?.status);
    if (status === 'done' && tone && tone !== 'active') status = tone;
    const note = status === 'skipped' ? 'not run'
      : reported && reported.status !== 'started' && reported.status !== 'completed' ? label(reported.status)
      : status === 'stopped' || (status === 'failed' && !reported) ? label(state)
      : overlap && index > reached ? 'started early, per file'
      : spec.note;
    const entered = at.get(spec.state);
    const own = took.get(spec.state);
    return {
      ...spec, status, note,
      took: overlap && Number.isFinite(own) && Number.isFinite(entered) ? own + entered - ahead : own ?? null,
      at: overlap ? ahead : entered ?? null,
    };
  });

  return {
    steps,
    reached,
    terminal,
    // Only a published review has walked the whole pipeline. A failed, cancelled
    // or superseded one stopped where it stopped and must not read as finished.
    percent: Math.round((reached / (pipeline.length - 1)) * 100),
    tone: FAILED.has(state) ? 'bad' : HALTED.has(state) ? 'warn' : '',
  };
}

// Progress is independent of the bounded activity feed. Worker snapshots count
// logical units, so transport/coverage retries never add work to the denominator.
export class UnitProgress {
  constructor() { this.stages = new Map(); }
  clear() { this.stages.clear(); }
  accept({kind, data = {}}) {
    if (!data.name) return false;
    const prior = this.stages.get(data.name);
    if (kind === 'stage_dispatch') {
      this.stages.set(data.name, {total: data.units, completed: null, running: null, idle: null, stopped: 0});
    } else if (kind === 'unit_progress') {
      if (prior?.execution_id === data.execution_id && prior?.revision >= data.revision) return false;
      this.stages.set(data.name, {...data});
    } else if (kind === 'stage_coverage' && !prior?.revision) {
      const completed = data.examined ?? 0, stopped = data.skipped ?? 0;
      this.stages.set(data.name, {total: prior?.total ?? completed + stopped,
        completed, running: 0, idle: Math.max(0, (prior?.total ?? completed + stopped) - completed - stopped), stopped});
    } else return false;
    return true;
  }
  get(step) {
    return step.stage ? this.stages.get(step.stage) : undefined;
  }
  close() {
    for (const value of this.stages.values()) {
      if (Number.isFinite(value.running)) {
        value.stopped = (value.stopped ?? 0) + value.running;
        value.running = 0;
      }
    }
  }
}
