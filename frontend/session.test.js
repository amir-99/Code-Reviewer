import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const helpers = source.slice(source.indexOf('function startSession('), source.indexOf('function clearSession('));

function harness(request) {
  const nodes = new Map();
  const notices = [], intervals = [];
  const context = vm.createContext({
    token: '', account: null, refreshTimer: undefined,
    $: id => {
      if (!nodes.has(id)) nodes.set(id, {hidden: false, value: 'password'});
      return nodes.get(id);
    },
    request, applyAccount() {}, refresh: async () => {}, loadModels: async () => {},
    clearInterval() {}, setInterval: fn => intervals.push(fn),
    notice: text => notices.push(text), connection() {},
  });
  vm.runInContext(helpers, context);
  return {context, nodes, notices, intervals};
}

test('page startup restores account and CSRF, reveals workspace and starts refresh', async () => {
  const account = {id: 'owner', role: 'user'};
  const h = harness(async path => {
    assert.equal(path, '/auth/me');
    return {json: async () => ({account, csrf_token: 'restored'})};
  });
  // Execute the actual page startup statement with freshly initialized state.
  await vm.runInContext(source.slice(source.lastIndexOf('void restoreSession();')).replace('void ', ''), h.context);
  assert.equal(h.context.account, account);
  assert.equal(h.context.token, 'restored');
  assert.equal(h.nodes.get('connect').hidden, true);
  assert.equal(h.nodes.get('workspace').hidden, false);
  assert.equal(h.nodes.get('disconnect').hidden, false);
  assert.equal(h.nodes.get('token').value, '');
  assert.equal(h.intervals.length, 1);
});

test('expired cookie shows login, while network failures explain how to retry', async () => {
  for (const status of [401, 503]) {
    const h = harness(async () => {throw Object.assign(new Error('failed'), {status});});
    await h.context.restoreSession();
    assert.equal(h.context.account, null);
    assert.equal(h.context.token, '');
    assert.equal(h.nodes.get('connect').hidden, false);
    assert.equal(h.intervals.length, 0);
    assert.equal(h.notices.length, status === 401 ? 0 : 1);
  }
});

test('review data outage does not discard a restored session', async () => {
  const h = harness(async () => ({json: async () => ({account: {id: 'owner'}, csrf_token: 'restored'})}));
  h.context.refresh = async () => {throw new Error('API unavailable');};
  await h.context.restoreSession();
  assert.equal(h.context.token, 'restored');
  assert.equal(h.nodes.get('connect').hidden, true);
  assert.equal(h.nodes.get('workspace').hidden, false);
  assert.equal(h.notices.length, 1);
});
