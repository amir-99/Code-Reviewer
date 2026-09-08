import {events} from './sse.js';
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n;};
let token = '', selected = '', controller, noticeTimer, refreshTimer, lastRecheck = null;
const verdicts = {fixed: 'Fixed', partially_fixed: 'Partially fixed', not_fixed: 'Still open', obsolete: 'No longer applies', unverifiable: 'Could not verify'};
const activityNames = new Map();
const stages = ['purpose', 'design', 'correctness', 'complexity', 'tests_', 'line_review', 'system_context'];
const labels = x => String(x || '').replaceAll('_', ' ').toLowerCase();
function notice(message) { $('notice').textContent = message; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').textContent = '', 8000); }
async function request(path, options = {}) {
  const response = await fetch('/api' + path, {...options, headers: {Authorization: `Bearer ${token}`, ...options.headers}});
  if (!response.ok) {
    let detail; try { detail = (await response.json()).detail; } catch {}
    const error = new Error(typeof detail === 'string' ? detail : `Request failed (${response.status})`);
    error.status = response.status; throw error;
  }
  return response;
}
async function refresh() {
  const {reviews} = await (await request('/admin/reviews')).json();
  $('reviews').replaceChildren();
  if (!reviews.length) $('reviews').append(el('p', 'No reviews yet.', 'hint'));
  for (const review of reviews) {
    const button = el('button', `Project ${review.project_id} · MR !${review.mr_iid}`, review.id === selected ? 'selected' : '');
    button.append(el('small', `${labels(review.state)} · ${review.head_sha.slice(0, 8)}`));
    button.onclick = () => select(review.id); $('reviews').append(button);
  }
}
function render(review) {
  $('identity').textContent = `PROJECT ${review.project_id} / MR !${review.mr_iid}`;
  $('state').textContent = labels(review.state);
  $('metadata').textContent = `${review.head_sha.slice(0, 12)} · ${review.decision || 'Decision pending'}${review.partial ? ' · Partial review (fails open)' : ''}`;
  $('findings').replaceChildren(); $('count').textContent = review.findings.length;
  if (!review.findings.length) $('findings').append(el('p', 'No stored findings to show.', 'muted'));
  for (const finding of review.findings) {
    const article = el('article', '', 'finding');
    article.append(el('span', finding.severity || finding.status, `badge ${finding.severity}`), el('h3', finding.claim), el('code', `${finding.file || ''}:${finding.line_start || ''}`));
    for (const key of ['reason', 'impact', 'failure_scenario', 'suggested_direction']) if (finding[key]) article.append(el('p', finding[key]));
    article.append(el('p', `${labels(finding.stage)} · ${finding.verdict || 'Unverified'} · ${finding.status}`, 'hint'));
    $('findings').append(article);
  }
  $('report-section').hidden = !review.report; $('report').textContent = review.report || '';
  renderRecheck(review.recheck);
}
function renderRecheck(recheck) {
  lastRecheck = JSON.stringify(recheck ?? null);
  $('recheck-section').hidden = !recheck;
  if (!recheck) return;
  const answers = recheck.verdicts || [];
  const resolved = new Set((recheck.posted || []).filter(p => p.resolved).map(p => p.fingerprint));
  $('recheck-count').textContent = answers.length;
  $('recheck-meta').textContent = [recheck.mode === 'draft' ? 'Drafted on GitLab' : 'Posted on the merge request',
    `head ${String(recheck.head_sha || '').slice(0, 12)}`, recheck.at ? new Date(recheck.at).toLocaleString() : ''].filter(Boolean).join(' · ');
  $('recheck-results').replaceChildren();
  for (const answer of answers) {
    const article = el('article', '', 'finding');
    article.append(el('span', verdicts[answer.verdict] || labels(answer.verdict), `badge ${answer.verdict}`), el('h3', answer.claim), el('code', `${answer.file || ''}:${answer.line || ''}`));
    for (const key of ['change_summary', 'reasoning']) if (answer[key]) article.append(el('p', answer[key]));
    article.append(el('p', `${answer.judged ? 'Judged by model' : 'Determined from the diff'} · ${resolved.has(answer.fingerprint) ? 'thread resolved' : 'thread left open'}`, 'hint'));
    $('recheck-results').append(article);
  }
}
function addActivity(item) {
  const {data, kind} = item;
  if (kind === 'state') { $('state').textContent = labels(data.state); }
  if (kind === 'agent') {
    const node = Array.from($('stages').children).find(node => node.dataset.stage === data.name);
    if (node) { node.className = `stage ${data.status === 'started' ? 'active' : data.status === 'completed' ? 'done' : 'warning'}`; node.textContent = `${labels(data.name)} · ${data.status}`; }
  }
  const ancestor = activityNames.get(data.parent_id);
  const name = ancestor ? `${ancestor} / ${labels(data.name)}` : labels(data.name);
  if (data.activity_id) activityNames.set(data.activity_id, name);
  const li = el('li', '', kind);
  li.append(el('time', new Date(item.at).toLocaleTimeString()), el('span', kind === 'state' ? labels(data.state) : `${labels(kind)} / ${name}`, 'label'), el('span', data.status || 'transition', 'badge'));
  $('activity').prepend(li);
  if ($('activity').children.length > 300) $('activity').lastChild.remove();
}
const delay = (ms, signal) => new Promise(resolve => {
  const done = () => {clearTimeout(timer); signal.removeEventListener('abort', done); resolve();};
  const timer = setTimeout(done, ms); signal.addEventListener('abort', done, {once:true});
  if (signal.aborted) done();
});
async function follow(id, signal) {
  let cursor = '0';
  while (!signal.aborted) {
    try {
      $('live').textContent = 'Connecting';
      const response = await request(`/admin/reviews/${encodeURIComponent(id)}/events`, {signal, headers: {'Last-Event-ID': cursor, Accept: 'text/event-stream'}});
      $('live').textContent = '● Live';
      for await (const event of events(response.body)) {
        if (signal.aborted) return;
        if (event.event === 'snapshot') render(event.data);
        if (event.event === 'activity' && Number(event.id) > Number(cursor)) {addActivity(event.data); cursor = event.id;}
        if (event.event === 'complete') { $('live').textContent = 'Finished'; for (const node of $('stages').children) { if (!node.classList.contains('done') && !node.classList.contains('warning')) { node.className = 'stage'; node.textContent += ' · no completion recorded'; } } refresh().catch(e => notice(e.message)); return; }
      }
    } catch (error) {
      if (signal.aborted) return;
      if ([401, 403, 404].includes(error.status)) { $('live').textContent = 'Disconnected'; notice(error.message); return; }
    }
    $('live').textContent = 'Reconnecting…'; await delay(2000, signal);
  }
}
function select(id) {
  controller?.abort(); controller = new AbortController(); selected = id; activityNames.clear();
  $('empty').hidden = true; $('review').hidden = false;
  $('activity').replaceChildren(); $('findings').replaceChildren(); $('count').textContent = '0';
  $('identity').textContent = ''; $('state').textContent = 'Loading review…'; $('metadata').textContent = ''; $('report-section').hidden = true;
  $('recheck-section').hidden = true; $('recheck-results').replaceChildren(); lastRecheck = null;
  $('stages').replaceChildren(...stages.map(name => {const n = el('span', labels(name), 'stage'); n.dataset.stage = name; return n;}));
  refresh().catch(e => notice(e.message)); follow(id, controller.signal).catch(e => notice(e.message));
}
$('login').onsubmit = async event => {
  event.preventDefault(); token = $('token').value;
  try { await refresh(); $('token').value = ''; $('connect').hidden = true; $('workspace').hidden = false; $('disconnect').hidden = false; clearInterval(refreshTimer); refreshTimer = setInterval(() => refresh().catch(() => {}), 15000); }
  catch (error) {token = ''; notice(error.message);}
};
$('disconnect').onclick = () => { controller?.abort(); clearInterval(refreshTimer); token = ''; location.reload(); };
$('refresh').onclick = () => refresh().catch(e => notice(e.message));
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
      if (JSON.stringify(review.recheck ?? null) !== before) { renderRecheck(review.recheck); notice('Recheck answered the open threads.'); return; }
    }
    notice('No recheck answers recorded yet. The review may have no open reviewer threads, or recheck may be off for this project.');
  } catch (error) { if (error.name !== 'AbortError') notice(error.message); }
  finally { $('recheck').disabled = false; }
};
$('trigger').onsubmit = async event => {
  event.preventDefault(); $('start').disabled = true;
  const data = new FormData(event.target);
  const body = {merge_request_url:data.get('merge_request_url'), report_mode:data.get('report_mode'), document_urls:data.get('documents').split('\n').map(x => x.trim()).filter(Boolean)};
  for (const key of ['issue_key','epic_key']) if (data.get(key).trim()) body[key] = data.get(key).trim();
  try {
    const job = await (await request('/admin/reviews', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
    notice('Review queued. Waiting for worker admission…');
    $('empty').hidden = false; $('review').hidden = true;
    $('empty').replaceChildren(el('h2', 'Review queued'), el('p', `Project ${job.project_id} · MR !${job.iid}. Waiting for worker admission…`, 'muted'));
    controller?.abort(); controller = new AbortController(); const signal = controller.signal;
    // Keep the queued event address available even if admission is delayed.
    while (!signal.aborted) {
      const review = await (await request(job.poll, {signal})).json();
      if (review.id) { select(review.id); break; }
      await delay(2000, signal);
    }
  } catch (error) {if (error.name !== 'AbortError') notice(error.message);}
  finally { $('start').disabled = false; }
};
