import test from 'node:test';
import assert from 'node:assert/strict';
import {RecheckProgress, describe} from '../public/recheck-progress.js';

const recheck = data => ({kind: 'recheck', data});

test('a recheck reports its threads as it judges them', () => {
  const progress = new RecheckProgress();
  assert.equal(progress.view, null);
  progress.accept(recheck({activity_id: 'p', status: 'started', total: 2}));
  progress.accept(recheck({activity_id: 't1', parent_id: 'p', index: 1, status: 'started', name: 'a.py:3'}));
  assert.equal(describe(progress.view), 'Judging 1 of 2 threads — a.py:3');
  progress.accept(recheck({activity_id: 't1', parent_id: 'p', index: 1, status: 'completed', verdict: 'fixed'}));
  progress.accept(recheck({activity_id: 't2', parent_id: 'p', index: 2, status: 'started', name: 'b.py:9'}));
  assert.equal(describe(progress.view, v => ({fixed: 'Fixed'})[v] || v), 'Judging 2 of 2 threads — b.py:9 · 1 fixed');
  progress.accept(recheck({activity_id: 't2', parent_id: 'p', index: 2, status: 'completed', verdict: 'not_fixed'}));
  progress.accept(recheck({activity_id: 'p', status: 'completed', total: 2}));
  const view = progress.view;
  assert.equal(view.active, false);
  assert.deepEqual(view.tally, {fixed: 1, not_fixed: 1});
});

test('a new pass starts the count again and ignores strays', () => {
  const progress = new RecheckProgress();
  progress.accept(recheck({activity_id: 'old', status: 'started', total: 5}));
  progress.accept(recheck({activity_id: 'new', status: 'started', total: 1}));
  assert.equal(progress.accept(recheck({activity_id: 'x', parent_id: 'old', index: 1, status: 'completed'})), false);
  assert.equal(progress.view.total, 1);
  assert.equal(progress.view.done, 0);
});
