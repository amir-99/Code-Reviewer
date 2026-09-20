import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

test('queued recheck reconnects after complete and streams new activity with its cursor', async () => {
  const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  const follow = source.slice(source.indexOf('async function follow('), source.indexOf('\nfunction select('));
  const cursors = [], activity = [];
  let calls = 0;
  const context = vm.createContext({
    pendingRecheck: 'review', live() {}, render() {}, refresh: async () => {},
    closeOpenActivities() {}, tabTouched: true, tone: () => 'ok',
    request: async (url, options) => {cursors.push(options.headers['Last-Event-ID']); return {body: ++calls};},
    events: async function* (body) {
      yield {event: 'activity', id: String(body), data: {id: body}};
      if (body === 2) context.pendingRecheck = '';
      yield {event: 'complete', data: {state: 'PUBLISHED'}};
    },
    addActivity: row => activity.push(row.id), delay: async () => {},
  });
  vm.runInContext(follow, context);
  await context.follow('review', new AbortController().signal);
  assert.deepEqual(cursors, ['0', '1']);
  assert.deepEqual(activity, [1, 2]);
});
