import test from 'node:test';
import assert from 'node:assert/strict';
import {CheckLog, checkTone} from '../public/checks.js';

const row = data => ({kind: 'check', data: {activity_id: 'c1', ...data}});

test('streamed rows build one check with its output and outcome', () => {
  const log = new CheckLog();
  assert.equal(log.accept({kind: 'tool', data: {activity_id: 't'}}), false);
  log.accept(row({name: 'ruff', status: 'running', lines: []}));
  log.accept(row({status: 'running', lines: ['a.py:1:1: F401 unused']}));
  log.accept(row({status: 'failed', lines: ['Found 1 error.'], exit_code: 1, duration_ms: 900}));
  const [check] = log.list;
  assert.equal(check.name, 'ruff');
  assert.deepEqual(check.lines, ['a.py:1:1: F401 unused', 'Found 1 error.']);
  assert.equal(check.status, 'failed');
  assert.equal(check.exit_code, 1);
  assert.deepEqual(log.counts, {running: 0, passed: 0, failed: 1, other: 0});
  assert.equal(checkTone('failed'), 'tone-bad');
});

test('the finished record replaces what streamed, and clear forgets both', () => {
  const log = new CheckLog();
  log.accept(row({name: 'go vet', status: 'running'}));
  log.load([{name: 'go vet', status: 'passed', lines: []}]);
  assert.deepEqual(log.list.map(c => c.status), ['passed']);
  log.clear();
  assert.deepEqual(log.list, []);
});
