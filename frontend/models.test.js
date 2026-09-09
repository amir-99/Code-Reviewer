import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chosenModels, modelFor, roleFor, roleLabel} from './models.js';

const RESOLVED = {
  correctness: 'vendor/strong', line_review: 'vendor/cheap',
  verification: 'vendor/judge', recheck: 'vendor/judge',
};

const attribute = (kind, data, ancestor) => {
  const role = roleFor(kind, data, ancestor);
  return {role, model: modelFor(kind, data, role, RESOLVED)};
};

test('a stage reports the model resolved for its own role', () => {
  assert.deepEqual(attribute('agent', {name: 'correctness'}),
    {role: 'correctness', model: 'vendor/strong'});
  assert.deepEqual(attribute('agent', {name: 'line_review'}),
    {role: 'line_review', model: 'vendor/cheap'});
});

test('work units and the gateway call inherit the stage they run under', () => {
  const stage = attribute('agent', {name: 'line_review'});
  const unit = attribute('unit', {name: 'Review work unit'}, stage);
  const call = attribute('tool', {name: 'LLM gateway'}, unit);
  assert.equal(unit.model, 'vendor/cheap');
  assert.equal(call.model, 'vendor/cheap');
});

test('the verifier and the recheck judge carry their own roles, not their parent', () => {
  const stage = attribute('agent', {name: 'correctness'});
  assert.deepEqual(attribute('tool', {name: 'Independent verifier'}, stage),
    {role: 'verification', model: 'vendor/judge'});
  assert.deepEqual(attribute('tool', {name: 'Fix recheck'}, stage),
    {role: 'recheck', model: 'vendor/judge'});
});

test('a gateway attempt reports the model that actually served it', () => {
  // The attempt is the only event that knows a call ran on something other than
  // what selection announced, so its own model wins over the resolved map.
  assert.equal(modelFor('llm_attempt', {stage: 'design', model: 'vendor/fallback'}, 'design', RESOLVED),
    'vendor/fallback');
  // Without one it still falls back to the role the attempt names.
  assert.equal(modelFor('llm_attempt', {role: 'correctness'}, roleFor('llm_attempt', {role: 'correctness'}), RESOLVED),
    'vendor/strong');
});

test('phases and the selection announcement belong to no role', () => {
  assert.equal(roleFor('state', {state: 'DESIGN_REVIEW'}), '');
  assert.equal(roleFor('models', {correctness: 'vendor/strong'}), '');
  assert.equal(roleFor('run', {name: 'review run'}), '');
  assert.equal(modelFor('state', {state: 'DESIGN_REVIEW'}, '', RESOLVED), '');
});

test('a role with no resolved model reports none rather than guessing', () => {
  assert.equal(modelFor('agent', {name: 'purpose'}, 'purpose', RESOLVED), '');
  assert.equal(modelFor('unit', {name: 'Review work unit'}, '', {}), '');
});

test('a run sends only the roles the operator moved', () => {
  const choices = new Map([['correctness', 'vendor/strong'], ['design', '']]);
  assert.deepEqual(chosenModels(choices), {correctness: 'vendor/strong'});
  assert.deepEqual(chosenModels(new Map()), {});
  assert.deepEqual(chosenModels({line_review: 'vendor/cheap'}), {line_review: 'vendor/cheap'});
});

test('roles are labelled for people, not for Python', () => {
  assert.equal(roleLabel('tests_'), 'Tests');
  assert.equal(roleLabel('system_context'), 'System context');
  assert.equal(roleLabel('unknown_role'), 'unknown_role');
});
