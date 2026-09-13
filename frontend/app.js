import {matchesFinding, commentActions, bulkCommentKeys} from './findings.js';
import {apiURL} from './paths.js';
import {events} from './sse.js';
import {FAILED, HALTED, TERMINAL, label as labels, walk, UnitProgress} from './flow.js';
import {chosenModels, modelFor, roleFor, roleLabel, rolesFor} from './models.js';
import {addAttempt, breakdown, cost as money, emptySpend, mergeSpend, tokens as count, usage} from './spend.js';

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
let commentState = {comments: [], can_manage: false}, commentBusy = false, commentsLoadedFor = '', commentsLoadingFor = '';
let commentResults = [];
let chatState = {messages: [], can_ask: false, reason: ''}, chatLoadedFor = '', chatPolling = '';
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

let token = '', account = null, sessionGeneration = 0, sessionAbort = new AbortController(), selected = '', current = null, controller, refreshTimer, lastRecheck = null;
let reviews = [], listFilter = 'all', listQuery = '', activityKind = 'all', severity = 'all', tabTouched = false;
let snapshotSeq = 0;            // the newest event the last snapshot already reflects
const activities = new Map();   // activity_id -> {row, kind, name, status, depth, at}
const running = new Set();
const unitProgress = new UnitProgress();
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
  const generation = sessionGeneration;
  const response = await fetch(apiURL(path), {...options, credentials: 'same-origin', signal: options.signal ? AbortSignal.any([options.signal, sessionAbort.signal]) : sessionAbort.signal, headers: {'X-CSRF-Token': token, ...options.headers}});
  if (generation !== sessionGeneration) throw new DOMException('Session changed', 'AbortError');
  if (response.status === 401 && account) clearSession();
  if (!response.ok) {
    let detail; try { detail = (await response.json()).detail; } catch {}
    const error = new Error(typeof detail === 'string' ? detail : `Request failed (${response.status})`);
    error.status = response.status; throw error;
  }
  const readJSON = response.json.bind(response);
  response.json = async () => { const data = await readJSON(); if (generation !== sessionGeneration) throw new DOMException('Session changed', 'AbortError'); return data; };
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
  const filter = account?.role === 'admin' && $('owner-filter')?.value ? `?owner=${encodeURIComponent($('owner-filter').value)}` : '';
  const body = await (await request('/admin/reviews' + filter)).json();
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
  return `${identity(review)} ${review.kind || 'code'} ${review.head_sha} ${labels(review.state)} ${labels(review.decision)}`
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
  top.append(el('i', '', 'dot'), el('span', identity(review), 'grow'));
  if (review.kind === 'document') top.append(el('span', 'document', 'badge tone-kind'));
  if (review.decision) top.append(decisionBadge(review.decision));
  const bottom = el('div', '', 'bottom');
  bottom.append(el('span', labels(review.state)),
    el('span', review.kind === 'document' ? `v${review.head_sha}` : short(review.head_sha), 'sha'));
  if (review.partial) bottom.append(el('span', 'partial', 'badge tone-warn'));
  const when = el('time', ago(review.started_at));
  when.dateTime = review.started_at ?? '';
  when.dataset.ago = review.started_at ?? '';
  bottom.append(when);
  if (account?.role === 'admin') bottom.append(el('span', review.owner_user_id || 'system / legacy'));
  if (review.spend) bottom.append(el('span', `${money(review.spend.cost, {unpriced: review.spend.unpriced_calls})} · ${count(review.spend.tokens)} tokens`));
  button.append(top, bottom);
  button.onclick = () => select(review.id);
  return button;
}

// What a review is about, in one line: a merge request or a page.
function identity(review) {
  if (review.kind === 'document') {
    const subject = review.subject || {};
    return `${subject.space ? `${subject.space} · ` : ''}${subject.title || `Page ${subject.page_id ?? ''}`}`;
  }
  return `Project ${review.project_id} · !${review.mr_iid}`;
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
  renderReviewLinks();
  const overrides = review.overrides || {};
  const isDocument = review.kind === 'document';
  $('identity').textContent = isDocument
    ? `Document review · ${identity(review)}`
    : `Project ${review.project_id} · Merge request !${review.mr_iid}`;
  $('state').textContent = labels(review.state);
  $('metadata').textContent = [
    isDocument ? `page version ${review.head_sha}` : `head ${short(review.head_sha, 12)}`,
    account?.role === 'admin' ? `owner ${review.owner_user_id || 'system / legacy'}` : null,
    overrides.requested_by ? `triggered by ${overrides.requested_by}` : 'triggered by webhook',
    overrides.issue_key ? `story ${overrides.issue_key}` : null,
    overrides.epic_key ? `epic ${overrides.epic_key}` : null,
    (overrides.document_urls || []).length ? `${overrides.document_urls.length} supplied page(s)` : null,
    (overrides.supporting_urls || []).length ? `${overrides.supporting_urls.length} supporting page(s)` : null,
    overrides.check_space ? 'checked against the space' : null,
    overrides.instruction ? 'custom instruction' : null,
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
    $('alert').textContent = isDocument
      ? 'Partial review — not every section was examined, so the outcome falls back to comment only.'
      : 'Partial review — coverage is incomplete, so the decision falls back to comment only and the commit status passes.';
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
    isDocument ? metric('Page version', review.head_sha, {mono: true}) : metric('Head', short(review.head_sha, 12), {mono: true}),
    metric('Started', ago(review.started_at), {at: review.started_at ?? ''}),
    Number.isFinite(finished) && Number.isFinite(started)
      ? metric('Took', span(finished - started))
      : metric('Elapsed', Number.isFinite(started) ? span(Date.now() - started) : '—', {elapsed: true}),
    metric('Findings', String(findings.length)),
    metric('Blocker / required', String(high), {shade: high ? 'bad' : 'good'}),
    isDocument ? metric('Space', review.subject?.space || '—') : metric('Commit status', review.status_delivered ? 'delivered' : 'pending',
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

function buildFlow(steps = walk("INIT").steps) {
  nodes.clear();
  const flow = $('flow');
  flow.replaceChildren();
  const cell = (kind, text) => {
    const box = el(kind === 'lane' ? 'span' : 'div', '', kind);
    const top = el('span', '', 'n-top');
    top.append(el('i', '', 'n-dot'), el('span', text, 'n-label'), el('span', '', 'n-time'));
    const counts = el('span', '', 'unit-counts');
    for (const key of ['total', 'completed', 'running', 'idle']) {
      const counter = el('span', '', `unit-count unit-${key}`);
      counter.append(el('span', key), el('b', '—'));
      counter.dataset.count = key;
      counts.append(counter);
    }
    box.append(top, el('span', '', 'n-note'), counts, el('span', '', 'unit-stopped'));
    return box;
  };
  // The flow's own shape, read from an unstarted review: labels and lanes only.
  steps.forEach((step, index) => {
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
  const counts = unitProgress.get(step);
  for (const counter of target.querySelectorAll('[data-count]')) {
    const value = counts?.[counter.dataset.count];
    counter.querySelector('b').textContent = Number.isFinite(value) ? String(value) : '—';
  }
  target.querySelector('.unit-counts').title = counts
    ? 'Logical work units; retries do not add units. Idle units have not started.'
    : 'Unit counts have not been reported for this step.';
  target.querySelector('.unit-stopped').textContent = counts?.stopped
    ? `${counts.stopped} stopped / skipped` : '';
  // Colour alone never carries the status: the title always spells it out.
  target.title = `${step.label} · ${step.status}${step.note ? ` · ${step.note}` : ''}`;
}

function renderPipeline(state = current?.state ?? 'INIT', history = current?.history ?? []) {
  const flow = walk(state, history, stages, stateAt);
  if (flow.steps.length !== nodes.size || flow.steps.some(step => !nodes.has(step.state))) buildFlow(flow.steps);
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
  renderCommentControls();
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
  if (!finding.quote) {
    top.append(el('span', finding.introduced_by_this_change ? 'this change' : 'pre-existing',
      `badge ${finding.introduced_by_this_change ? 'tone-info' : ''}`.trim()));
  }
  if (finding.verdict) {
    top.append(el('span', finding.verdict, `badge ${finding.verdict === 'confirmed' ? 'tone-good' : 'tone-warn'}`));
  } else {
    top.append(el('span', 'unverified', 'badge tone-warn'));
  }
  article.append(top, el('h3', finding.claim || 'Untitled finding'));

  if (finding.quote) {
    // A document finding points at a passage, not a line.
    const anchor = el('div', '', 'anchor');
    anchor.append(el('div', finding.heading_path || '(intro)', 'section'));
    const quote = el('blockquote');
    quote.append(document.createTextNode(finding.quote));
    anchor.append(quote);
    article.append(anchor);
  } else if (finding.file) {
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

  const related = (finding.related || []).slice(0, 4);
  if (related.length) {
    const list = el('ul', '', 'related');
    for (const item of related) {
      const line = el('li');
      line.append(document.createTextNode(`${item.page_id && item.page_id !== finding.page_id ? `page ${item.page_id} · ` : ''}${item.heading_path ?? ''}: `));
      const quote = el('q'); quote.append(document.createTextNode(item.quote ?? ''));
      line.append(quote);
      if (item.note) line.append(document.createTextNode(` — ${item.note}`));
      list.append(line);
    }
    article.append(list);
  }
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
  article.append(commentPanel(commentState.comments.find(c => c.key === finding.fingerprint)));
  return article;
}

/* ---------- GitLab comment management ---------- */

function renderCommentControls() {
  renderReviewLinks();
  const manageable = current?.capabilities?.execute && commentState.can_manage && !commentBusy;
  $('resolve-all-comments').hidden = !current?.capabilities?.execute;
  $('publish-all-comments').hidden = !current?.capabilities?.execute;
  $('resolve-all-comments').disabled = !manageable || !bulkCommentKeys(commentState.comments, 'resolve_all').length;
  $('publish-all-comments').disabled = !manageable || !bulkCommentKeys(commentState.comments, 'publish_all').length;
  $('refresh-comments').disabled = commentBusy || !current || current.state !== 'PUBLISHED';
  $('comment-status').textContent = commentBusy ? 'Updating GitLab comments…' : commentState.error ||
    (commentState.synced ? 'Comment status synchronized with GitLab.' : 'Comment status has not been synchronized.');
  $('comment-results').replaceChildren();
  for (const result of commentResults) {
    const finding = current?.findings?.find(f => f.fingerprint === result.key);
    $('comment-results').append(el('p', `${result.key === 'summary' ? 'Overall review message' : finding?.claim || 'Comment'}: ${result.error || result.status}`, result.status === 'failed' ? 'tone-bad' : 'hint'));
  }
  $('overall-message').replaceChildren();
  const summary = commentState.comments.find(c => c.key === 'summary');
  const report = typeof current?.report === 'string' ? current.report : current?.report?.summary;
  if (summary || report) {
    const card = el('article', '', 'finding');
    card.append(el('h3', 'Overall review message'));
    card.append(commentPanel(summary || {key: 'summary', status: 'not_published', thread_status: 'not_applicable', message: report}));
    $('overall-message').append(card);
  }
}

function renderReviewLinks() {
  const container = $('review-links');
  container.replaceChildren();
  const seen = new Set();
  for (const link of [...(current?.links || []), ...(commentState.links || [])]) {
    let url;
    try { url = new URL(link.url); } catch { continue; }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || seen.has(url.href)) continue;
    seen.add(url.href);
    const anchor = el('a', link.label);
    anchor.href = url.href; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
    container.append(anchor);
  }
  container.hidden = !container.children.length;
}

function commentPanel(comment) {
  const panel = el('section', '', 'comment-panel');
  const status = el('div', '', 'chips');
  status.append(el('span', `Finding status: ${labels(comment?.status || 'not_published')}`, 'badge'));
  status.append(el('span', `Thread status: ${labels(comment?.thread_status || 'not_applicable')}`, 'badge'));
  panel.append(status);
  if (!comment) return panel;
  if (comment.conflict) panel.append(el('p', comment.conflict, 'tone-warn'));
  if (comment.intent === 'remove') panel.append(el('p', 'Removal pending. Retry Remove to finish.', 'tone-warn'));
  const message = el('pre', comment.message ?? comment.body ?? '', 'comment-message');
  const content = el('details', '', 'comment-content');
  content.append(el('summary', 'Message content'), message);
  panel.append(content);
  const actions = el('div', '', 'actions');
  for (const action of commentActions(comment, current?.capabilities?.execute && commentState.can_manage)) {
    const button = el('button', action === 'edit' ? 'Edit message' : action === 'resolve' ? 'Resolve thread' : comment.status === 'drafted' ? 'Remove draft' : 'Remove comment');
    button.type = 'button'; button.disabled = commentBusy;
    button.onclick = () => {
      if (action === 'edit') {
        content.open = true;
        const editor = el('textarea'); editor.value = comment.message ?? comment.body ?? '';
        editor.setAttribute('aria-label', 'Comment message'); editor.rows = 12;
        const save = el('button', 'Save'), cancel = el('button', 'Cancel');
        save.type = cancel.type = 'button';
        save.onclick = () => {
          if (!editor.value.trim()) { editor.focus(); return; }
          runCommentAction('edit', comment.key, editor.value, comment.revision);
        };
        cancel.onclick = () => { message.hidden = false; actions.hidden = false; form.remove(); };
        const form = el('div', '', 'comment-editor'); form.append(editor, save, cancel);
        message.hidden = true; actions.hidden = true; content.append(form); editor.focus();
      } else runCommentAction(action, comment.key);
    };
    actions.append(button);
  }
  panel.append(actions);
  if (comment.status === 'committed' && actions.children.length) panel.append(el('p', 'Removing this comment preserves replies from other people.', 'hint'));
  return panel;
}

async function refreshComments() {
  const id = selected;
  if (!id || commentBusy) return;
  commentsLoadingFor = id;
  try {
    const response = await request(`/admin/reviews/${encodeURIComponent(id)}/comments`);
    const state = await response.json();
    if (id !== selected) return;
    commentState = state; commentsLoadedFor = id; renderFindings();
  } catch (error) {
    if (id === selected) {
      commentState = {...commentState, can_manage: false, synced: false, error: error.message};
      commentsLoadedFor = id; renderFindings();
    }
  } finally { if (commentsLoadingFor === id) commentsLoadingFor = ''; }
}

async function runCommentAction(action, key, message, revision) {
  const id = selected;
  if (!id || commentBusy) return;
  commentBusy = true; commentResults = []; renderFindings();
  try {
    const state = await (await request(`/admin/reviews/${encodeURIComponent(id)}/comments`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, key, message, revision})
    })).json();
    if (id !== selected) return;
    commentState = state; commentResults = state.results || [];
    const failed = commentResults.filter(r => r.status === 'failed').length;
    notice(failed ? `${failed} comment action(s) failed. Details are above the findings.` : 'Comment changes saved.', failed ? 'error' : 'ok');
  } catch (error) {
    if (id === selected) { notice(error.message, 'error'); commentState = {...commentState, synced: false, can_manage: false, error: 'Action outcome could not be confirmed. Refresh comment status before retrying.'}; }
  } finally { commentBusy = false; renderFindings(); }
}

$('refresh-comments').onclick = refreshComments;
$('resolve-all-comments').onclick = () => runCommentAction('resolve_all');
$('publish-all-comments').onclick = () => runCommentAction('publish_all');

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
    recheck.reason ? `Completed: ${recheck.reason.replaceAll('_', ' ')}` : recheck.mode === 'draft' ? 'Drafted on GitLab' : 'Posted on the merge request',
    recheck.head_sha ? `head ${short(recheck.head_sha, 12)}` : '',
    recheck.at ? new Date(recheck.at).toLocaleString() : '',
  ].filter(Boolean).join(' · ');
  if (!answers.length) {
    $('recheck-results').append(el('p', recheck.reason ? `Recheck: ${recheck.reason.replaceAll('_', ' ')}.` : 'The recheck found no open reviewer threads to judge.', 'empty-line'));
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

/* ---------- chat ---------- */

async function loadChat() {
  const id = selected;
  if (!id) return;
  chatLoadedFor = id; // Snapshots arrive more than once; one load per review.
  try {
    const state = await (await request(`/admin/reviews/${encodeURIComponent(id)}/chat`)).json();
    if (selected !== id) return;
    chatState = state;
    renderChat();
    if (state.messages.some(m => m.status === 'pending')) pollChat(id);
  } catch (error) {
    if (error.name !== 'AbortError' && selected === id) { chatLoadedFor = ''; chatState = {messages: [], can_ask: false, reason: error.message}; renderChat(); }
  }
}

// A question is answered by the worker, and the review's event stream is
// already finished: read the answer back until it lands, like a recheck.
async function pollChat(id) {
  if (chatPolling === id) return;
  chatPolling = id;
  try {
    for (let attempt = 0; attempt < 120 && selected === id; attempt++) {
      await delay(2000, controller.signal);
      if (selected !== id) return;
      const state = await (await request(`/admin/reviews/${encodeURIComponent(id)}/chat`)).json();
      if (selected !== id) return;
      chatState = state;
      renderChat();
      audited = {...(audited || emptySpend()), chat: state.spend};
      renderSpend();
      if (!state.messages.some(m => m.status === 'pending')) return;
    }
    if (selected === id) $('chat-status').textContent = 'Still waiting for an answer. It will appear here when the worker settles it.';
  } catch (error) { if (error.name !== 'AbortError' && selected === id) notice(error.message, 'error'); }
  finally { if (chatPolling === id) chatPolling = ''; }
}

function citationChip(citation) {
  const chip = el('button', '', 'chip');
  chip.type = 'button';
  if (citation.kind === 'file') {
    const where = citation.line_start ? `:${citation.line_start}${citation.line_end && citation.line_end !== citation.line_start ? `-${citation.line_end}` : ''}` : '';
    chip.textContent = `${citation.ref}${where}`;
    chip.title = 'Copy the location';
    chip.onclick = () => copy(`${citation.ref}${where}`, 'the location');
  } else if (citation.kind === 'finding') {
    const finding = (current?.findings || []).find(f => f.id === citation.ref);
    chip.textContent = finding ? `finding: ${finding.claim || finding.id}`.slice(0, 80) : `finding ${citation.ref}`;
    chip.title = 'Show in Findings';
    chip.onclick = () => { tabTouched = true; severity = 'all'; impactLevel = 'all'; renderFindings(); showTab('findings'); };
  } else {
    chip.textContent = `${citation.kind}: ${citation.ref}`;
    chip.onclick = () => copy(citation.ref, `the ${citation.kind}`);
  }
  return chip;
}

function renderChat() {
  const messages = chatState.messages || [];
  const answered = messages.filter(m => m.status !== 'pending').length;
  $('tabn-chat').hidden = !messages.length;
  $('tabn-chat').textContent = String(messages.length);
  $('chat-count').textContent = String(messages.length);
  $('chat-empty').hidden = messages.length > 0;
  const pending = messages.some(m => m.status === 'pending');
  const spend = chatState.spend;
  $('chat-meta').textContent = [
    spend?.calls ? `${count(spend.tokens)} tokens · ${money(spend.cost, {unpriced: spend.unpriced_calls})} on the chat budget` : '',
    answered !== messages.length ? 'answering…' : '',
  ].filter(Boolean).join(' · ');
  const list = $('chat-messages');
  list.replaceChildren(...messages.flatMap(message => {
    const asked = el('li', '', 'asked');
    const who = el('div', '', 'who');
    who.append(el('span', message.user_id === account?.id ? 'You' : 'Owner', 'badge tone-info'), el('time', ago(message.created_at)));
    asked.append(who, el('div', message.question, 'text'));
    const reply = el('li', '', message.status);
    const replyWho = el('div', '', 'who');
    replyWho.append(el('span', message.status === 'pending' ? 'Answering…' : message.status === 'failed' ? 'Could not answer' : 'Answer', `badge ${message.status === 'failed' ? 'tone-bad' : message.status === 'pending' ? 'tone-warn' : 'tone-good'}`));
    if (message.model) replyWho.append(el('span', message.model, 'mono'));
    if (message.answered_at) replyWho.append(el('time', ago(message.answered_at)));
    reply.append(replyWho);
    if (message.status === 'answered') {
      reply.append(el('div', message.answer || '', 'text'));
      if (message.citations?.length) {
        const cites = el('div', '', 'cites');
        cites.append(...message.citations.map(citationChip));
        reply.append(cites);
      }
      if (message.context_used?.length) reply.append(el('div', `Read: ${message.context_used.join(', ')}`, 'reads'));
    } else if (message.status === 'failed') {
      reply.append(el('div', message.error || 'The question could not be answered.', 'text'));
    } else {
      reply.append(el('div', 'Reading the review record and asking the model…', 'text hint'));
    }
    return [asked, reply];
  }));
  $('chat-form').hidden = !chatState.can_ask && !(current?.capabilities?.chat);
  $('chat-send').disabled = !chatState.can_ask || pending;
  $('chat-question').disabled = !chatState.can_ask;
  $('chat-status').textContent = chatState.can_ask
    ? (pending ? 'Waiting for the current answer before the next question.' : '')
    : (chatState.reason || (current && !TERMINAL.has(current.state) ? 'Questions open once the review has finished.' : ''));
}

$('chat-form').onsubmit = async event => {
  event.preventDefault();
  const id = selected, question = $('chat-question').value.trim();
  if (!id || !question || !chatState.can_ask) return;
  $('chat-send').disabled = true;
  $('chat-status').textContent = 'Sending…';
  try {
    const result = await (await request(`/admin/reviews/${encodeURIComponent(id)}/chat`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question}),
    })).json();
    if (selected !== id) return;
    $('chat-question').value = '';
    chatState = {...chatState, messages: [...(chatState.messages || []), result.message]};
    renderChat();
    pollChat(id);
  } catch (error) {
    if (error.name !== 'AbortError') { notice(error.message, 'error'); $('chat-status').textContent = error.message; $('chat-send').disabled = false; }
  }
};

$('chat-question').onkeydown = event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('chat-form').requestSubmit(); }
};

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
  if (unitProgress.accept(item)) renderPipeline();
  if (kind === 'unit_progress') return;
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
  unitProgress.close();
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
  const chat = audited?.chat;
  $('spend-chat').hidden = !chat?.calls;
  if (chat?.calls) {
    $('spend-chat').textContent = `Chat — ${chat.calls} ${chat.calls === 1 ? 'call' : 'calls'} · ${count(chat.tokens_in)} in · ${count(chat.tokens_out)} out · ${money(chat.cost, {unpriced: chat.unpriced_calls})}. Billed to the chat budget, not the review's.`;
  }
  $('report-spend-meta').textContent = `${total} · ${spend.tokens.toLocaleString()} tokens · ${spend.calls} calls (including retries and rechecks)`
    + (spend.unpriced_calls ? ` · ${spend.unpriced_calls} calls with unknown cost` : '');
  for (const [dimension, target] of [['model', 'report-model-rows'], ['role', 'report-step-rows']]) {
    $(target).replaceChildren(...breakdown(spend, dimension).map(row => {
      const line = el('tr');
      line.append(el('td', dimension === 'role' ? roleLabel(row.label) : row.label),
        ...[row.calls, row.tokens_in, row.tokens_out, row.tokens].map(n => el('td', n.toLocaleString(), 'num')),
        el('td', money(row.cost, {unpriced: row.unpriced_calls}), 'num cost'));
      return line;
    }));
  }
  return spend;
}

/* ---------- model selection ---------- */

async function loadModels() {
  const body = await (await request('/admin/models')).json();
  modelDefaults = body.defaults || {};
  modelCatalog = body.catalog || [];
  renderModelFields();
}

const reviewKind = () => new FormData($('trigger')).get('kind') || 'code';

function renderModelFields() {
  const roles = rolesFor(reviewKind(), Object.keys(modelDefaults)).filter(role => modelDefaults[role]);
  $('models').replaceChildren(...roles.map(modelField));
  markModelChoices();
}

// The form switches between a merge request and a page: different fields,
// different roles, and the report modes are worded for the target.
function renderKind() {
  const kind = reviewKind();
  for (const chip of all('#kind .chip')) chip.classList.toggle('on', chip.querySelector('input').checked);
  $('code-fields').hidden = kind !== 'code';
  $('document-fields').hidden = kind !== 'document';
  $('mr').required = kind === 'code';
  $('document-url').required = kind === 'document';
  for (const option of all('#mode option')) option.textContent = option.dataset[kind] || option.textContent;
  renderModelFields();
}
for (const input of all('#kind input')) input.onchange = renderKind;

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
  for (const id of ['replay', 'recheck', 'recheck-tab-action', 'recheck-report-action']) $(id).hidden = !review.capabilities?.execute;
  // A page has no threads to recheck.
  const isDocument = review.kind === 'document';
  for (const id of ['recheck', 'recheck-tab-action', 'recheck-report-action']) if (isDocument) $(id).hidden = true;
  $('tab-recheck').hidden = isDocument;
  if (isDocument && $('tab-recheck').getAttribute('aria-selected') === 'true') $('tab-activity').click();

  current = review;
  if (review.state === 'PUBLISHED' && commentsLoadedFor !== selected && commentsLoadingFor !== selected) refreshComments();
  if (TERMINAL.has(review.state) && chatLoadedFor !== selected) loadChat();
  else if (!TERMINAL.has(review.state)) renderChat();
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
        if (event.event === 'session_expired') { clearSession(); return; }
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
  commentState = {comments: [], can_manage: false}; commentResults = []; commentsLoadedFor = ''; commentsLoadingFor = '';
  chatState = {messages: [], can_ask: false, reason: ''}; chatLoadedFor = ''; chatPolling = '';
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
  unitProgress.clear();
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
  try {
    const result = await (await request('/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({login:$('login-name').value, password:$('token').value})})).json();
    token = result.csrf_token; account = result.account;
    applyAccount();
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

$('disconnect').onclick = async () => { try { await request('/auth/logout', {method:'POST'}); } finally { clearSession(); } };
$('refresh').onclick = refreshNow;
$('owner-filter').onchange = refreshNow;
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
  for (const button of ['recheck', 'recheck-tab-action', 'recheck-report-action']) $(button).disabled = true;
  $('recheck-meta').textContent = 'Recheck queued…';
  showTab('recheck');
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
        select(id); // Reload the durable activity feed, including standalone recheck actions.
        render(review);
        tabTouched = true;
        showTab('recheck');
        notice(review.recheck?.reason ? `Recheck: ${review.recheck.reason.replaceAll('_', ' ')}.` : 'Recheck answered the open threads.', review.recheck?.reason === 'failed' ? 'error' : 'ok');
        return;
      }
    }
    notice('No recheck answers recorded yet. The review may have no open reviewer threads, or recheck may be off for this project.');
  } catch (error) { if (error.name !== 'AbortError') notice(error.message, 'error'); }
  finally { for (const button of ['recheck', 'recheck-tab-action', 'recheck-report-action']) $(button).disabled = false; }
};

$('recheck-tab-action').onclick = $('recheck-report-action').onclick = () => $('recheck').click();

$('trigger').onsubmit = async event => {
  event.preventDefault();
  $('start').disabled = true;
  const data = new FormData(event.target);
  const kind = data.get('kind') || 'code';
  const lines = name => (data.get(name) || '').split('\n').map(line => line.trim()).filter(Boolean);
  const body = kind === 'document'
    ? {
      document_url: data.get('document_url'),
      report_mode: data.get('report_mode'),
      supporting_urls: lines('supporting_urls'),
      check_space: data.get('check_space') === 'on',
    }
    : {
      merge_request_url: data.get('merge_request_url'),
      report_mode: data.get('report_mode'),
      document_urls: lines('documents'),
    };
  if (kind === 'document' && (data.get('instruction') || '').trim()) body.instruction = data.get('instruction').trim();
  if (kind === 'code') for (const key of ['issue_key', 'epic_key']) if (data.get(key).trim()) body[key] = data.get(key).trim();
  // Only the roles actually moved: every other role stays on project policy,
  // and a run that names nothing is exactly a webhook run.
  const chosen = chosenModels(modelChoice);
  const models = Object.fromEntries(rolesFor(kind, Object.keys(chosen)).map(role => [role, chosen[role]]));
  if (Object.keys(models).length) body.models = models;
  try {
    const endpoint = kind === 'document' ? '/admin/document-reviews' : '/admin/reviews';
    const job = await (await request(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json();
    notice('Review queued. Waiting for worker admission…', 'ok');
    $('empty').hidden = false;
    $('review').hidden = true;
    $('empty').replaceChildren(
      el('span', '◈', 'symbol'),
      el('h2', 'Review queued'),
      el('p', kind === 'document'
        ? `${job.subject?.title || 'Page'} (version ${job.subject?.version ?? '?'}). Waiting for worker admission…`
        : `Project ${job.project_id} · merge request !${job.iid}. Waiting for worker admission…`, 'muted'),
    );
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    // Keep the queued event address available even if admission is delayed.
    while (!signal.aborted) {
      const review = await (await request(job.poll, {signal})).json();
      if (review.id) { select(review.id); break; }
      if (['CONFLICT', 'REJECTED'].includes(review.state)) {
        const message = review.state === 'CONFLICT'
          ? `Another review is active for this ${kind === 'document' ? 'page' : 'merge request'}. Try again after it finishes.`
          : `The queued review is no longer eligible. Check your account, credentials and ${kind === 'document' ? 'page' : 'merge request'}.`;
        $('empty').replaceChildren(el('h2', 'Review not admitted'), el('p', message));
        throw new Error(message);
      }
      await delay(2000, signal);
    }
  } catch (error) { if (error.name !== 'AbortError') notice(error.message, 'error'); }
  finally { $('start').disabled = false; }
};


function clearSession() {
  sessionGeneration++; sessionAbort.abort(); sessionAbort = new AbortController();
  controller?.abort(); clearInterval(refreshTimer); token = ''; account = null;
  commentState = {comments: [], can_manage: false}; commentResults = []; commentsLoadedFor = ''; commentsLoadingFor = '';
  selected = ''; current = null; reviews = []; activities.clear(); unitProgress.clear(); stages.clear(); stateAt.clear();
  modelChoice.clear(); reviewModels = {}; modelDefaults = {}; modelCatalog = []; audited = null; streamed = emptySpend();
  $('workspace').hidden = true; $('connect').hidden = false; $('disconnect').hidden = true;
  $('account-identity').hidden = true; $('account-content').replaceChildren(); $('review').hidden = true;
  $('activity').replaceChildren(); $('report').textContent = ''; $('token').value = '';
}

function applyAccount() {
  const admin = account.role === 'admin';
  $('account-identity').textContent = `${account.display_name} · ${account.role}`;
  $('account-identity').hidden = false; $('nav-reviews').textContent = admin ? 'All reviews' : 'My reviews';
  $('nav-profile').textContent = admin ? 'Account' : 'Profile';
  $('owner-filter-label').hidden = !admin;
  $('nav-users').hidden = !admin; $('nav-new').hidden = admin; $('new-review-card').hidden = admin;
  if (!admin) readiness();
}

async function readiness() {
  try {
    const statuses = await (await request('/profile/integrations')).json();
    const missing = Object.entries(statuses).filter(([,v]) => v.status !== 'configured').map(([k]) => k);
    $('credential-readiness').textContent = missing.length ? `Missing or invalid: ${missing.join(', ')}. Gateway and GitLab are required; missing Jira or Confluence degrades requirements.` : 'Integration credentials are configured.';
  } catch (error) { notice(error.message, 'error'); }
}

function field(form, label, type='text') {
  const input = el('input'); input.type = type; input.required = true;
  const wrapper = el('label', label); wrapper.append(input); form.append(wrapper); return input;
}
function action(label, fn) {
  const button = el('button', label); button.type = 'button';
  button.onclick = async () => { button.disabled = true; try { await fn(); } catch (error) { notice(error.message, 'error'); } finally { button.disabled = false; } };
  return button;
}
function jsonRequest(path, method, body) { return request(path, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); }
function panel(title) { $('account-panel').hidden = false; $('review-layout').hidden = true; $('account-title').textContent = title; $('account-content').replaceChildren(); return $('account-content'); }
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  return parts.length ? (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase() : '?';
}
function statusBadge(status) {
  const tone = status === 'configured' ? 'tone-good' : status === 'invalid' ? 'tone-bad' : 'tone-warn';
  return el('span', status, `badge ${tone}`);
}
// Native <dialog> keeps this dependency-free; resolves false on Cancel, backdrop click cancel, or Escape.
function confirmDialog(title, message, confirmLabel) {
  return new Promise(resolve => {
    const dialog = document.createElement('dialog'); dialog.className = 'confirm-dialog';
    dialog.append(el('h3', title), el('p', message));
    const actions = el('div', null, 'form-actions');
    const cancel = el('button', 'Cancel'); cancel.type = 'button';
    const confirmButton = el('button', confirmLabel, 'danger'); confirmButton.type = 'button';
    actions.append(cancel, confirmButton);
    dialog.append(actions);
    document.body.append(dialog);
    let result = false;
    cancel.onclick = () => dialog.close();
    confirmButton.onclick = () => { result = true; dialog.close(); };
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); }, {once: true});
    dialog.showModal();
  });
}
$('nav-reviews').onclick = () => { $('account-panel').hidden = true; $('review-layout').hidden = false; };
$('nav-new').onclick = () => { $('nav-reviews').click(); $('mr').focus(); };
$('nav-profile').onclick = async () => {
  const admin = account.role === 'admin';
  const root = panel(admin ? 'Account' : 'Profile');
  root.append(el('p', `Signed in as ${account.display_name} (@${account.login}).`, 'account-lede'));

  const passwordCard = el('section', null, 'card');
  passwordCard.append(el('h3', 'Change password'));
  const password = el('form'); const grid = el('div', null, 'form-grid');
  const old = field(grid, 'Current password', 'password');
  const next = field(grid, 'New password', 'password'); next.minLength = 12;
  password.append(grid);
  const passwordActions = el('div', null, 'form-actions');
  const submit = el('button', 'Change password', 'primary'); submit.type = 'submit';
  passwordActions.append(submit, el('span', 'At least 12 characters. You will be signed out everywhere after this.', 'hint'));
  password.append(passwordActions);
  password.onsubmit = async event => { event.preventDefault(); try { await jsonRequest('/auth/password', 'POST', {current_password:old.value, password:next.value}); clearSession(); notice('Password changed. Sign in again.'); } catch (error) { notice(error.message, 'error'); } finally { old.value = ''; next.value = ''; } };
  passwordCard.append(password);
  root.append(passwordCard);
  if (admin) return;

  root.append(el('h3', 'Integrations'));
  root.append(el('p', 'Personal credentials used whenever you trigger a review. Gateway and GitLab are required for merge requests; Jira and Confluence unlock requirement linkage. Document reviews need Gateway and Confluence.', 'hint'));
  try {
    const statuses = await (await request('/profile/integrations')).json();
    for (const [name, state] of Object.entries(statuses)) {
      const card = el('section', null, 'card');
      const head = el('div', null, 'row');
      head.append(el('h3', name), statusBadge(state.status));
      card.append(head);
      const form = el('form'); const input = field(form, 'Replacement token', 'password'); input.autocomplete = 'off';
      const save = el('button', 'Save token', 'primary'); save.type = 'submit'; form.append(save);
      form.onsubmit = async event => { event.preventDefault(); const value = input.value; input.value = ''; try { await jsonRequest(`/profile/integrations/${name}`, 'PUT', {token:value}); notice('Token saved'); $('nav-profile').click(); readiness(); } catch (error) { notice(error.message, 'error'); } };
      card.append(form);
      const cardActions = el('div', null, 'user-actions');
      cardActions.append(action('Remove', async () => { await request(`/profile/integrations/${name}`, {method:'DELETE'}); $('nav-profile').click(); readiness(); }), action('Check connection', async () => { const result = await (await request(`/profile/integrations/${name}/check`, {method:'POST'})).json(); notice(result.reason || result.status); }));
      card.append(cardActions);
      root.append(card);
    }
  } catch (error) { notice(error.message, 'error'); }
};
$('activate').onsubmit = async event => { event.preventDefault(); try { await jsonRequest('/auth/activate', 'POST', {token:$('activation-token').value, password:$('activation-password').value}); notice('Password set. You can sign in.'); } catch (error) { notice(error.message, 'error'); } finally { $('activation-token').value = ''; $('activation-password').value = ''; } };
$('nav-users').onclick = async () => {
  const root = panel('Users');
  root.append(el('p', 'Create accounts and manage roles, access and removal for everyone on this deployment.', 'account-lede'));

  const createCard = el('section', null, 'card');
  createCard.append(el('h3', 'Create account'));
  const form = el('form'); const grid = el('div', null, 'form-grid');
  const login = field(grid, 'Login'); const name = field(grid, 'Display name');
  const roleLabel = el('label', 'Role'); const role = el('select');
  for (const value of ['user','admin']) { const option = el('option', value); option.value = value; role.append(option); }
  roleLabel.append(role); grid.append(roleLabel);
  form.append(grid);
  const createActions = el('div', null, 'form-actions');
  const submit = el('button', 'Create account', 'primary'); submit.type = 'submit';
  createActions.append(submit, el('span', "They'll get a one-time activation link to set their own password.", 'hint'));
  form.append(createActions);
  const delivery = el('div', null, 'token-callout'); delivery.hidden = true;
  createCard.append(form, delivery);
  root.append(createCard);
  form.onsubmit = async event => {
    event.preventDefault();
    try {
      const result = await (await jsonRequest('/auth/users','POST',{login:login.value,display_name:name.value,role:role.value})).json();
      delivery.replaceChildren(el('span', 'Deliver privately, expires in one hour:'), el('code', result.activation_token));
      const copy = el('button', 'Copy'); copy.type = 'button';
      copy.onclick = async () => { try { await navigator.clipboard.writeText(result.activation_token); notice('Copied to clipboard'); } catch { notice('Could not copy — select and copy the token manually', 'error'); } };
      delivery.append(copy);
      delivery.hidden = false;
      form.reset();
      await listUsers();
    } catch (error) { notice(error.message,'error'); }
  };

  const listCard = el('section', null, 'card');
  listCard.append(el('h3', 'Accounts'));
  const searchWrap = el('div', null, 'user-search');
  const search = el('input'); search.type = 'search'; search.placeholder = 'Search by login…'; search.setAttribute('aria-label','Search accounts');
  searchWrap.append(search);
  listCard.append(searchWrap);
  const showRemovedRow = el('label', null, 'checkbox-row');
  const showRemoved = el('input'); showRemoved.type = 'checkbox';
  showRemovedRow.append(showRemoved, el('span', 'Show removed accounts'));
  listCard.append(showRemovedRow);
  const list = el('div', null, 'user-list');
  listCard.append(list);
  root.append(listCard);

  function userRow(user) {
    const removed = Boolean(user.removed_at);
    const row = el('section', null, `card user-row${removed ? ' is-removed' : ''}`);
    row.append(el('div', initials(user.display_name), `avatar${user.role === 'admin' ? ' role-admin' : ''}`));
    const main = el('div', null, 'user-main');
    const heading = el('div', null, 'row');
    heading.append(el('h3', user.display_name), el('span', user.role, `badge${user.role === 'admin' ? ' tone-info' : ''}`));
    heading.append(removed ? el('span', 'removed', 'badge tone-bad') : el('span', user.active ? 'active' : 'disabled', `badge ${user.active ? 'tone-good' : 'tone-warn'}`));
    main.append(heading, el('p', `@${user.login}`, 'user-meta'));
    const actionsRow = el('div', null, 'user-actions');
    if (!removed) {
      for (const [label, body] of [['Toggle role',{action:'role',value:user.role === 'admin' ? 'user':'admin'}], [user.active ? 'Disable':'Reactivate',{action:'active',value:!user.active}], ['Revoke sessions',{action:'revoke_sessions'}], ['Start recovery (revokes integrations)',{action:'recovery'}]]) actionsRow.append(action(label, async () => { const result = await (await jsonRequest(`/auth/users/${user.id}`,'POST',body)).json(); if (result.activation_token) notice(`Deliver privately: ${result.activation_token}`); await listUsers(); }));
      const remove = action('Delete user', async () => {
        const ok = await confirmDialog('Delete this account?', `${user.display_name} (@${user.login}) will be signed out everywhere, permanently lose their saved integration credentials, and won't be able to sign back in. This can't be undone.`, 'Delete user');
        if (!ok) return;
        await jsonRequest(`/auth/users/${user.id}`, 'POST', {action:'remove'});
        notice('Account removed');
        await listUsers();
      });
      remove.className = 'danger';
      if (user.id === account.id) { remove.disabled = true; remove.title = "You can't delete the account you're signed in as."; }
      actionsRow.append(remove);
    }
    main.append(actionsRow);
    row.append(main);
    return row;
  }

  async function listUsers() {
    try {
      const result = await (await request(`/auth/users?search=${encodeURIComponent(search.value)}`)).json();
      const users = result.users.filter(user => showRemoved.checked || !user.removed_at);
      list.replaceChildren();
      if (!users.length) { list.append(el('p', result.users.length ? 'No matching accounts.' : 'No accounts yet.', 'empty-line')); return; }
      for (const user of users) list.append(userRow(user));
    } catch (error) { notice(error.message,'error'); }
  }
  search.oninput = listUsers; showRemoved.onchange = listUsers; await listUsers();
};

$('replay').onclick = async () => {
  if (!selected || !current?.capabilities?.execute) return;
  $('replay').disabled = true;
  try {
    const job = await (await request(`/admin/reviews/${encodeURIComponent(selected)}/replay`, {method:'POST'})).json();
    notice('Replay queued. Waiting for worker admission…');
    const signal = controller.signal;
    while (!signal.aborted) {
      const review = await (await request(`/admin/reviews?event_id=${encodeURIComponent(job.event_id)}`, {signal})).json();
      if (review.id) { select(review.id); break; }
      if (['CONFLICT','REJECTED'].includes(review.state)) throw new Error('Replay was not admitted. Check credentials and retry after any active review finishes.');
      await delay(2000, signal);
    }
  } catch (error) { if (error.name !== 'AbortError') notice(error.message,'error'); }
  finally { $('replay').disabled = false; }
};
