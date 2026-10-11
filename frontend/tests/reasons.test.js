import test from 'node:test';
import assert from 'node:assert/strict';
import {explain} from '../public/reasons.js';

const reason = (message, partial = false) => ({code: message, partial, message});

test('failed and stopped reviews lead with why', () => {
  const failed = explain({state: 'FAILED_INTERNAL', reasons: [reason('Git broke')]});
  assert.equal(failed.tone, 'bad');
  assert.deepEqual(failed.items.map(r => r.message), ['Git broke']);
  // Nothing recorded is said plainly rather than left blank.
  assert.equal(explain({state: 'FAILED_CONTEXT', reasons: []}).items[0].message, 'No reason was recorded.');
  const stopped = explain({state: 'CANCELLED', reasons: [reason('Stopped by its owner')]});
  assert.equal(stopped.tone, 'warn');
  assert.equal(stopped.title, 'Why this review stopped');
});

test('partial reviews put what made them partial first', () => {
  const view = explain({state: 'PUBLISHED', partial: true, reasons: [reason('Jira down'), reason('Budget ran out', true)]});
  assert.equal(view.title, 'Why this review is partial');
  assert.deepEqual(view.items.map(r => r.message), ['Budget ran out', 'Jira down']);
});

test('complete reviews mention limits quietly, and clean ones say nothing', () => {
  assert.equal(explain({state: 'PUBLISHED', partial: false, reasons: [reason('Jira down')]}).tone, 'info');
  assert.equal(explain({state: 'PUBLISHED', partial: false, reasons: []}), null);
  assert.equal(explain({state: 'DEFECT_REVIEW'}), null);
  // An older API sends only the stored error.
  assert.equal(explain({state: 'FAILED_INTERNAL', error: 'boom'}).items[0].message, 'boom');
});
