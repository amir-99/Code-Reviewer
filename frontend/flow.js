/* The review pipeline as a flow, and the pure reading of where a review is in
   it. Nothing here touches the DOM: app.js paints what `walk` returns. */

export const FAILED = new Set(['FAILED_CONTEXT', 'FAILED_INTERNAL']);
export const HALTED = new Set(['TERMINATED_EARLY', 'CANCELLED', 'SUPERSEDED']);
export const TERMINAL = new Set([...FAILED, ...HALTED, 'PUBLISHED']);

export const label = value => String(value ?? '').replaceAll('_', ' ').trim().toLowerCase();

// The states the orchestrator walks, drawn as the flow it actually is: the
// sequential steps, the two gates that can end a review on a verified blocker,
// and the lanes that fan out concurrently before the system stage rejoins them.
// `stage` is the agent a step reports its work under.
export const FLOW = [
  {state: 'INIT', label: 'Admit', note: 'admission'},
  {state: 'CONTEXT_COLLECTION', label: 'Context', note: 'repository, requirements'},
  {state: 'STATIC_ANALYSIS', label: 'Static analysis', note: 'scanners'},
  {state: 'DEFECT_REVIEW', label: 'Defect review', stage: 'defect_review', note: 'concurrent chunks'},
  {state: 'PURPOSE_REVIEW', label: 'Purpose', stage: 'purpose', note: 'gate'},
  {state: 'DESIGN_REVIEW', label: 'Design', stage: 'design', note: 'gate'},
  {state: 'ANALYSIS_FAN_OUT', label: 'Analysis', note: 'concurrent', lanes: [
    ['correctness', 'Correctness'], ['complexity', 'Complexity'],
    ['tests_', 'Tests'], ['line_review', 'Line review'],
  ]},
  {state: 'SYSTEM_CONTEXT_REVIEW', label: 'System context', stage: 'system_context', note: 'aggregated'},
  {state: 'EVIDENCE_VALIDATION', label: 'Evidence', note: 'mechanical checks'},
  {state: 'FINDING_VERIFICATION', label: 'Verification', note: 'independent'},
  {state: 'FINALIZATION', label: 'Finalize', note: 'dedup, severity'},
  {state: 'DECISION', label: 'Decision', note: 'deterministic'},
  {state: 'PUBLISHED', label: 'Publish', note: 'report, status'},
];
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

/* Where the review stands, step by step.

   `stages` maps an agent name to {status, at, took} as its activity reported it,
   and `at` maps a state to when the pipeline entered it. A step reached but
   never walked was skipped outright — by a milestone short circuit or a gate
   that ended the review — and is not a step still to come. */
export function walk(state, history = [], stages = new Map(), at = new Map()) {
  const walked = history.length ? history : [state];
  const seen = new Set(walked);
  const standard = seen.has('DEFECT_REVIEW') || state === 'DEFECT_REVIEW' || stages.has('defect_review');
  const deepStates = new Set(['PURPOSE_REVIEW', 'DESIGN_REVIEW', 'ANALYSIS_FAN_OUT', 'SYSTEM_CONTEXT_REVIEW']);
  const flow = FLOW.filter(s => standard ? !deepStates.has(s.state) : s.state !== 'DEFECT_REVIEW');
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
    const reported = spec.stage ? stages.get(spec.stage) : null;
    const tone = stageTone(reported?.status);
    if (status === 'done' && tone && tone !== 'active') status = tone;
    const note = status === 'skipped' ? 'not run'
      : reported && reported.status !== 'started' && reported.status !== 'completed' ? label(reported.status)
      : status === 'stopped' || (status === 'failed' && !reported) ? label(state)
      : spec.note;
    const lanes = (spec.lanes ?? []).map(([stage, name]) => {
      const own = stages.get(stage);
      return {
        stage,
        label: name,
        // A lane the fan-out never reported did not run: only triage mode and an
        // early exit leave one silent, and neither is a lane that passed.
        status: own ? (stageTone(own.status) ?? 'done')
          : status === 'pending' || status === 'active' ? 'pending' : 'skipped',
        // Only the lanes worth a second look say anything: a clean one is its
        // colour and its time.
        note: !own ? 'not reported'
          : own.status === 'completed' || own.status === 'started' ? '' : label(own.status),
        took: own?.took ?? null,
        at: own?.at ?? null,
      };
    });
    return {...spec, status, note, lanes, took: took.get(spec.state) ?? null, at: at.get(spec.state) ?? null};
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
