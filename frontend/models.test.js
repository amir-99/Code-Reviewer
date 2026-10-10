import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chosenModels, modelFor, roleFor, roleLabel} from './models.js';

const RESOLVED = {
  defect_review: 'vendor/strong', document_review: 'vendor/cheap',
  verification: 'vendor/judge', recheck: 'vendor/judge',
};

const attribute = (kind, data, ancestor) => {
  const role = roleFor(kind, data, ancestor);
  return {role, model: modelFor(kind, data, role, RESOLVED)};
};

test('a stage reports the model resolved for its own role', () => {
  assert.deepEqual(attribute('agent', {name: 'defect_review'}),
    {role: 'defect_review', model: 'vendor/strong'});
  assert.deepEqual(attribute('agent', {name: 'document_review'}),
    {role: 'document_review', model: 'vendor/cheap'});
});

test('work units and the gateway call inherit the stage they run under', () => {
  const stage = attribute('agent', {name: 'document_review'});
  const unit = attribute('unit', {name: 'Review work unit'}, stage);
  const call = attribute('tool', {name: 'LLM gateway'}, unit);
  assert.equal(unit.model, 'vendor/cheap');
  assert.equal(call.model, 'vendor/cheap');
});

test('the verifier and the recheck judge carry their own roles, not their parent', () => {
  const stage = attribute('agent', {name: 'defect_review'});
  assert.deepEqual(attribute('tool', {name: 'Independent verifier'}, stage),
    {role: 'verification', model: 'vendor/judge'});
  assert.deepEqual(attribute('tool', {name: 'Fix recheck'}, stage),
    {role: 'recheck', model: 'vendor/judge'});
});

test('a gateway attempt reports the model that actually served it', () => {
  // The attempt is the only event that knows a call ran on something other than
  // what selection announced, so its own model wins over the resolved map.
  assert.equal(modelFor('llm_attempt', {stage: 'defect_review', model: 'vendor/fallback'}, 'defect_review', RESOLVED),
    'vendor/fallback');
  // Without one it still falls back to the role the attempt names.
  assert.equal(modelFor('llm_attempt', {role: 'defect_review'}, roleFor('llm_attempt', {role: 'defect_review'}), RESOLVED),
    'vendor/strong');
});

test('phases and the selection announcement belong to no role', () => {
  assert.equal(roleFor('state', {state: 'DEFECT_REVIEW'}), '');
  assert.equal(roleFor('models', {defect_review: 'vendor/strong'}), '');
  assert.equal(roleFor('run', {name: 'review run'}), '');
  assert.equal(modelFor('state', {state: 'DEFECT_REVIEW'}, '', RESOLVED), '');
});

test('a role with no resolved model reports none rather than guessing', () => {
  assert.equal(modelFor('agent', {name: 'document_verification'}, 'document_verification', RESOLVED), '');
  assert.equal(modelFor('unit', {name: 'Review work unit'}, '', {}), '');
});

test('a run sends only the roles the operator moved', () => {
  const choices = new Map([['defect_review', 'vendor/strong'], ['recheck', '']]);
  assert.deepEqual(chosenModels(choices), {defect_review: 'vendor/strong'});
  assert.deepEqual(chosenModels(new Map()), {});
  assert.deepEqual(chosenModels({document_review: 'vendor/cheap'}), {document_review: 'vendor/cheap'});
});

test('roles are labelled for people, not for Python', () => {
  assert.equal(roleLabel('defect_review'), 'Defect review');
  assert.equal(roleLabel('document_verification'), 'Document verification');
  assert.equal(roleLabel('build_agent'), 'Build agent');
  // The planner runs inside the Defect Review but bills its own role.
  assert.equal(roleFor('phase', {name: 'Plan unit context'}, {role: 'defect_review'}), 'context_planner');
  assert.equal(roleLabel('unknown_role'), 'unknown_role');
  // Spend and models recorded by the removed deep mode still read as words.
  assert.equal(roleLabel('tests_'), 'Tests');
  assert.equal(roleLabel('system_context'), 'System context');
});

test('each review kind selects models for its own roles', async () => {
  const {rolesFor, DOCUMENT_ROLES} = await import('./models.js');
  const roles = ['defect_review', 'verification', 'chat', 'document_review', 'document_verification'];
  assert.deepEqual(rolesFor('document', roles), ['chat', 'document_review', 'document_verification']);
  assert.equal(DOCUMENT_ROLES.length, 3);
  assert.deepEqual(rolesFor('code', roles), ['defect_review', 'verification', 'chat']);
  assert.equal(roleLabel('document_review'), 'Document review');
});
