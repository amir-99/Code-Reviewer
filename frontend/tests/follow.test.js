import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
const follow = source.slice(source.indexOf('async function follow('), source.indexOf('\nfunction select('));

function harness(pendingRecheck) {
  const cursors = [], activity = [], notices = [];
  let calls = 0;
  const context = vm.createContext({
    pendingRecheck, live() {}, render() {}, refresh: async () => {},
    closeOpenActivities() {}, tabTouched: true, tone: () => 'ok', current: null,
    RECHECK_BUTTONS: [], $: () => ({}), notice: text => notices.push(text), showTab() {},
    request: async (url, options) => {cursors.push(options.headers['Last-Event-ID']); return {body: ++calls};},
    events: async function* (body) {
      yield {event: 'activity', id: String(body), data: {id: body}};
      if (body === 2) context.pendingRecheck = null;
      yield {event: 'complete', data: {state: 'PUBLISHED'}};
    },
    addActivity: row => activity.push(row.id), delay: async () => {},
  });
  vm.runInContext(follow, context);
  return {context, cursors, activity, notices};
}

test('an unanswered recheck reconnects after complete and streams on with its cursor', async () => {
  const h = harness({id: 'review', before: 'null', until: Infinity});
  await h.context.follow('review', new AbortController().signal);
  assert.deepEqual(h.cursors, ['0', '1']);
  assert.deepEqual(h.activity, [1, 2]);
  assert.deepEqual(h.notices, []);
});

test('the page stops waiting on a recheck that never answers', async () => {
  const h = harness({id: 'review', before: 'null', until: 0});
  await h.context.follow('review', new AbortController().signal);
  assert.deepEqual(h.cursors, ['0']);
  assert.equal(h.context.pendingRecheck, null);
  assert.equal(h.notices.length, 1);
});

test('a recheck for another review does not hold this stream', async () => {
  const h = harness({id: 'other', before: 'null', until: Infinity});
  await h.context.follow('review', new AbortController().signal);
  assert.deepEqual(h.cursors, ['0']);
});
