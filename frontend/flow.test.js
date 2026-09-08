import {test} from 'node:test';
import assert from 'node:assert/strict';
import {walk, spans} from './flow.js';

const at = (...steps) => new Map(steps);
const status = (flow, state) => flow.steps.find(step => step.state === state).status;
const lane = (flow, name) => flow.steps.find(step => step.lanes.length).lanes.find(l => l.stage === name);

test('a fresh review lights only the step it is on', () => {
  const flow = walk('INIT', ['INIT']);
  assert.equal(status(flow, 'INIT'), 'active');
  assert.equal(status(flow, 'CONTEXT_COLLECTION'), 'pending');
  assert.equal(status(flow, 'PUBLISHED'), 'pending');
  assert.equal(flow.percent, 0);
  assert.equal(flow.terminal, false);
});

test('walked steps light up behind the running one', () => {
  const flow = walk('DESIGN_REVIEW',
    ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'PURPOSE_REVIEW', 'DESIGN_REVIEW']);
  assert.equal(status(flow, 'STATIC_ANALYSIS'), 'done');
  assert.equal(status(flow, 'PURPOSE_REVIEW'), 'done');
  assert.equal(status(flow, 'DESIGN_REVIEW'), 'active');
  assert.equal(status(flow, 'ANALYSIS_FAN_OUT'), 'pending');
  assert.ok(flow.percent > 0 && flow.percent < 100);
});

test('the fan-out lanes report themselves, and a silent lane did not run', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'PURPOSE_REVIEW',
    'DESIGN_REVIEW', 'ANALYSIS_FAN_OUT'];
  const running = walk('ANALYSIS_FAN_OUT', history, new Map([
    ['correctness', {status: 'started', at: 10}],
    ['complexity', {status: 'completed', at: 5, took: 900}],
    ['tests_', {status: 'partial', at: 5, took: 400}],
    ['line_review', {status: 'failed', at: 5, took: 100}],
  ]));
  assert.equal(lane(running, 'correctness').status, 'active');
  assert.equal(lane(running, 'complexity').status, 'done');
  assert.equal(lane(running, 'tests_').status, 'warn');
  assert.equal(lane(running, 'line_review').status, 'failed');
  assert.equal(lane(running, 'complexity').took, 900);
  // A clean lane says nothing; the ones worth a second look name themselves.
  assert.equal(lane(running, 'complexity').note, '');
  assert.equal(lane(running, 'tests_').note, 'partial');

  // Triage mode dispatches one lane; the rest never ran and must not read as passed.
  const triage = walk('SYSTEM_CONTEXT_REVIEW', [...history, 'SYSTEM_CONTEXT_REVIEW'],
    new Map([['tests_', {status: 'completed'}]]));
  assert.equal(lane(triage, 'tests_').status, 'done');
  assert.equal(lane(triage, 'correctness').status, 'skipped');
  assert.equal(lane(triage, 'correctness').note, 'not reported');
});

test('a partial stage keeps its step off "done" after the state moves on', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'PURPOSE_REVIEW', 'DESIGN_REVIEW'];
  const flow = walk('DESIGN_REVIEW', history, new Map([['purpose', {status: 'partial'}]]));
  assert.equal(status(flow, 'PURPOSE_REVIEW'), 'warn');
  assert.equal(flow.steps.find(s => s.state === 'PURPOSE_REVIEW').note, 'partial');

  const lost = walk('DESIGN_REVIEW', history, new Map([['purpose', {status: 'no completion'}]]));
  assert.equal(status(lost, 'PURPOSE_REVIEW'), 'failed');
});

test('a milestone short circuit marks the states it jumped as not run', () => {
  const flow = walk('PUBLISHED', ['INIT', 'FINALIZATION', 'DECISION', 'PUBLISHED']);
  assert.equal(status(flow, 'INIT'), 'done');
  assert.equal(status(flow, 'CONTEXT_COLLECTION'), 'skipped');
  assert.equal(status(flow, 'ANALYSIS_FAN_OUT'), 'skipped');
  assert.equal(flow.steps.find(s => s.state === 'ANALYSIS_FAN_OUT').note, 'not run');
  assert.equal(status(flow, 'PUBLISHED'), 'done');
  assert.equal(flow.percent, 100);
});

test('a review that stopped stops the flow where it stopped', () => {
  const early = walk('TERMINATED_EARLY',
    ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'PURPOSE_REVIEW', 'TERMINATED_EARLY']);
  assert.equal(status(early, 'PURPOSE_REVIEW'), 'stopped');
  assert.equal(status(early, 'DESIGN_REVIEW'), 'skipped');
  assert.equal(status(early, 'PUBLISHED'), 'skipped');
  assert.equal(early.tone, 'warn');
  assert.equal(early.terminal, true);
  assert.ok(early.percent < 100, 'a terminated review has not walked the pipeline');

  const failed = walk('FAILED_INTERNAL',
    ['INIT', 'CONTEXT_COLLECTION', 'FAILED_INTERNAL']);
  assert.equal(status(failed, 'CONTEXT_COLLECTION'), 'failed');
  assert.equal(failed.tone, 'bad');
});

test('each step reports the time the pipeline spent in it', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS'];
  const times = at(['INIT', 1000], ['CONTEXT_COLLECTION', 4000], ['STATIC_ANALYSIS', 9000]);
  assert.deepEqual([...spans(history, times)], [['INIT', 3000], ['CONTEXT_COLLECTION', 5000]]);
  const flow = walk('STATIC_ANALYSIS', history, new Map(), times);
  assert.equal(flow.steps.find(s => s.state === 'INIT').took, 3000);
  // The step still running has no duration yet, only the moment it began.
  assert.equal(flow.steps.find(s => s.state === 'STATIC_ANALYSIS').took, null);
  assert.equal(flow.steps.find(s => s.state === 'STATIC_ANALYSIS').at, 9000);
});

test('an empty history still reads the state it was given', () => {
  const flow = walk('CONTEXT_COLLECTION');
  assert.equal(status(flow, 'CONTEXT_COLLECTION'), 'active');
  assert.equal(status(flow, 'INIT'), 'skipped');
});
