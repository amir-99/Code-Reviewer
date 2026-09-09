import {matchesFinding} from './findings.js';
import {events} from './sse.js';
import {FAILED, HALTED, TERMINAL, label as labels, walk} from './flow.js';
import {chosenModels, modelFor, roleFor, roleLabel} from './models.js';
import {addAttempt, cost as money, emptySpend, mergeSpend, tokens as count, usage} from './spend.js';

const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const all = selector => Array.from(document.querySelectorAll(selector));

const SEVERITIES = ['BLOCKER', 'REQUIRED', 'SUGGESTION', 'QUESTION', 'NIT', 'FYI', 'PRAISE'];
const IMPACTS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'unknown'];
let impactLevel = 'all';
const DECISIONS = {APPROVE: 'Approve', REQUEST_CHANGES: 'Request changes', COMMENT_ONLY: 'Comment only'};
const VERDICTS = {fixed: 'Fixed', partially_fixed: 'Partially fixed', not_fixed: 'Still open',
  obsolete: 'No longer applies', unverifiable: 'Could not verify'};
const CONFIDENCE = {high: 1, medium: 0.6, low: 0.3};
const THEME_KEY = 'review-room-theme';
// Kinds beyond the four the filter chips name, and the chip each belongs under:
// a gateway attempt is reported inside the LLM tool call it was made for, and
// the run bracket spans the states of one worker run.
const GROUPS = {llm_attempt: 'tool', run: 'state', models: 'state'};
const KINDS = {llm_attempt: 'llm'};
const KEEP = 300;

let token = '', selected = '', current = null, controller, refreshTimer, lastRecheck = null;
let reviews = [], listFilter = 'all', listQuery = '', activityKind = 'all', severity = 'all', tabTouched = false;
let snapshotSeq = 0;            // the newest event the last snapshot already reflects
const activities = new Map();   // activity_id -> {row, kind, name, status, depth, at}
const running = new Set();
const stages = new Map();       // agent name -> {status, at, took}
const stateAt = new Map();      // review state -> when the pipeline entered it
const nodes = new Map();        // review state -> the flow node's elements
let modelDefaults = {};         // role -> the model a run uses when none is chosen
let modelCatalog = [];          // model IDs this deployment allows an operator to pick
const modelChoice = new Map();  // role -> the model chosen for the next manual run
let reviewModels = {};          // role -> model, as resolved by the review being watched
let audited = null;             // the review's spend as the last snapshot reported it
let streamed = emptySpend();    // attempts seen since that snapshot, not yet audited

const group = kind => GROUPS[kind] || kind;
const shows = kind => activityKind === 'all' || activityKind === group(kind);

const short = (sha, length = 8) => String(sha ?? '').slice(0, length);
const tone = state => FAILED.has(state) ? 'bad' : HALTED.has(state) ? 'warn' : state === 'PUBLISHED' ? 'ok' : 'run';

function ago(value) {
  const at = Date.parse(value ?? '');
  if (!Number.isFinite(at)) return '—';
  const seconds = Math.max(0, (Date.now() - at) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function span(milliseconds) {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function notice(message, kind = '') {
  const toast = el('div', message, `toast ${kind}`.trim());
  $('toasts').append(toast);
  while ($('toasts').children.length > 3) $('toasts').firstChild.remove();
  setTimeout(() => toast.remove(), 8000);
}

async function request(path, options = {}) {
  const response = await fetch('/api' + path, {...options, headers: {Authorization: `Bearer ${token}`, ...options.headers}});
  if (!response.ok) {
    let detail; try { detail = (await response.json()).detail; } catch {}
    const error = new Error(typeof detail === 'string' ? detail : `Request failed (${response.status})`);
    error.status = response.status; throw error;
  }
  return response;
}

/* ---------- theme ---------- */

function applyTheme(value) {
  if (value === 'dark' || value === 'light') document.documentElement.dataset.theme = value;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem(THEME_KEY)); } catch {}
$('theme').onclick = () => {
  const systemLight = matchMedia('(prefers-color-scheme: light)').matches;
  const active = document.documentElement.dataset.theme || (systemLight ? 'light' : 'dark');
  const next = active === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch {}
};

/* ---------- review list ---------- */

function connection(state, text) {
  $('conn').hidden = false;
  $('conn').className = `pill ${state}`;
  $('conn-text').textContent = text;
}

async function refresh() {
  const body = await (await request('/admin/reviews')).json();
  reviews = body.reviews || [];
  connection('live', 'Connected');
  renderReviews();
}

function refreshNow() {
  const button = $('refresh');
  button.classList.remove('spinning');
  void button.offsetWidth;
  button.classList.add('spinning');
  refresh().catch(error => { connection('bad', 'API unreachable'); notice(error.message, 'error'); });
}

const kept = review => {
  if (listFilter === 'running' && TERMINAL.has(review.state)) return false;
  if (listFilter === 'changes' && review.decision !== 'REQUEST_CHANGES') return false;
  if (listFilter === 'failed' && !FAILED.has(review.state) && !HALTED.has(review.state)) return false;
  if (!listQuery) return true;
  return `${review.project_id} !${review.mr_iid} ${review.head_sha} ${labels(review.state)} ${labels(review.decision)}`
    .toLowerCase().includes(listQuery);
};

function renderReviews() {
  const counts = {all: reviews.length, running: 0, changes: 0, failed: 0};
  for (const review of reviews) {
    if (!TERMINAL.has(review.state)) counts.running++;
    if (review.decision === 'REQUEST_CHANGES') counts.changes++;
    if (FAILED.has(review.state) || HALTED.has(review.state)) counts.failed++;
  }
  for (const node of all('#filters .n')) node.textContent = counts[node.dataset.count];
  $('review-total').textContent = counts.all;
  const shown = reviews.filter(kept);
  $('reviews').replaceChildren();
  if (!shown.length) {
    $('reviews').append(el('p', reviews.length ? 'No review matches this filter.' : 'No reviews yet.', 'empty-line'));
    return;
  }
  for (const review of shown) $('reviews').append(reviewRow(review));
}

function reviewRow(review) {
  const button = el('button', '', `s-${tone(review.state)}${review.id === selected ? ' selected' : ''}`);
  button.type = 'button';
  const top = el('div', '', 'top');
  top.append(el('i', '', 'dot'), el('span', `Project ${review.project_id} · !${review.mr_iid}`, 'grow'));
  if (review.decision) top.append(decisionBadge(review.decision));
  const bottom = el('div', '', 'bottom');
  bottom.append(el('span', labels(review.state)), el('span', short(review.head_sha), 'sha'));
  if (review.partial) bottom.append(el('span', 'partial', 'badge tone-warn'));
  const when = el('time', ago(review.started_at));
  when.dateTime = review.started_at ?? '';
  when.dataset.ago = review.started_at ?? '';
  bottom.append(when);
  button.append(top, bottom);
  button.onclick = () => select(review.id);
  return button;
}

function decisionBadge(decision) {
  const shade = decision === 'APPROVE' ? 'tone-good' : decision === 'REQUEST_CHANGES' ? 'tone-bad' : 'tone-info';
  return el('span', DECISIONS[decision] || labels(decision), `badge ${shade}`);
}

/* ---------- review header ---------- */

function metric(term, value, {shade = '', mono = false, at = '', elapsed = false, id = ''} = {}) {
  const box = el('div', '', `metric ${shade}`.trim());
  const detail = el('dd', value, mono ? 'mono' : '');
  if (at) detail.dataset.ago = at;
  if (elapsed) detail.dataset.elapsed = '';
  if (id) box.id = id;
  box.append(el('dt', term), detail);
  return box;
}

function renderHead(review) {
  const overrides = review.overrides || {};
  $('identity').textContent = `Project ${review.project_id} · Merge request !${review.mr_iid}`;
  $('state').textContent = labels(review.state);
  $('metadata').textContent = [
    `head ${short(review.head_sha, 12)}`,
    overrides.requested_by ? `triggered by ${overrides.requested_by}` : 'triggered by webhook',
    overrides.issue_key ? `story ${overrides.issue_key}` : null,
    overrides.epic_key ? `epic ${overrides.epic_key}` : null,
    (overrides.document_urls || []).length ? `${overrides.document_urls.length} supplied page(s)` : null,
  ].filter(Boolean).join('  ·  ');

  $('decision').hidden = !review.decision;
  if (review.decision) {
    $('decision').textContent = DECISIONS[review.decision] || labels(review.decision);
    $('decision').className = decisionBadge(review.decision).className;
  }

  if (review.error) {
    $('alert').hidden = false; $('alert').className = 'alert';
    $('alert').textContent = `This review recorded an error: ${review.error}`;
  } else if (review.partial) {
    $('alert').hidden = false; $('alert').className = 'alert warn';
    $('alert').textContent = 'Partial review — coverage is incomplete, so the decision falls back to comment only and the commit status passes.';
  } else {
    $('alert').hidden = true;
  }

  const findings = review.findings || [];
  // Stored findings include ones the pipeline discarded; only live ones are counted.
  const high = findings.filter(f => (f.severity === 'BLOCKER' || f.severity === 'REQUIRED')
    && !['discarded', 'suppressed', 'resolved'].includes(f.status)).length;
  const finished = Date.parse(review.finished_at ?? '');
  const started = Date.parse(review.started_at ?? '');
  $('metrics').replaceChildren(
    metric('Head', short(review.head_sha, 12), {mono: true}),
    metric('Started', ago(review.started_at), {at: review.started_at ?? ''}),
    Number.isFinite(finished) && Number.isFinite(started)
      ? metric('Took', span(finished - started))
      : metric('Elapsed', Number.isFinite(started) ? span(Date.now() - started) : '—', {elapsed: true}),
    metric('Findings', String(findings.length)),
    metric('Blocker / required', String(high), {shade: high ? 'bad' : 'good'}),
    metric('Commit status', review.status_delivered ? 'delivered' : 'pending',
      {shade: !review.status_delivered && TERMINAL.has(review.state) ? 'warn' : ''}),
    metric('Report mode', overrides.report_mode || 'project default'),
    metric('Tokens', '—', {mono: true, id: 'metric-tokens'}),
    metric('Cost', '—', {mono: true, id: 'metric-cost'}),
  );
  renderHeadSpend();

  renderTrail(review.history);
  renderPipeline(review.state, review.history);
}

// The header answers "what has this cost so far" without leaving the tab that
// is open; the Spend tab answers "where did it go".
function renderHeadSpend() {
  const spend = mergeSpend(audited, streamed);
  const budget = usage(spend);
  const tokensBox = $('metric-tokens');
  const costBox = $('metric-cost');
  if (tokensBox) {
    tokensBox.lastChild.textContent = spend.calls ? count(spend.tokens) : '—';
    tokensBox.className = `metric ${budget && budget.percent >= 90 ? 'bad' : budget && budget.percent >= 70 ? 'warn' : ''}`.trim();
    tokensBox.title = budget
      ? `${budget.used.toLocaleString()} of ${budget.ceiling.toLocaleString()} tokens in this review's ceiling`
      : `${spend.tokens.toLocaleString()} tokens over ${spend.calls} gateway calls`;
  }
  if (costBox) {
    costBox.lastChild.textContent = spend.calls ? money(spend.cost, {unpriced: spend.unpriced_calls}) : '—';
    costBox.title = spend.unpriced_calls
      ? `${spend.unpriced_calls} call(s) ran on a model with no configured price, so this is a floor`
      : 'Configured price of every model call this review made';
  }
}

function renderTrail(history) {
  const states = (history || []).slice(-14);
  $('trail').replaceChildren();
  states.forEach((state, index) => {
    if (index) $('trail').append(el('i', '›'));
    $('trail').append(el('span', labels(state), index === states.length - 1 ? 'now' : ''));
  });
}

/* ---------- pipeline flow ---------- */

function buildFlow() {
  nodes.clear();
  const flow = $('flow');
  flow.replaceChildren();
  const cell = (kind, text) => {
    const box = el(kind === 'lane' ? 'span' : 'div', '', kind);
    const top = el('span', '', 'n-top');
    top.append(el('i', '', 'n-dot'), el('span', text, 'n-label'), el('span', '', 'n-time'));
    box.append(top, el('span', '', 'n-note'));
    return box;
  };
  // The flow's own shape, read from an unstarted review: labels and lanes only.
  walk('INIT').steps.forEach((step, index) => {
    if (index) flow.append(el('i', '', 'link'));
    const node = cell('node', step.label);
    node.dataset.node = step.state;
    const entry = {node, lanes: new Map()};
    if (!step.lanes.length) flow.append(node);
    else {
      // The fan-out is the one place the flow branches: its lanes hang off the
      // node that dispatched them and rejoin at the step that follows.
      const branch = el('div', '', 'branch');
      const lanes = el('div', '', 'lanes');
      for (const lane of step.lanes) {
        const box = cell('lane', lane.label);
        box.dataset.lane = lane.stage;
        entry.lanes.set(lane.stage, box);
        lanes.append(box);
      }
      branch.append(node, lanes);
      flow.append(branch);
    }
    nodes.set(step.state, entry);
  });
}

function paint(target, step) {
  if (!target) return;
  target.className = `${target.dataset.lane ? 'lane' : 'node'} ${step.status}`;
  const time = target.querySelector('.n-time');
  delete time.dataset.since;
  if (step.status === 'active' && Number.isFinite(step.at)) {
    // A step in flight counts up; a finished one keeps the time it took.
    time.dataset.since = step.at;
    time.textContent = span(Date.now() - step.at);
  } else time.textContent = Number.isFinite(step.took) ? span(step.took) : '';
  target.querySelector('.n-note').textContent = step.note ?? '';
  // Colour alone never carries the status: the title always spells it out.
  target.title = `${step.label} · ${step.status}${step.note ? ` · ${step.note}` : ''}`;
}

function renderPipeline(state = current?.state ?? 'INIT', history = current?.history ?? []) {
  const flow = walk(state, history, stages, stateAt);
  $('track-fill').style.setProperty('--progress', flow.percent);
  $('track').className = `track${flow.terminal ? '' : ' running'}${flow.tone ? ` ${flow.tone}` : ''}`;
  for (const step of flow.steps) {
    const entry = nodes.get(step.state);
    if (!entry) continue;
    paint(entry.node, step);
    for (const lane of step.lanes) paint(entry.lanes.get(lane.stage), lane);
  }
}

function applyState(state) {
  // A snapshot already carries every state it walked, and the stream replays the
  // events that produced them. Applying one twice doubles the trail and rewinds
  // the header, so only a state the review is not already in moves it on.
  if (!current || current.state === state) return;
  current.state = state;
  current.history = [...(current.history || []), state];
  $('state').textContent = labels(state);
  renderTrail(current.history);
  renderPipeline(state, current.history);
}

function markStage(item) {
  const {data, at} = item;
  const when = Date.parse(at);
  const open = stages.get(data.name);
  stages.set(data.name, data.status === 'started'
    ? {status: 'started', at: when}
    : {status: data.status, at: open?.at, took: Number.isFinite(open?.at) ? when - open.at : null});
  renderPipeline();
}

/* ---------- findings ---------- */

function severityTone(value) {
  if (value === 'BLOCKER' || value === 'REQUIRED') return 'tone-bad';
  if (value === 'SUGGESTION') return 'tone-info';
  if (value === 'QUESTION') return 'tone-note';
  if (value === 'PRAISE') return 'tone-good';
  return '';
}

function copyable(text, what) {
  const node = el('button', text, 'where');
  node.type = 'button';
  node.title = `Copy ${what}`;
  node.onclick = () => copy(text, what);
  return node;
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); notice(`${what} copied.`, 'ok'); }
  catch { notice('The browser refused clipboard access.', 'error'); }
}

function renderFindings() {
  const findings = current?.findings || [];
  const counts = new Map();
  for (const finding of findings) counts.set(finding.severity, (counts.get(finding.severity) || 0) + 1);
  if (severity !== 'all' && !counts.get(severity)) severity = 'all';

  $('count').textContent = String(findings.length);
  $('tabn-findings').textContent = String(findings.length);
  $('sevbar').hidden = !findings.length;
  $('sevbar').replaceChildren(...SEVERITIES.filter(name => counts.get(name)).map(name => {
    const segment = el('i', '', name);
    segment.style.setProperty('--share', counts.get(name));
    segment.title = `${counts.get(name)} ${labels(name)}`;
    return segment;
  }));

  const chip = (key, text, number) => {
    const button = el('button', text, `chip${severity === key ? ' on' : ''}`);
    button.type = 'button';
    if (number !== undefined) button.append(el('span', String(number), 'n'));
    button.onclick = () => { severity = key; renderFindings(); };
    return button;
  };
  $('severity-filters').replaceChildren(
    ...(findings.length ? [chip('all', 'All', findings.length)] : []),
    ...SEVERITIES.filter(name => counts.get(name)).map(name => chip(name, labels(name), counts.get(name))),
  );

  const impactCounts = new Map();
  for (const f of findings) {
    const level = f.impact_level || 'unknown';
    impactCounts.set(level, (impactCounts.get(level) || 0) + 1);
  }
  $('impact-filters').replaceChildren(...['all', ...IMPACTS].map(level => {
    const button = el('button', level === 'all' ? 'All impacts' : labels(level),
      `chip${impactLevel === level ? ' on' : ''}`);
    button.type = 'button';
    button.append(el('span', String(level === 'all' ? findings.length : impactCounts.get(level) || 0), 'n'));
    button.onclick = () => { impactLevel = level; renderFindings(); };
    return button;
  }));

  const shown = findings.filter(f => matchesFinding(f, severity, impactLevel))
    .slice().sort((a, b) => SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity));
  $('findings').replaceChildren();
  if (!shown.length) {
    $('findings').append(el('p', findings.length ? 'No findings match these filters.' : 'No stored findings to show.', 'empty-line'));
    return;
  }
  for (const finding of shown) $('findings').append(findingCard(finding));
}

function findingCard(finding) {
  const article = el('article', '', `finding sev-${finding.severity || 'NONE'}`);
  const top = el('div', '', 'top');
  top.append(el('span', labels(finding.severity) || 'finding', `badge ${severityTone(finding.severity)}`));
  top.append(el('span', `Impact: ${finding.impact_level || 'unknown'} (advisory)`, 'badge'));
  if (finding.category) top.append(el('span', labels(finding.category), 'badge'));
  top.append(el('span', finding.introduced_by_this_change ? 'this change' : 'pre-existing',
    `badge ${finding.introduced_by_this_change ? 'tone-info' : ''}`.trim()));
  if (finding.verdict) {
    top.append(el('span', finding.verdict, `badge ${finding.verdict === 'confirmed' ? 'tone-good' : 'tone-warn'}`));
  } else {
    top.append(el('span', 'unverified', 'badge tone-warn'));
  }
  article.append(top, el('h3', finding.claim || 'Untitled finding'));

  if (finding.file) {
    const lines = finding.line_end && finding.line_end !== finding.line_start
      ? `${finding.line_start}-${finding.line_end}` : finding.line_start;
    article.append(copyable(lines ? `${finding.file}:${lines}` : finding.file, 'the location'));
  }

  const body = el('div', '', 'body');
  for (const [key, term] of [['reason', 'Why'], ['impact', 'Impact'], ['failure_scenario', 'Failure'], ['suggested_direction', 'Direction']]) {
    if (!finding[key]) continue;
    const paragraph = el('p');
    paragraph.append(el('b', `${term} — `), document.createTextNode(finding[key]));
    body.append(paragraph);
  }
  article.append(body);

  const evidence = (finding.evidence || []).slice(0, 4);
  if (evidence.length) {
    const list = el('ul', '', 'evidence');
    for (const item of evidence) {
      list.append(el('li', `${item.file ?? ''}:${item.line_start ?? ''}-${item.line_end ?? ''} — ${item.note ?? ''}`));
    }
    article.append(list);
  }

  const footer = el('footer');
  footer.append(el('span', labels(finding.stage)), el('span', labels(finding.status)));
  if (finding.confidence) {
    const meter = el('span', '', 'meter');
    const bar = el('span', '', 'bar');
    const fill = el('i');
    fill.style.setProperty('--v', CONFIDENCE[finding.confidence] ?? 0.3);
    bar.append(fill);
    meter.append(el('span', `${finding.confidence} confidence`), bar);
    footer.append(meter);
  }
  if (finding.requirement_ref) footer.append(el('span', `requirement ${finding.requirement_ref}`));
  if (finding.resolution) footer.append(el('span', labels(finding.resolution), 'badge'));
  article.append(footer);
  return article;
}

/* ---------- report and recheck ---------- */

function renderReport(report) {
  $('report').textContent = report || '';
  $('report').hidden = !report;
  $('report-empty').hidden = !!report;
  $('copy-report').disabled = !report;
  $('tabn-report').hidden = !report;
  $('tabn-report').textContent = '✓';
}

function renderRecheck(recheck) {
  lastRecheck = JSON.stringify(recheck ?? null);
  const answers = recheck?.verdicts || [];
  $('tabn-recheck').hidden = !answers.length;
  $('tabn-recheck').textContent = String(answers.length);
  $('recheck-count').textContent = String(answers.length);
  $('recheck-results').replaceChildren();
  if (!recheck) {
    $('recheck-meta').textContent = '';
    $('recheck-results').append(el('p', 'This review has not been rechecked. Use “Recheck comments” to re-judge its open threads at the current head.', 'empty-line'));
    return;
  }
  const resolved = new Set((recheck.posted || []).filter(entry => entry.resolved).map(entry => entry.fingerprint));
  $('recheck-meta').textContent = [
    recheck.mode === 'draft' ? 'Drafted on GitLab' : 'Posted on the merge request',
    `head ${short(recheck.head_sha, 12)}`,
    recheck.at ? new Date(recheck.at).toLocaleString() : '',
  ].filter(Boolean).join(' · ');
  if (!answers.length) {
    $('recheck-results').append(el('p', 'The recheck found no open reviewer threads to judge.', 'empty-line'));
    return;
  }
  for (const answer of answers) {
    const article = el('article', '', 'finding');
    const top = el('div', '', 'top');
    top.append(el('span', VERDICTS[answer.verdict] || labels(answer.verdict), `badge ${answer.verdict}`));
    top.append(el('span', answer.judged ? 'judged by model' : 'determined from the diff', 'badge'));
    top.append(el('span', resolved.has(answer.fingerprint) ? 'thread resolved' : 'thread left open',
      `badge ${resolved.has(answer.fingerprint) ? 'tone-good' : 'tone-warn'}`));
    article.append(top, el('h3', answer.claim || 'Reviewer thread'));
    if (answer.file) article.append(copyable(`${answer.file}:${answer.line ?? ''}`, 'the location'));
    const body = el('div', '', 'body');
    for (const [key, term] of [['change_summary', 'What changed'], ['reasoning', 'Reasoning']]) {
      if (!answer[key]) continue;
      const paragraph = el('p');
      paragraph.append(el('b', `${term} — `), document.createTextNode(answer[key]));
      body.append(paragraph);
    }
    article.append(body);
    $('recheck-results').append(article);
  }
}

/* ---------- activity feed ---------- */

function statusTone(status) {
  if (status === 'completed') return 'tone-good';
  if (status === 'partial' || status === 'no completion') return 'tone-warn';
  if (status === 'failed' || status === 'cancelled') return 'tone-bad';
  return '';
}

function fillRight(row, status, took, since) {
  const right = row.lastChild;
  right.replaceChildren();
  row.dataset.status = status || '';
  if (took) right.append(el('span', took, 'took'));
  if (status === 'started') {
    // Something still in flight is more useful counting up than spinning: a work
    // unit four minutes in is the reason a review looks stuck.
    const live = el('span', '0s', 'took live');
    if (Number.isFinite(since)) live.dataset.since = since;
    right.append(live, el('i', '', 'spinner'));
  } else right.append(el('span', status || 'transition', `badge ${statusTone(status)}`));
}

// What one gateway attempt spent, said on the row that reports the attempt.
function attemptSpend(data) {
  const total = (Number(data.tokens_in) || 0) + (Number(data.tokens_out) || 0);
  if (!total) return '';
  const priced = typeof data.cost === 'number' ? ` · ${money(data.cost)}` : ' · unpriced';
  return ` · ${count(total)} tok${priced}`;
}

function setModel(row, model) {
  const chip = row.querySelector('.model');
  if (!chip) return;
  chip.textContent = model || '';
  chip.hidden = !model;
  chip.title = model ? `Model for this step: ${model}` : '';
}

// A review announces its models before any stage starts, but a reconnect can
// replay activity ahead of the snapshot that carries them. Fill the rows that
// were drawn without one rather than leaving them blank for the whole run.
function paintModels() {
  for (const entry of activities.values()) {
    if (entry.model || !entry.role) continue;
    const model = reviewModels[entry.role];
    if (model) { entry.model = model; setModel(entry.row, model); }
  }
}

function activityRow(kind, name, parent, at, status, depth, model) {
  const row = el('li', '', `k-${kind}${status === 'started' ? ' running' : ''}`);
  row.style.setProperty('--depth', depth);
  row.dataset.kind = kind;
  row.hidden = !shows(kind);
  const time = el('time', new Date(at).toLocaleTimeString());
  time.dateTime = at ?? '';
  const label = el('span', '', 'label');
  label.append(el('span', KINDS[kind] || kind, 'kind'), el('span', name, 'name'));
  if (parent) label.append(el('span', `· ${parent}`, 'parent'));
  const chip = el('span', '', 'model');
  chip.hidden = true;
  label.append(chip);
  row.append(time, label, el('span', '', 'right'));
  setModel(row, model);
  fillRight(row, status, null, Date.parse(at));
  return row;
}

function countRunning() {
  // Name what is in flight rather than only counting it: with four stages fanned
  // out at once, which ones are still going is the question being asked.
  const named = [...running].map(id => activities.get(id)).filter(Boolean)
    .filter(entry => entry.kind === 'agent' || entry.kind === 'unit')
    .map(entry => entry.name);
  const badge = $('running');
  badge.hidden = !running.size;
  badge.textContent = named.length
    ? `${running.size} running · ${named.slice(0, 3).join(' · ')}${named.length > 3 ? ` +${named.length - 3}` : ''}`
    : `${running.size} running`;
}

// One count per filter, read off the feed so a bounded row never inflates it.
function tally() {
  const counts = {all: 0, state: 0, agent: 0, unit: 0, tool: 0};
  for (const row of $('activity').children) {
    counts.all++;
    const key = group(row.dataset.kind);
    if (key in counts) counts[key]++;
  }
  for (const node of all('#activity-filters .n')) node.textContent = String(counts[node.dataset.tally] ?? 0);
}

function addActivity(item) {
  const {data = {}, kind} = item;
  // Everything at or below the snapshot's cursor is a replay of what the
  // snapshot already reflects. Those events still belong in the feed, where they
  // are the record of what ran, but they must not move the header a second time.
  if (kind === 'state') {
    // Replayed or live, the times are what the flow reports each step took.
    if (!stateAt.has(data.state)) stateAt.set(data.state, Date.parse(item.at));
    if (Number(item.id) > snapshotSeq) applyState(data.state);
    else renderPipeline();
  }
  if (kind === 'agent') markStage(item);
  // The run announces the model it resolved for every role before the first
  // stage starts; it is the record of what produced this review.
  if (kind === 'models') { reviewModels = {...reviewModels, ...data}; paintModels(); }
  if (kind === 'budget') { audited = {...(audited || emptySpend()), ...data}; renderSpend(); }
  // Only attempts the snapshot has not already audited: everything at or below
  // its cursor is a replay of calls already counted in the numbers it carried.
  if (kind === 'llm_attempt' && Number(item.id) > snapshotSeq) {
    addAttempt(streamed, data);
    renderSpend();
    renderHeadSpend();
  }

  const known = data.activity_id ? activities.get(data.activity_id) : null;
  if (known) {
    // One row per activity: the completion lands on the row its start opened.
    known.status = data.status;
    known.row.classList.toggle('running', data.status === 'started');
    fillRight(known.row, data.status, span(Date.parse(item.at) - known.at), Date.parse(item.at));
    // A recovered run reopens its own marker: only a finish stops counting it.
    if (data.status === 'started') running.add(data.activity_id);
    else running.delete(data.activity_id);
    countRunning();
    return;
  }

  const ancestor = data.parent_id ? activities.get(data.parent_id) : null;
  const depth = kind === 'state' ? 0 : Math.min((ancestor ? ancestor.depth + 1 : 0), 4);
  // A gateway attempt reports its outcome instead of a start/finish pair, and a
  // model selection is a fact about the run rather than a step that runs.
  const status = kind === 'llm_attempt'
    ? (data.outcome === 'success' ? 'completed' : labels(data.outcome) || 'attempt')
    : kind === 'models' ? 'selected'
    : data.status;
  const name = kind === 'state' ? labels(data.state)
    : kind === 'agent' ? labels(data.name)
    : kind === 'llm_attempt' ? `${labels(data.stage)} · attempt ${data.transport_attempt ?? 1}${attemptSpend(data)}`
    : kind === 'models' ? `${Object.keys(data).length} roles · ${[...new Set(Object.values(data))].join(' · ')}`
    : String(data.name ?? '');
  const role = roleFor(kind, data, ancestor);
  const model = modelFor(kind, data, role, reviewModels);
  const row = activityRow(kind, name, ancestor?.name, item.at, status, depth, model);
  if (data.activity_id) {
    row.dataset.activity = data.activity_id;
    activities.set(data.activity_id, {row, kind, name, depth, status, role, model, at: Date.parse(item.at)});
    if (status === 'started') running.add(data.activity_id); else running.delete(data.activity_id);
    countRunning();
  }
  $('activity').prepend(row);
  $('activity-empty').hidden = true;
  prune();
}

// The feed is bounded; every retained event stays available through the API.
// Rows for activities still in flight are kept whatever their age: stages fan
// out in parallel, so the oldest rows are the ones still waiting on a completion
// that has to land on the row its start opened.
function prune() {
  const list = $('activity');
  let node = list.lastChild;
  while (node && list.children.length > KEEP) {
    const previous = node.previousSibling;
    const id = node.dataset.activity;
    if (!id || !running.has(id)) {
      if (id) activities.delete(id);
      node.remove();
    }
    node = previous;
  }
  $('tabn-activity').textContent = String(list.children.length);
  tally();
}

function filterActivity() {
  let visible = 0;
  for (const row of $('activity').children) {
    row.hidden = !shows(row.dataset.kind);
    if (!row.hidden) visible++;
  }
  $('activity-empty').hidden = visible > 0;
  $('activity-empty').textContent = $('activity').children.length
    ? 'No activity of this kind yet.' : 'Waiting for the first event…';
}

function closeOpenActivities() {
  for (const id of running) {
    const entry = activities.get(id);
    if (!entry) continue;
    entry.row.classList.remove('running');
    // Process termination can leave a start without its completion event.
    fillRight(entry.row, 'no completion', null);
  }
  running.clear();
  countRunning();
  // A stage whose completion never landed is not a stage that passed.
  for (const [name, stage] of stages) {
    if (stage.status === 'started') stages.set(name, {...stage, status: 'no completion'});
  }
  renderPipeline();
}

/* ---------- tabs ---------- */

function showTab(name) {
  for (const tab of all('.tab')) {
    const on = tab.dataset.tab === name;
    tab.setAttribute('aria-selected', String(on));
    $(`panel-${tab.dataset.tab}`).hidden = !on;
  }
}
for (const tab of all('.tab')) tab.onclick = () => { tabTouched = true; showTab(tab.dataset.tab); };

/* ---------- spend ---------- */

function renderSpend() {
  const spend = mergeSpend(audited, streamed);
  const total = money(spend.cost, {unpriced: spend.unpriced_calls});
  $('tabn-spend').textContent = String(spend.calls);
  $('spend-total').textContent = total;
  $('spend-meta').textContent = [
    `${spend.calls} gateway ${spend.calls === 1 ? 'call' : 'calls'}`,
    spend.retries ? `${spend.retries} unsuccessful` : null,
    `${count(spend.tokens_in)} in · ${count(spend.tokens_out)} out`,
    // Silence about missing prices would read as a complete total.
    spend.unpriced_calls ? `${spend.unpriced_calls} call(s) on models with no configured price` : null,
  ].filter(Boolean).join('  ·  ');

  const budget = usage(spend);
  $('budget-track').hidden = !budget;
  $('budget-text').textContent = budget
    ? `${count(budget.used)} of ${count(budget.ceiling)} tokens · ${budget.percent.toFixed(budget.percent < 10 ? 1 : 0)}% of the review's ceiling`
    : 'This review has not reported a token ceiling.';
  if (budget) {
    // Same track the pipeline uses: progress on the fill, tone on the track.
    $('budget-fill').style.setProperty('--progress', budget.percent);
    $('budget-track').className = `track ${budget.percent >= 90 ? 'bad' : budget.percent >= 70 ? 'warn' : ''}`.trim();
  }

  const most = Math.max(1, ...spend.roles.map(row => row.cost || 0), 0.0000001);
  $('spend-rows').replaceChildren(...spend.roles.map(row => {
    const line = el('tr');
    // The share bar is drawn against the costliest role, so the row worth
    // changing a model for is the one that reads as full.
    line.style.setProperty('--share', `${Math.min(100, ((row.cost || 0) / most) * 100)}%`);
    line.append(
      el('td', roleLabel(row.role)),
      el('td', row.model || '—', 'mono'),
      el('td', String(row.calls) + (row.retries ? ` (${row.retries}✗)` : ''), 'num'),
      el('td', count(row.tokens_in), 'num'),
      el('td', count(row.tokens_out), 'num'),
      el('td', count(row.tokens), 'num'),
      el('td', money(row.cost, {unpriced: row.unpriced_calls}), 'num cost'),
    );
    return line;
  }));
  $('spend-empty').hidden = spend.roles.length > 0;
  return spend;
}

/* ---------- model selection ---------- */

async function loadModels() {
  const body = await (await request('/admin/models')).json();
  modelDefaults = body.defaults || {};
  modelCatalog = body.catalog || [];
  const roles = (body.roles || Object.keys(modelDefaults)).filter(role => modelDefaults[role]);
  $('models').replaceChildren(...roles.map(modelField));
  markModelChoices();
}

function modelField(role) {
  const field = el('div', '', 'field model-field');
  const select = el('select');
  select.id = `model-${role}`;
  select.dataset.role = role;
  // The empty value is not "no model": it is this deployment's own choice for
  // the role, which is what the run uses when the operator names nothing.
  const fallback = el('option', `Default — ${modelDefaults[role]}`);
  fallback.value = '';
  select.append(fallback);
  for (const model of modelCatalog) {
    const option = el('option', model);
    option.value = model;
    if (model === modelChoice.get(role)) option.selected = true;
    select.append(option);
  }
  select.onchange = () => {
    if (select.value) modelChoice.set(role, select.value); else modelChoice.delete(role);
    markModelChoices();
  };
  const label = el('label', roleLabel(role));
  label.htmlFor = select.id;
  field.append(label, select);
  return field;
}

// Selection is optional and easy to forget about, so say how much of this run
// is no longer running on the configured defaults.
function markModelChoices() {
  const changed = modelChoice.size;
  $('models-changed').hidden = !changed;
  $('models-changed').textContent = String(changed);
  $('models-reset').hidden = !changed;
  for (const select of all('#models select')) {
    select.classList.toggle('chosen', Boolean(modelChoice.get(select.dataset.role)));
  }
}

/* ---------- streaming ---------- */

function live(state, text) {
  $('live').className = `pill ${state}`;
  $('live-text').textContent = text;
}

const delay = (ms, signal) => new Promise(resolve => {
  const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
  const timer = setTimeout(done, ms); signal.addEventListener('abort', done, {once: true});
  if (signal.aborted) done();
});

function render(review) {
  current = review;
  snapshotSeq = Number(review.sequence ?? 0);
  if (review.models) { reviewModels = {...reviewModels, ...review.models}; paintModels(); }
  // The snapshot is the audited record up to its own cursor, so it replaces
  // both sides rather than adding to them.
  audited = review.spend || null;
  streamed = emptySpend();
  renderSpend();
  renderHead(review);
  renderFindings();
  renderReport(review.report);
  renderRecheck(review.recheck);
}

async function follow(id, signal) {
  let cursor = '0';
  while (!signal.aborted) {
    try {
      live('', 'Connecting');
      const response = await request(`/admin/reviews/${encodeURIComponent(id)}/events`,
        {signal, headers: {'Last-Event-ID': cursor, Accept: 'text/event-stream'}});
      live('live', 'Live');
      for await (const event of events(response.body)) {
        if (signal.aborted) return;
        if (event.event === 'snapshot') render(event.data);
        if (event.event === 'activity' && Number(event.id) > Number(cursor)) { addActivity(event.data); cursor = event.id; }
        if (event.event === 'complete') {
          live(tone(event.data?.state) === 'bad' ? 'bad' : 'done', 'Finished');
          closeOpenActivities();
          if (!tabTouched && current?.findings?.length) showTab('findings');
          refresh().catch(() => {});
          return;
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      if ([401, 403, 404].includes(error.status)) { live('bad', 'Disconnected'); notice(error.message, 'error'); return; }
    }
    live('warn', 'Reconnecting…');
    await delay(2000, signal);
  }
}

function select(id) {
  controller?.abort();
  controller = new AbortController();
  selected = id;
  current = null;
  snapshotSeq = 0;
  reviewModels = {};
  audited = null;
  streamed = emptySpend();
  tabTouched = false;
  severity = 'all';
  impactLevel = 'all';
  activities.clear();
  running.clear();
  countRunning();
  $('empty').hidden = true;
  $('review').hidden = false;
  $('activity').replaceChildren();
  $('activity-empty').hidden = false;
  $('activity-empty').textContent = 'Waiting for the first event…';
  $('tabn-activity').textContent = '0';
  $('identity').textContent = '';
  $('state').textContent = 'Loading review…';
  $('metadata').textContent = '';
  $('metrics').replaceChildren();
  $('trail').replaceChildren();
  $('alert').hidden = true;
  $('decision').hidden = true;
  stages.clear();
  stateAt.clear();
  buildFlow();
  renderPipeline('INIT', []);
  tally();
  renderFindings();
  renderReport('');
  renderRecheck(null);
  showTab('activity');
  renderReviews();
  refresh().catch(error => notice(error.message, 'error'));
  follow(id, controller.signal).catch(error => notice(error.message, 'error'));
}

/* ---------- live relative times ---------- */

setInterval(() => {
  for (const node of all('[data-ago]')) node.textContent = ago(node.dataset.ago);
  for (const node of all('[data-since]')) node.textContent = span(Date.now() - Number(node.dataset.since));
  const elapsed = document.querySelector('[data-elapsed]');
  if (elapsed && current && !current.finished_at) {
    const started = Date.parse(current.started_at ?? '');
    if (Number.isFinite(started)) elapsed.textContent = span(Date.now() - started);
  }
}, 1000);

/* ---------- wiring ---------- */

$('login').onsubmit = async event => {
  event.preventDefault();
  token = $('token').value;
  try {
    await refresh();
    $('token').value = '';
    $('connect').hidden = true;
    $('workspace').hidden = false;
    $('disconnect').hidden = false;
    // Selection is optional: a deployment that cannot report its models still
    // triggers reviews, they just run on whatever it is configured with.
    loadModels().catch(() => {
      $('models-panel').hidden = true;
      notice('Model selection is unavailable; runs will use the configured defaults.');
    });
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => refresh().catch(() => connection('bad', 'API unreachable')), 15000);
  } catch (error) {
    token = '';
    $('conn').hidden = true;
    notice(error.message, 'error');
  }
};

$('models-reset').onclick = () => {
  modelChoice.clear();
  for (const select of all('#models select')) select.value = '';
  markModelChoices();
};

$('disconnect').onclick = () => { controller?.abort(); clearInterval(refreshTimer); token = ''; location.reload(); };
$('refresh').onclick = refreshNow;
$('copy-report').onclick = () => copy($('report').textContent, 'The report');
$('search').oninput = event => { listQuery = event.target.value.trim().toLowerCase(); renderReviews(); };

for (const chip of all('#filters .chip')) {
  chip.onclick = () => {
    listFilter = chip.dataset.filter;
    for (const other of all('#filters .chip')) other.classList.toggle('on', other === chip);
    renderReviews();
  };
}

for (const chip of all('#activity-filters .chip')) {
  chip.onclick = () => {
    activityKind = chip.dataset.kind;
    for (const other of all('#activity-filters .chip')) other.classList.toggle('on', other === chip);
    filterActivity();
  };
}

addEventListener('keydown', event => {
  if (!token || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target?.matches?.('input, textarea, select, [contenteditable]')) {
    if (event.key === 'Escape') event.target.blur();
    return;
  }
  if (event.key === '/') { event.preventDefault(); $('search').focus(); }
  if (event.key === 'r') refreshNow();
});

$('recheck').onclick = async () => {
  if (!selected) return;
  const id = selected, before = lastRecheck;
  $('recheck').disabled = true;
  try {
    await request(`/admin/reviews/${encodeURIComponent(id)}/recheck`, {method: 'POST'});
    notice('Recheck queued. Judging the open threads at the current head…');
    // The review is already terminal, so its event stream reports nothing more:
    // read the answers back from the review itself.
    for (let attempt = 0; attempt < 40 && selected === id; attempt++) {
      await delay(3000, controller.signal);
      if (selected !== id) return;
      const review = await (await request(`/admin/reviews/${encodeURIComponent(id)}`)).json();
      if (JSON.stringify(review.recheck ?? null) !== before) {
        renderRecheck(review.recheck);
        showTab('recheck');
        notice('Recheck answered the open threads.', 'ok');
        return;
      }
    }
    notice('No recheck answers recorded yet. The review may have no open reviewer threads, or recheck may be off for this project.');
  } catch (error) { if (error.name !== 'AbortError') notice(error.message, 'error'); }
  finally { $('recheck').disabled = false; }
};

$('trigger').onsubmit = async event => {
  event.preventDefault();
  $('start').disabled = true;
  const data = new FormData(event.target);
  const body = {
    merge_request_url: data.get('merge_request_url'),
    report_mode: data.get('report_mode'),
    document_urls: data.get('documents').split('\n').map(line => line.trim()).filter(Boolean),
  };
  for (const key of ['issue_key', 'epic_key']) if (data.get(key).trim()) body[key] = data.get(key).trim();
  // Only the roles actually moved: every other role stays on project policy,
  // and a run that names nothing is exactly a webhook run.
  const models = chosenModels(modelChoice);
  if (Object.keys(models).length) body.models = models;
  try {
    const job = await (await request('/admin/reviews', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json();
    notice('Review queued. Waiting for worker admission…', 'ok');
    $('empty').hidden = false;
    $('review').hidden = true;
    $('empty').replaceChildren(
      el('span', '◈', 'symbol'),
      el('h2', 'Review queued'),
      el('p', `Project ${job.project_id} · merge request !${job.iid}. Waiting for worker admission…`, 'muted'),
    );
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    // Keep the queued event address available even if admission is delayed.
    while (!signal.aborted) {
      const review = await (await request(job.poll, {signal})).json();
      if (review.id) { select(review.id); break; }
      await delay(2000, signal);
    }
  } catch (error) { if (error.name !== 'AbortError') notice(error.message, 'error'); }
  finally { $('start').disabled = false; }
};
