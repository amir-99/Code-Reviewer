import {test} from 'node:test';
import assert from 'node:assert/strict';
import {walk, spans} from './flow.js';

const at = (...steps) => new Map(steps);
const status = (flow, state) => flow.steps.find(step => step.state === state).status;

test('a fresh review lights only the step it is on', () => {
  const flow = walk('INIT', ['INIT']);
  assert.equal(status(flow, 'INIT'), 'active');
  assert.equal(status(flow, 'CONTEXT_COLLECTION'), 'pending');
  assert.equal(status(flow, 'PUBLISHED'), 'pending');
  assert.equal(flow.percent, 0);
  assert.equal(flow.terminal, false);
});

test('walked steps light up behind the running one', () => {
  const flow = walk('DEFECT_REVIEW',
    ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'DEFECT_REVIEW']);
  assert.equal(status(flow, 'STATIC_ANALYSIS'), 'done');
  assert.equal(status(flow, 'DEFECT_REVIEW'), 'active');
  assert.equal(status(flow, 'EVIDENCE_VALIDATION'), 'pending');
  assert.ok(flow.percent > 0 && flow.percent < 100);
});

test('a review run by the removed deep mode reads as the defect review step', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'PURPOSE_REVIEW',
    'DESIGN_REVIEW', 'ANALYSIS_FAN_OUT', 'SYSTEM_CONTEXT_REVIEW', 'EVIDENCE_VALIDATION'];
  const times = at(['STATIC_ANALYSIS', 1000], ['PURPOSE_REVIEW', 2000], ['DESIGN_REVIEW', 3000],
    ['ANALYSIS_FAN_OUT', 4000], ['SYSTEM_CONTEXT_REVIEW', 5000], ['EVIDENCE_VALIDATION', 9000]);
  const stages = new Map([
    ['purpose', {status: 'completed', at: 2000}],
    ['tests_', {status: 'partial', at: 4000}],
  ]);
  const flow = walk('EVIDENCE_VALIDATION', history, stages, times);
  assert.ok(!flow.steps.some(step => step.state === 'PURPOSE_REVIEW'));
  // One partial legacy stage keeps the combined step off "done".
  assert.equal(status(flow, 'DEFECT_REVIEW'), 'warn');
  assert.equal(flow.steps.find(s => s.state === 'DEFECT_REVIEW').took, 7000);
  assert.equal(status(flow, 'EVIDENCE_VALIDATION'), 'active');
  // Interrupted in a legacy stage, it is in the defect review step now.
  const running = walk('DESIGN_REVIEW', history.slice(0, 5));
  assert.equal(status(running, 'DEFECT_REVIEW'), 'active');
});

test('a partial stage keeps its step off "done" after the state moves on', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'DEFECT_REVIEW',
    'EVIDENCE_VALIDATION'];
  const flow = walk('EVIDENCE_VALIDATION', history, new Map([['defect_review', {status: 'partial'}]]));
  assert.equal(status(flow, 'DEFECT_REVIEW'), 'warn');
  assert.equal(flow.steps.find(s => s.state === 'DEFECT_REVIEW').note, 'partial');

  const lost = walk('EVIDENCE_VALIDATION', history,
    new Map([['defect_review', {status: 'no completion'}]]));
  assert.equal(status(lost, 'DEFECT_REVIEW'), 'failed');
});

test('a milestone short circuit marks the states it jumped as not run', () => {
  const flow = walk('PUBLISHED', ['INIT', 'FINALIZATION', 'DECISION', 'PUBLISHED']);
  assert.equal(status(flow, 'INIT'), 'done');
  assert.equal(status(flow, 'CONTEXT_COLLECTION'), 'skipped');
  assert.equal(status(flow, 'DEFECT_REVIEW'), 'skipped');
  assert.equal(flow.steps.find(s => s.state === 'DEFECT_REVIEW').note, 'not run');
  assert.equal(status(flow, 'PUBLISHED'), 'done');
  assert.equal(flow.percent, 100);
});

test('a review that stopped stops the flow where it stopped', () => {
  const early = walk('TERMINATED_EARLY',
    ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'DEFECT_REVIEW', 'TERMINATED_EARLY']);
  assert.equal(status(early, 'DEFECT_REVIEW'), 'stopped');
  assert.equal(status(early, 'EVIDENCE_VALIDATION'), 'skipped');
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

test('standard reviews show the combined stage and preserve partial coverage', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'DEFECT_REVIEW',
    'EVIDENCE_VALIDATION', 'FINDING_VERIFICATION', 'FINALIZATION', 'DECISION', 'PUBLISHED'];
  const flow = walk('PUBLISHED', history, new Map([['defect_review', {status: 'partial'}]]));
  assert.equal(status(flow, 'DEFECT_REVIEW'), 'warn');
  assert.equal(status(flow, 'FINDING_VERIFICATION'), 'done');
  assert.equal(flow.steps.some(step => step.state === 'PURPOSE_REVIEW'), false);
  assert.equal(flow.percent, 100);
});

test('unit counts use planned totals and ignore stale progress snapshots', async () => {
  const {UnitProgress} = await import('./flow.js');
  const progress = new UnitProgress();
  const step = {stage: 'defect_review'};
  assert.equal(progress.get(step), undefined);
  progress.accept({kind: 'stage_dispatch', data: {name: step.stage, units: 8}});
  assert.equal(progress.get(step).total, 8);
  assert.equal(progress.get(step).running, null);
  const data = {name: step.stage, execution_id: 'a', revision: 2,
    total: 8, completed: 2, running: 3, idle: 3, stopped: 0};
  progress.accept({kind: 'unit_progress', data});
  progress.accept({kind: 'unit_progress', data: {...data, revision: 1, completed: 0}});
  progress.accept({kind: 'unit_progress', data});
  assert.equal(progress.get(step).completed, 2);
  progress.close();
  assert.equal(progress.get(step).running, 0);
  assert.equal(progress.get(step).stopped, 3);
  assert.equal(progress.get(step).completed, 2);
  progress.clear();
  assert.equal(progress.get(step), undefined);
});

test('coverage without unit snapshots still yields counts, and stageless steps have none', async () => {
  const {UnitProgress} = await import('./flow.js');
  const progress = new UnitProgress();
  progress.accept({kind: 'stage_coverage', data: {name: 'defect_review', examined: 3, skipped: 1}});
  assert.deepEqual(progress.get({stage: 'defect_review'}),
    {total: 4, completed: 3, running: 0, idle: 0, stopped: 1});
  assert.equal(progress.get({state: 'FINALIZATION'}), undefined);
});

test('a document review walks the page flow without the code stages', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'DOCUMENT_REVIEW', 'EVIDENCE_VALIDATION'];
  const {steps} = walk('EVIDENCE_VALIDATION', history);
  assert.deepEqual(steps.map(s => s.state), [
    'INIT', 'CONTEXT_COLLECTION', 'DOCUMENT_REVIEW', 'EVIDENCE_VALIDATION',
    'FINDING_VERIFICATION', 'FINALIZATION', 'DECISION', 'PUBLISHED',
  ]);
  assert.equal(steps[2].status, 'done');
  assert.equal(steps[3].status, 'active');
  // A merge request review never shows the document step.
  assert.ok(!walk('DEFECT_REVIEW', ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'DEFECT_REVIEW'])
    .steps.some(s => s.state === 'DOCUMENT_REVIEW'));
});

test('verification that starts per file runs alongside the defect review step', () => {
  const history = ['INIT', 'CONTEXT_COLLECTION', 'STATIC_ANALYSIS', 'DEFECT_REVIEW'];
  const early = at(['FINDING_VERIFICATION', 5000]);
  const running = walk('DEFECT_REVIEW', history, new Map(), at(['DEFECT_REVIEW', 1000]), early);
  const verification = running.steps.find(s => s.state === 'FINDING_VERIFICATION');
  assert.equal(status(running, 'DEFECT_REVIEW'), 'active');
  assert.equal(status(running, 'EVIDENCE_VALIDATION'), 'pending');
  assert.equal(verification.status, 'active');
  assert.equal(verification.note, 'started early, per file');
  assert.equal(verification.at, 5000);
  // The track still reads where the pipeline is, not where its early work is.
  assert.equal(running.reached, walk('DEFECT_REVIEW', history).reached);

  // Once walked, the step's time counts from the first verifier, not the state.
  const walked = [...history, 'EVIDENCE_VALIDATION', 'FINDING_VERIFICATION', 'FINALIZATION'];
  const times = at(['DEFECT_REVIEW', 1000], ['EVIDENCE_VALIDATION', 9000],
    ['FINDING_VERIFICATION', 9000], ['FINALIZATION', 12000]);
  const done = walk('FINALIZATION', walked, new Map(), times, early);
  const step = done.steps.find(s => s.state === 'FINDING_VERIFICATION');
  assert.equal(step.status, 'done');
  assert.equal(step.took, 7000);
  assert.equal(step.note, 'independent');

  // A verifier that starts after the state was entered changes nothing.
  const late = walk('FINALIZATION', walked, new Map(), times, at(['FINDING_VERIFICATION', 10000]));
  assert.equal(late.steps.find(s => s.state === 'FINDING_VERIFICATION').took, 3000);

  // A review that stops in defect review does not show verification running.
  const cancelled = walk('CANCELLED', [...history, 'CANCELLED'], new Map(), at(), early);
  assert.equal(status(cancelled, 'FINDING_VERIFICATION'), 'skipped');
});
