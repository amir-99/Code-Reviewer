import {test} from 'node:test';
import assert from 'node:assert/strict';
import {addAttempt, cost, emptySpend, mergeSpend, tokens, usage} from './spend.js';

const attempt = (over = {}) => ({
  role: 'correctness', model: 'vendor/strong', outcome: 'success',
  tokens_in: 1000, tokens_out: 200, cost: 0.005, ...over,
});

test('an attempt adds to both the total and its own role', () => {
  const delta = addAttempt(emptySpend(), attempt());
  assert.equal(delta.calls, 1);
  assert.equal(delta.tokens, 1200);
  assert.equal(delta.cost, 0.005);
  assert.deepEqual(delta.roles.map(r => [r.role, r.model, r.tokens, r.calls]),
    [['correctness', 'vendor/strong', 1200, 1]]);
});

test('attempts on the same role and model accumulate into one row', () => {
  const delta = emptySpend();
  addAttempt(delta, attempt());
  addAttempt(delta, attempt({tokens_in: 500, cost: 0.002}));
  assert.equal(delta.roles.length, 1);
  assert.equal(delta.roles[0].calls, 2);
  assert.equal(delta.tokens, 1900);
  // The same role served by a different model is a different row: that is the
  // comparison an operator changing one role's model is trying to make.
  addAttempt(delta, attempt({model: 'vendor/cheap'}));
  assert.equal(delta.roles.length, 2);
});

test('a failed attempt still spent its tokens', () => {
  const delta = addAttempt(emptySpend(), attempt({outcome: 'invalid_output'}));
  assert.equal(delta.calls, 1);
  assert.equal(delta.retries, 1);
  assert.equal(delta.tokens, 1200);
});

test('an unpriced model is counted in tokens and never as free', () => {
  const delta = addAttempt(emptySpend(), attempt({cost: null}));
  assert.equal(delta.tokens, 1200);
  assert.equal(delta.cost, 0);
  assert.equal(delta.unpriced_calls, 1);
  // And the total says it is a floor rather than a price.
  assert.equal(cost(0.004, {unpriced: 1}), '≥ $0.0040');
  assert.equal(cost(0, {unpriced: 2}), 'unpriced');
});

test('the audited spend and the live delta merge without double counting', () => {
  const audited = {
    roles: [{role: 'correctness', model: 'vendor/strong', calls: 2, retries: 0,
      tokens_in: 2000, tokens_out: 400, tokens: 2400, cost: 0.01, unpriced_calls: 0}],
    calls: 2, retries: 0, tokens_in: 2000, tokens_out: 400, tokens: 2400,
    cost: 0.01, unpriced_calls: 0, token_ceiling: 100000,
  };
  const delta = emptySpend();
  addAttempt(delta, attempt());                       // same role and model
  addAttempt(delta, attempt({role: 'line_review', model: 'vendor/cheap', cost: 0.001}));
  const merged = mergeSpend(audited, delta);
  assert.equal(merged.calls, 4);
  assert.equal(merged.tokens, 2400 + 1200 + 1200);
  assert.equal(Math.round(merged.cost * 1000) / 1000, 0.016);
  assert.equal(merged.token_ceiling, 100000, 'the ceiling survives the merge');
  const correctness = merged.roles.find(r => r.role === 'correctness');
  assert.equal(correctness.calls, 3, 'one row, not one per source');
  assert.equal(merged.roles.length, 2);
  // Most expensive first: that is the row worth changing a model for.
  assert.equal(merged.roles[0].role, 'correctness');
});

test('merging works before either side has anything to say', () => {
  assert.deepEqual(mergeSpend(null, null).roles, []);
  assert.equal(mergeSpend(null, emptySpend()).cost, 0);
  assert.equal(mergeSpend({roles: [], tokens: 5}, emptySpend()).tokens, 5);
});

test('usage is reported against the ceiling the budget enforces', () => {
  assert.deepEqual(usage({tokens: 25000, token_ceiling: 100000}),
    {ceiling: 100000, used: 25000, percent: 25});
  // A run past its ceiling is reported at the ceiling, not above it.
  assert.equal(usage({tokens: 150000, token_ceiling: 100000}).percent, 100);
  assert.equal(usage({tokens: 10}), null, 'no ceiling, nothing to report against');
});

test('token counts stay readable at every scale', () => {
  assert.equal(tokens(0), '0');
  assert.equal(tokens(940), '940');
  assert.equal(tokens(1200), '1.2k');
  assert.equal(tokens(48000), '48k');
  assert.equal(tokens(2400000), '2.40M');
});

test('cost keeps the digits that matter at review scale', () => {
  assert.equal(cost(2.5), '$2.50');
  assert.equal(cost(0.128), '$0.128');
  assert.equal(cost(0.0042), '$0.0042');
  assert.equal(cost(0), '$0.0000');
  assert.equal(cost(null), '—');
  assert.equal(cost(undefined), '—');
});
