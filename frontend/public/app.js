import {identity, identityNode} from './identity.js';
import {matchesFinding, commentActions, bulkCommentKeys, verificationBadge} from './findings.js';
import {apiURL} from './paths.js';
import {events} from './sse.js';
import {FAILED, HALTED, TERMINAL, label as labels, walk, UnitProgress} from './flow.js';
import {chosenModels, modelFor, roleFor, roleLabel, rolesFor} from './models.js';
import {addAttempt, breakdown, cost as money, emptySpend, mergeSpend, tokens as count, usage} from './spend.js';
import {runControls} from './controls.js';
import {$, all, ago, confirmDialog, delay, el, notice, short, span} from './dom.js';
import {mountAccounts, readiness} from './accounts.js';
import {explain} from './reasons.js';
import {CheckLog, checkTone} from './checks.js';
import {RecheckProgress, describe as describeRecheck} from './recheck-progress.js';

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
const GROUPS = {llm_attempt: 'tool', run: 'state', models: 'state', check: 'tool'};
const KINDS = {llm_attempt: 'llm'};
const KEEP = 300;

let token = '', account = null, sessionGeneration = 0, sessionAbort = new AbortController(), selected = '', current = null, controller, refreshTimer, lastRecheck = null;
let pendingRecheck = null;      // {id, before, until}: a recheck this page asked for, not yet answered
let controlBusy = false;        // a stop, rerun or retry request is in flight
let reviews = [], listFilter = 'all', listQuery = '', activityKind = 'all', severity = 'all', tabTouched = false;
let snapshotSeq = 0;            // the newest event the last snapshot already reflects
const activities = new Map();   // activity_id -> {row, kind, name, status, depth, at}
const running = new Set();
const unitProgress = new UnitProgress();
const checkLog = new CheckLog();          // static checks, streamed then as recorded
const recheckProgress = new RecheckProgress();
const stages = new Map();       // agent name -> {status, at, took}
const stateAt = new Map();      // review state -> when the pipeline entered it
const earlyAt = new Map();      // review state -> when its work started ahead of the state
const nodes = new Map();        // review state -> the flow node's elements
let modelDefaults = {};         // role -> the model a run uses when none is chosen
let modelCatalog = [];          // model IDs this deployment allows an operator to pick
const modelChoice = new Map();  // role -> the model chosen for the next manual run
let reviewModels = {};          // role -> model, as resolved by the review being watched
let audited = null;             // the review's spend as the last snapshot reported it
let streamed = emptySpend();    // attempts seen since that snapshot, not yet audited

const group = kind => GROUPS[kind] || kind;
const shows = kind => activityKind === 'all' || activityKind === group(kind);

const tone = state => FAILED.has(state) ? 'bad' : HALTED.has(state) ? 'warn' : state === 'PUBLISHED' ? 'ok' : 'run';

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
  return `${identity(review)} ${review.project_id ?? ""} ${review.kind || 'code'} ${review.head_sha} ${labels(review.state)} ${labels(review.decision)}`
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
  const title = identityNode(review, document);
  title.className = 'grow';
  top.append(el('i', '', 'dot'), title);
  if (review.kind === 'document') top.append(el('span', 'document', 'badge tone-kind'));
  if (review.decision) top.append(decisionBadge(review.decision));
  const bottom = el('div', '', 'bottom');
  bottom.append(el('span', labels(review.state)),
    el('span', review.kind === 'document' ? `v${review.head_sha}` : short(review.head_sha), 'sha'));
  if (review.partial) bottom.append(el('span', 'partial', 'badge tone-warn'));
  if (review.controls?.retry?.allowed) bottom.append(el('span', 'can retry', 'badge tone-info'));
  const when = el('time', ago(review.started_at));
  when.dateTime = review.started_at ?? '';
  when.dataset.ago = review.started_at ?? '';
  bottom.append(when);
  if (account?.role === 'admin') bottom.append(el('span', review.owner_user_id || 'system / legacy'));
  if (review.spend) bottom.append(el('span', `${money(review.spend.cost, {unpriced: review.spend.unpriced_calls})} · ${count(review.spend.tokens)} tokens`));
  button.append(top, bottom);
  // A failed or stopped run says why without opening it.
  if (review.error) button.title = review.error;
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
  renderReviewLinks();
  const overrides = review.overrides || {};
  const isDocument = review.kind === 'document';
  $('identity').replaceChildren(...(isDocument ? ['Document review · '] : []), identityNode(review, document));
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

  renderReasons(review);

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

// Why a review failed, stopped or ran partial, as the API worded it.
function renderReasons(review) {
  const view = explain(review);
  $('alert').hidden = !view;
  if (!view) return;
  $('alert').className = `alert${view.tone === 'bad' ? '' : ` ${view.tone}`}`;
  const list = el('ul');
  for (const item of view.items) list.append(el('li', item.message));
  $('alert').replaceChildren(el('strong', view.title), ...(view.items.length ? [list] : []));
  if (review.partial && review.state === 'PUBLISHED') {
    $('alert').append(el('p', review.kind === 'document'
      ? 'Not every section was examined, so the outcome falls back to comment only.'
      : 'The decision falls back to comment only and the commit status passes.', 'consequence'));
  }
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
  const cell = text => {
    const box = el('div', '', 'node');
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
  // The flow's own shape, read from an unstarted review: labels only.
  steps.forEach((step, index) => {
    if (index) flow.append(el('i', '', 'link'));
    const node = cell(step.label);
    node.dataset.node = step.state;
    flow.append(node);
    nodes.set(step.state, node);
  });
}

function paint(target, step) {
  if (!target) return;
  target.className = `node ${step.status}`;
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
  const flow = walk(state, history, stages, stateAt, earlyAt);
  if (flow.steps.length !== nodes.size || flow.steps.some(step => !nodes.has(step.state))) buildFlow(flow.steps);
  $('track-fill').style.setProperty('--progress', flow.percent);
  $('track').className = `track${flow.terminal ? '' : ' running'}${flow.tone ? ` ${flow.tone}` : ''}`;
  for (const step of flow.steps) {
    paint(nodes.get(step.state), step);
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
  const normal = shown.filter(f => f.verdict !== 'rejected');
  const rejected = shown.filter(f => f.verdict === 'rejected');
  const previous = $('findings').querySelector('.rejected-findings');
  const keepOpen = previous?.open && previous.dataset.reviewId === String(current?.id);
  $('findings').replaceChildren();
  if (!shown.length) {
    $('findings').append(el('p', findings.length ? 'No findings match these filters.' : 'No stored findings to show.', 'empty-line'));
    return;
  }
  for (const finding of normal) $('findings').append(findingCard(finding));
  if (rejected.length) {
    const section = el('details', '', 'rejected-findings');
    section.dataset.reviewId = String(current?.id);
    section.open = Boolean(keepOpen);
    section.append(el('summary', `Rejected findings (${rejected.length})`));
    for (const finding of rejected) section.append(findingCard(finding));
    $('findings').append(section);
  }
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
  const verification = verificationBadge(finding);
  const badge = el('span', verification.text, `badge ${verification.tone}`.trim());
  if (verification.title) badge.title = verification.title;
  top.append(badge);
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

// Report content stays text; only locally created formatting nodes become HTML.
function markdownPreview(source) {
  const root = el('div');
  function inline(parent, text) {
    const pattern = /(`[^`]+`|\*\*[^*]+\*\*|_[^_]+_|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g;
    let start = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(start, match.index)));
      const token = match[0];
      if (token.startsWith('[')) {
        const [, label, url] = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const link = el('a', label); link.href = url; link.rel = 'noreferrer noopener'; parent.append(link);
      } else {
        const width = token.startsWith('**') ? 2 : 1;
        parent.append(el(token.startsWith('`') ? 'code' : width === 2 ? 'strong' : 'em', token.slice(width, -width)));
      }
      start = match.index + token.length;
    }
    parent.append(document.createTextNode(text.slice(start)));
  }
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const cells = line => line.trim().replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
  let list = null, paragraph = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) { list = paragraph = null; continue; }
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fence) {
      const code = [];
      while (++i < lines.length && !new RegExp(`^\\s*${fence[1][0]}{${fence[1].length},}\\s*$`).test(lines[i])) code.push(lines[i]);
      const pre = el('pre'); pre.append(el('code', code.join('\n'))); root.append(pre);
    } else if (line.includes('|') && i + 1 < lines.length && cells(lines[i + 1]).every(cell => /^:?-{3,}:?$/.test(cell))) {
      const table = el('table'), head = el('thead'), body = el('tbody');
      const row = (text, tag) => { const tr = el('tr'); for (const cell of cells(text)) { const td = el(tag); inline(td, cell); tr.append(td); } return tr; };
      head.append(row(line, 'th')); i++;
      while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim()) body.append(row(lines[++i], 'td'));
      table.append(head, body); root.append(table);
    } else if (/^#{1,6}\s/.test(line)) {
      const [, hashes, text] = line.match(/^(#{1,6})\s+(.*)$/);
      const heading = el(`h${hashes.length}`); inline(heading, text); root.append(heading);
    } else if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) root.append(el('hr'));
    else if (/^\s*(?:[-+*]|\d+\.)\s+/.test(line)) {
      const [, marker, text] = line.match(/^\s*([-+*]|\d+\.)\s+(.*)$/);
      const tag = /\d/.test(marker) ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); root.append(list); }
      const item = el('li'); inline(item, text); list.append(item); paragraph = null; continue;
    } else if (/^>\s?/.test(line)) {
      const quote = el('blockquote'); inline(quote, line.replace(/^>\s?/, '')); root.append(quote);
    } else {
      if (!paragraph) { paragraph = el('p'); root.append(paragraph); }
      else paragraph.append(document.createTextNode('\n'));
      inline(paragraph, line); list = null; continue;
    }
    list = paragraph = null;
  }
  return root;
}

let reportMode = 'view';
function setReportMode(mode) {
  reportMode = mode;
  const available = !!$('report').textContent;
  $('report').hidden = !available || mode !== 'text';
  $('report-preview').hidden = !available || mode !== 'view';
  for (const button of all('[data-report-mode]')) {
    button.setAttribute('aria-pressed', String(button.dataset.reportMode === mode));
    button.disabled = !available;
  }
}
for (const button of all('[data-report-mode]')) button.onclick = () => setReportMode(button.dataset.reportMode);

function renderReport(report) {
  $('report').textContent = report || '';
  $('report-preview').replaceChildren(markdownPreview(report || ''));
  setReportMode(reportMode);
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
  if (status === 'completed' || status === 'passed') return 'tone-good';
  if (['partial', 'no completion', 'errored', 'skipped', 'queued'].includes(status)) return 'tone-warn';
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
  const counts = {all: 0, state: 0, agent: 0, unit: 0, tool: 0, recheck: 0};
  for (const row of $('activity').children) {
    counts.all++;
    const key = group(row.dataset.kind);
    if (key in counts) counts[key]++;
  }
  for (const node of all('#activity-filters .n')) node.textContent = String(counts[node.dataset.tally] ?? 0);
}

function addActivity(item) {
  if (checkLog.accept(item)) renderChecks();
  if (recheckProgress.accept(item)) renderRecheckProgress();
  // A check streams its output under one row; while it runs it reads as started.
  if (item.kind === 'check' && item.data?.status === 'running') item = {...item, data: {...item.data, status: 'started'}};
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
  // Verifiers start per file while the Defect review step is still running.
  if (kind === 'tool' && data.name === 'Independent verifier' && data.status === 'started'
    && !earlyAt.has('FINDING_VERIFICATION')) {
    earlyAt.set('FINDING_VERIFICATION', Date.parse(item.at));
    renderPipeline();
  }
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
// Rows for activities still in flight are kept whatever their age: work units
// and verifiers run in parallel, so the oldest rows are the ones still waiting
// on a completion that has to land on the row its start opened.
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

/* ---------- static checks ---------- */

const checkOpen = new Map();    // check key -> whether the reader opened or closed it
function renderChecks() {
  const list = checkLog.list, counts = checkLog.counts;
  $('tabn-checks').textContent = String(list.length);
  $('tabn-checks').hidden = !list.length;
  $('checks-summary').textContent = [
    counts.running && `${counts.running} running`, counts.failed && `${counts.failed} failed`,
    counts.passed && `${counts.passed} passed`, counts.other && `${counts.other} other`,
  ].filter(Boolean).join(' · ') || '0';
  $('checks-empty').hidden = list.length > 0;
  $('checks').replaceChildren(...list.map((check, index) => checkCard(check, check.id || `${index}:${check.name}`)));
}

function checkCard(check, key) {
  const card = el('details', '', 'check-run');
  card.dataset.status = check.status || '';
  // Running and failed checks open by default; the reader's own toggle wins.
  card.open = checkOpen.get(key) ?? ['running', 'failed'].includes(check.status);
  card.ontoggle = () => checkOpen.set(key, card.open);
  const summary = el('summary');
  summary.append(el('span', check.name || 'check', 'name'));
  if (check.required) summary.append(el('span', 'required', 'badge'));
  const meta = [
    check.exit_code !== null && check.exit_code !== undefined ? `exit ${check.exit_code}` : '',
    Number.isFinite(check.duration_ms) ? span(check.duration_ms) : '',
  ].filter(Boolean).join(' · ');
  if (meta) summary.append(el('span', meta, 'meta'));
  summary.append(check.status === 'running' ? el('i', '', 'spinner')
    : el('span', check.status || 'unknown', `badge ${checkTone(check.status)}`));
  card.append(summary);
  const lines = check.lines || [];
  if (!lines.length) {
    card.append(el('p', check.status === 'running' ? 'Waiting for output…' : 'No output.', 'empty-line'));
    return card;
  }
  const output = el('pre', (check.truncated ? `… ${check.truncated} line(s) not shown\n` : '') + lines.join('\n'));
  card.append(output);
  // Follow a running check's newest line, as a terminal would.
  if (check.status === 'running') requestAnimationFrame(() => { output.scrollTop = output.scrollHeight; });
  return card;
}

/* ---------- recheck progress ---------- */

function renderRecheckProgress() {
  const view = recheckProgress.view;
  $('recheck-progress').hidden = !view?.active;
  if (!view?.active) return;
  $('recheck-progress-bar').max = Math.max(view.total, 1);
  $('recheck-progress-bar').value = view.done;
  $('recheck-progress-text').textContent = describeRecheck(view, verdict => VERDICTS[verdict] || labels(verdict));
  $('tabn-recheck').hidden = false;
  $('tabn-recheck').textContent = `${view.done}/${view.total}`;
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

function render(review) {
  for (const id of ['recheck', 'recheck-tab-action', 'recheck-report-action']) $(id).hidden = !review.capabilities?.execute;
  renderRunControls(review);
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
  renderRecheckProgress();
  settleRecheck(review);
  checkLog.load(review.checks);
  renderChecks();
}

const RECHECK_BUTTONS = ['recheck', 'recheck-tab-action', 'recheck-report-action'];

// A recheck this page asked for is answered once the review's recorded recheck
// changes; the stream that carried it is still open, so its progress was live.
function settleRecheck(review) {
  const waiting = pendingRecheck?.id === review.id;
  for (const button of RECHECK_BUTTONS) $(button).disabled = waiting;
  if (waiting && lastRecheck === pendingRecheck.before && !recheckProgress.view?.active) {
    $('recheck-meta').textContent = 'Recheck queued — waiting for a worker…';
  }
  if (!waiting || lastRecheck === pendingRecheck.before) return;
  pendingRecheck = null;
  for (const button of RECHECK_BUTTONS) $(button).disabled = false;
  const reason = review.recheck?.reason;
  notice(reason ? `Recheck: ${reason.replaceAll('_', ' ')}.` : 'Recheck answered the open threads.', reason === 'failed' ? 'error' : 'ok');
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
          // A recheck still unanswered: its queue outlasted the stream's hold.
          // Reconnect with the cursor until it answers or the page stops waiting.
          if (pendingRecheck?.id === id) {
            if (Date.now() < pendingRecheck.until) break;
            pendingRecheck = null;
            for (const button of RECHECK_BUTTONS) $(button).disabled = false;
            notice('No recheck answers recorded yet. The review may have no open reviewer threads, or recheck may be off for this project.');
          }
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
  checkLog.clear();
  recheckProgress.clear();
  renderChecks();
  renderRecheckProgress();
  stages.clear();
  stateAt.clear();
  earlyAt.clear();
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
    startSession(result);
  } catch (error) {
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
  const id = selected;
  for (const button of RECHECK_BUTTONS) $(button).disabled = true;
  $('recheck-meta').textContent = 'Recheck queued…';
  showTab('recheck');
  try {
    await request(`/admin/reviews/${encodeURIComponent(id)}/recheck`, {method: 'POST'});
    notice('Recheck queued. Its progress appears here as each thread is judged.');
    if (selected !== id) return;
    // Reconnect: the stream holds open while the recheck is queued and running,
    // so its activity arrives live and the closing snapshot carries the answers.
    pendingRecheck = {id, before: lastRecheck, until: Date.now() + 5 * 60_000};
    select(id);
    tabTouched = true;
    showTab('recheck');
  } catch (error) {
    if (error.name !== 'AbortError') notice(error.message, 'error');
    for (const button of RECHECK_BUTTONS) $(button).disabled = false;
  }
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
    const queued = el('p');
    if (kind === 'document') queued.textContent = `${job.subject?.title || 'Page'} (version ${job.subject?.version ?? '?'}). Waiting for worker admission…`;
    else queued.append(identityNode({...job, mr_iid: job.iid}, document), '. Waiting for worker admission…');
    $('empty').replaceChildren(
      el('span', '◈', 'symbol'),
      el('h2', 'Review queued'),
      queued,
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


function startSession(result) {
  token = result.csrf_token; account = result.account;
  applyAccount();
  $('token').value = '';
  $('connect').hidden = true;
  $('workspace').hidden = false;
  $('disconnect').hidden = false;
  // A data-loading failure does not invalidate a successfully restored session.
  refresh().catch(error => {
    if (error.name !== 'AbortError' && error.status !== 401) {
      connection('bad', 'API unreachable'); notice(error.message, 'error');
    }
  });
  loadModels().catch(error => {
    if (error.name === 'AbortError' || error.status === 401) return;
    $('models-panel').hidden = true;
    notice('Model selection is unavailable; runs will use the configured defaults.');
  });
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refresh().catch(() => connection('bad', 'API unreachable')), 15000);
}

async function restoreSession() {
  // Wait for the cookie check before allowing a competing login request.
  $('connect').hidden = true;
  try {
    startSession(await (await request('/auth/me')).json());
  } catch (error) {
    if (error.name === 'AbortError') return;
    $('connect').hidden = false;
    if (error.status !== 401) notice('Could not restore your session. Refresh to retry.', 'error');
  }
}


function clearSession() {
  sessionGeneration++; sessionAbort.abort(); sessionAbort = new AbortController();
  controller?.abort(); clearInterval(refreshTimer); token = ''; account = null;
  commentState = {comments: [], can_manage: false}; commentResults = []; commentsLoadedFor = ''; commentsLoadingFor = '';
  selected = ''; current = null; reviews = []; activities.clear(); unitProgress.clear(); stages.clear(); stateAt.clear(); earlyAt.clear();
  modelChoice.clear(); reviewModels = {}; modelDefaults = {}; modelCatalog = []; audited = null; streamed = emptySpend();
  $('workspace').hidden = true; $('connect').hidden = false; $('disconnect').hidden = true;
  $('account-identity').hidden = true; $('account-content').replaceChildren(); $('review').hidden = true;
  $('activity').replaceChildren(); renderReport(''); $('token').value = '';
}

function applyAccount() {
  const admin = account.role === 'admin';
  $('account-identity').textContent = `${account.display_name} · ${account.role}`;
  $('account-identity').hidden = false; $('nav-reviews').textContent = admin ? 'All reviews' : 'My reviews';
  $('nav-profile').textContent = admin ? 'Account' : 'Profile';
  $('owner-filter-label').hidden = !admin;
  $('nav-users').hidden = !admin; $('nav-new').hidden = admin; $('new-review-card').hidden = admin;
  if (!admin) readiness(request);
}

$('nav-reviews').onclick = () => { $('account-panel').hidden = true; $('review-layout').hidden = false; };
$('nav-new').onclick = () => { $('nav-reviews').click(); $('mr').focus(); };

/* ---------- stop, rerun and retry ---------- */

const CONTROL_HINTS = {
  stop: 'Cancel this review in flight; its commit status passes and posted comments stay',
  rerun: 'Start a fresh review at the current head, with the context this one was given',
  retry: 'Run this failed review again on the same head, with the same context and options',
};

function renderRunControls(review) {
  const view = runControls(review);
  for (const mode of ['stop', 'rerun', 'retry']) {
    const button = $(mode), control = view[mode];
    button.hidden = !control.show;
    button.disabled = controlBusy || !control.enabled;
    button.title = control.reason || CONTROL_HINTS[mode];
  }
  // Why a shown control is refused is worth reading without hovering.
  const refused = ['stop', 'retry'].map(mode => view[mode]).find(c => c.show && !c.enabled && c.reason);
  $('run-hint').hidden = !refused;
  $('run-hint').textContent = refused?.reason || '';
}

// A request the API refused means the controls moved on (the review finished,
// a newer run, a new head). A finished review's stream is closed, so read
// them back directly.
async function reloadControls(id) {
  const review = await (await request(`/admin/reviews/${encodeURIComponent(id)}`)).json();
  if (id !== selected || !current) return;
  current = {...current, controls: review.controls};
  refresh().catch(() => {});
}

async function control(mode, work) {
  const view = runControls(current)[mode];
  if (!selected || !view.enabled || controlBusy) return;
  controlBusy = true; renderRunControls(current);
  try { await work(selected, view); }
  catch (error) {
    if (error.name !== 'AbortError') notice(error.message, 'error');
    if (error.status === 409) await reloadControls(selected).catch(() => {});
  } finally { controlBusy = false; if (current) renderRunControls(current); }
}

// The stream stays open: the review's cancellation, its worker winding down
// and the reason it stopped all arrive on it.
const stopReview = () => control('stop', async id => {
  if (!await confirmDialog('Stop this review?',
    'The run is cancelled within a few seconds and its commit status passes. Comments it already posted stay posted.', 'Stop review')) return;
  await request(`/admin/reviews/${encodeURIComponent(id)}/stop`, {method: 'POST'});
  notice('Review stopped. The worker is winding it down…');
});

// Rerun and retry ask the API for a new run, then follow it from admission.
const runAgain = mode => control(mode, async (id, view) => {
  if (mode === 'rerun' && view.confirm && !await confirmDialog('Rerun this review?',
    'It is still running. A rerun supersedes it and starts again at the current head.', 'Rerun')) return;
  const noun = mode === 'retry' ? 'Retry' : 'Rerun';
  const job = await (await request(`/admin/reviews/${encodeURIComponent(id)}/${mode}`, {method: 'POST'})).json();
  notice(`${noun} queued. Waiting for worker admission…`);
  const signal = controller.signal;
  while (!signal.aborted) {
    const review = await (await request(job.poll, {signal})).json();
    if (review.id) { select(review.id); refresh().catch(() => {}); break; }
    if (['CONFLICT', 'REJECTED'].includes(review.state)) throw new Error(`${noun} was not admitted. Check credentials and try again after any active review finishes.`);
    await delay(2000, signal);
  }
});

$('stop').onclick = stopReview;
$('rerun').onclick = () => runAgain('rerun');
$('retry').onclick = () => runAgain('retry');

mountAccounts({request, account: () => account, clearSession});
void restoreSession();
