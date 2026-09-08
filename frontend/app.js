import {events} from './sse.js';

const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const all = selector => Array.from(document.querySelectorAll(selector));

// The pipeline the orchestrator walks, and the fan-out stages it reports as agents.
const PIPELINE = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'PURPOSE_REVIEW', 'DESIGN_REVIEW',
  'ANALYSIS_FAN_OUT', 'SYSTEM_CONTEXT_REVIEW', 'EVIDENCE_VALIDATION', 'FINDING_VERIFICATION',
  'FINALIZATION', 'DECISION', 'PUBLISHED'];
const STAGES = ['purpose', 'design', 'correctness', 'complexity', 'tests_', 'line_review', 'system_context'];
const FAILED = new Set(['FAILED_CONTEXT', 'FAILED_INTERNAL']);
const HALTED = new Set(['TERMINATED_EARLY', 'CANCELLED', 'SUPERSEDED']);
const TERMINAL = new Set([...FAILED, ...HALTED, 'PUBLISHED']);
const SEVERITIES = ['BLOCKER', 'REQUIRED', 'SUGGESTION', 'QUESTION', 'NIT', 'FYI', 'PRAISE'];
const DECISIONS = {APPROVE: 'Approve', REQUEST_CHANGES: 'Request changes', COMMENT_ONLY: 'Comment only'};
const VERDICTS = {fixed: 'Fixed', partially_fixed: 'Partially fixed', not_fixed: 'Still open',
  obsolete: 'No longer applies', unverifiable: 'Could not verify'};
const CONFIDENCE = {high: 1, medium: 0.6, low: 0.3};
const THEME_KEY = 'review-room-theme';

let token = '', selected = '', current = null, controller, refreshTimer, lastRecheck = null;
let reviews = [], listFilter = 'all', listQuery = '', activityKind = 'all', severity = 'all', tabTouched = false;
const activities = new Map();   // activity_id -> {row, status, depth, at}
const running = new Set();

const labels = value => String(value ?? '').replaceAll('_', ' ').trim().toLowerCase();
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

function metric(term, value, {shade = '', mono = false, at = '', elapsed = false} = {}) {
  const box = el('div', '', `metric ${shade}`.trim());
  const detail = el('dd', value, mono ? 'mono' : '');
  if (at) detail.dataset.ago = at;
  if (elapsed) detail.dataset.elapsed = '';
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
    metric('High severity', String(high), {shade: high ? 'bad' : 'good'}),
    metric('Commit status', review.status_delivered ? 'delivered' : 'pending',
      {shade: !review.status_delivered && TERMINAL.has(review.state) ? 'warn' : ''}),
    metric('Report mode', overrides.report_mode || 'project default'),
  );

  const history = (review.history || []).slice(-14);
  $('trail').replaceChildren();
  history.forEach((state, index) => {
    if (index) $('trail').append(el('i', '›'));
    $('trail').append(el('span', labels(state), index === history.length - 1 ? 'now' : ''));
  });

  renderProgress(review.state);
}

function renderProgress(state) {
  const index = PIPELINE.indexOf(state);
  const done = TERMINAL.has(state) || index < 0;
  const percent = done ? 100 : Math.round((index / (PIPELINE.length - 1)) * 100);
  $('track-fill').style.setProperty('--progress', percent);
  $('track').classList.toggle('running', !TERMINAL.has(state));
}

function resetStages() {
  $('stages').replaceChildren(...STAGES.map(name => {
    const node = el('span', labels(name), 'stage');
    node.dataset.stage = name;
    return node;
  }));
}

function markStage(data) {
  const node = Array.from($('stages').children).find(child => child.dataset.stage === data.name);
  if (!node) return;
  node.className = `stage ${data.status === 'started' ? 'active' : data.status === 'completed' ? 'done' : 'warning'}`;
  node.textContent = data.status === 'started' ? labels(data.name) : `${labels(data.name)} · ${data.status}`;
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

  const shown = (severity === 'all' ? findings : findings.filter(f => f.severity === severity))
    .slice().sort((a, b) => SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity));
  $('findings').replaceChildren();
  if (!shown.length) {
    $('findings').append(el('p', findings.length ? 'No finding at this severity.' : 'No stored findings to show.', 'empty-line'));
    return;
  }
  for (const finding of shown) $('findings').append(findingCard(finding));
}

function findingCard(finding) {
  const article = el('article', '', `finding sev-${finding.severity || 'NONE'}`);
  const top = el('div', '', 'top');
  top.append(el('span', labels(finding.severity) || 'finding', `badge ${severityTone(finding.severity)}`));
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

function fillRight(row, status, took) {
  const right = row.lastChild;
  right.replaceChildren();
  if (took) right.append(el('span', took, 'took'));
  if (status === 'started') right.append(el('i', '', 'spinner'));
  else right.append(el('span', status || 'transition', `badge ${statusTone(status)}`));
}

function activityRow(kind, name, parent, at, status, depth) {
  const row = el('li', '', `k-${kind}${status === 'started' ? ' running' : ''}`);
  row.style.setProperty('--depth', depth);
  row.dataset.kind = kind;
  row.hidden = activityKind !== 'all' && activityKind !== kind;
  const time = el('time', new Date(at).toLocaleTimeString());
  time.dateTime = at ?? '';
  const label = el('span', '', 'label');
  label.append(el('span', kind, 'kind'), el('span', name, 'name'));
  if (parent) label.append(el('span', `· ${parent}`, 'parent'));
  row.append(time, label, el('span', '', 'right'));
  fillRight(row, status, null);
  return row;
}

function countRunning() {
  $('running').hidden = !running.size;
  $('running').textContent = `${running.size} running`;
}

function addActivity(item) {
  const {data = {}, kind} = item;
  if (kind === 'state') {
    if (current) { current.state = data.state; current.history = [...(current.history || []), data.state]; }
    $('state').textContent = labels(data.state);
    renderProgress(data.state);
    const trail = $('trail');
    if (trail.lastChild) trail.lastChild.className = '';
    trail.append(el('i', '›'), el('span', labels(data.state), 'now'));
  }
  if (kind === 'agent') markStage(data);

  const known = data.activity_id ? activities.get(data.activity_id) : null;
  if (known) {
    // One row per activity: the completion lands on the row its start opened.
    known.status = data.status;
    known.row.classList.toggle('running', data.status === 'started');
    fillRight(known.row, data.status, span(Date.parse(item.at) - known.at));
    running.delete(data.activity_id);
    countRunning();
    return;
  }

  const ancestor = data.parent_id ? activities.get(data.parent_id) : null;
  const depth = kind === 'state' ? 0 : Math.min((ancestor ? ancestor.depth + 1 : 0), 4);
  const name = kind === 'state' ? labels(data.state)
    : kind === 'agent' ? labels(data.name) : String(data.name ?? '');
  const row = activityRow(kind, name, ancestor?.name, item.at, data.status, depth);
  if (data.activity_id) {
    row.dataset.activity = data.activity_id;
    activities.set(data.activity_id, {row, name, depth, status: data.status, at: Date.parse(item.at)});
    if (data.status === 'started') running.add(data.activity_id); else running.delete(data.activity_id);
    countRunning();
  }
  $('activity').prepend(row);
  $('activity-empty').hidden = true;
  // The feed is bounded; every retained event stays available through the API.
  while ($('activity').children.length > 300) {
    const dropped = $('activity').lastChild;
    if (dropped.dataset.activity) activities.delete(dropped.dataset.activity);
    dropped.remove();
  }
  $('tabn-activity').textContent = String($('activity').children.length);
}

function filterActivity() {
  let visible = 0;
  for (const row of $('activity').children) {
    row.hidden = activityKind !== 'all' && activityKind !== row.dataset.kind;
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
  for (const node of $('stages').children) {
    if (!node.classList.contains('done') && !node.classList.contains('warning')) {
      node.className = 'stage';
      node.textContent = `${labels(node.dataset.stage)} · not reported`;
    }
  }
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
  tabTouched = false;
  severity = 'all';
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
  renderProgress('INIT');
  resetStages();
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
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => refresh().catch(() => connection('bad', 'API unreachable')), 15000);
  } catch (error) {
    token = '';
    $('conn').hidden = true;
    notice(error.message, 'error');
  }
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
