import test from 'node:test';
import assert from 'node:assert/strict';
import {runControls} from '../public/controls.js';

const allowed = {allowed: true, reason: null};
const refused = reason => ({allowed: false, reason});

test('a failed review offers both, and retry is the one it can repeat', () => {
  const view = runControls({state: 'FAILED_INTERNAL', controls: {rerun: allowed, retry: allowed}});
  assert.deepEqual(view.retry, {show: true, enabled: true, reason: ''});
  assert.deepEqual(view.rerun, {show: true, enabled: true, reason: '', confirm: false});
});

test('a refused retry on a failed review stays visible with its reason', () => {
  const reason = 'A later review has run since this one; rerun instead';
  const view = runControls({state: 'FAILED_CONTEXT', controls: {rerun: allowed, retry: refused(reason)}});
  assert.deepEqual(view.retry, {show: true, enabled: false, reason});
});

test('retry is hidden on reviews that did not fail, and rerun confirms while running', () => {
  const running = runControls({state: 'DEFECT_REVIEW', controls: {rerun: allowed, retry: refused('The review is still running')}});
  assert.equal(running.retry.show, false);
  assert.equal(running.rerun.confirm, true);
  const published = runControls({state: 'PUBLISHED', controls: {rerun: allowed, retry: refused('Only a failed review can be retried')}});
  assert.equal(published.retry.show, false);
  assert.equal(published.rerun.confirm, false);
});

test('readers who cannot execute, and responses without controls, show nothing', () => {
  const reason = "Only the review's owner can run it again";
  const admin = runControls({state: 'FAILED_INTERNAL', controls: {rerun: refused(reason), retry: refused(reason)}});
  assert.equal(admin.rerun.show, false);
  assert.equal(admin.retry.show, false);
  const legacy = runControls({state: 'FAILED_INTERNAL', capabilities: {execute: true}});
  assert.equal(legacy.stop.show, false);
  assert.equal(legacy.rerun.show, false);
  assert.equal(legacy.retry.show, false);
});

test('stop is offered while a review runs, and explains itself while it publishes', () => {
  const running = runControls({state: 'DEFECT_REVIEW', controls: {stop: allowed, rerun: allowed, retry: refused('The review is still running')}});
  assert.deepEqual(running.stop, {show: true, enabled: true, reason: ''});
  const publishing = runControls({state: 'DECISION', controls: {stop: refused('The review is publishing its report'), rerun: allowed, retry: refused('x')}});
  assert.deepEqual(publishing.stop, {show: true, enabled: false, reason: 'The review is publishing its report'});
  const finished = runControls({state: 'PUBLISHED', controls: {stop: refused('The review has finished'), rerun: allowed, retry: refused('x')}});
  assert.equal(finished.stop.show, false);
  const reader = runControls({state: 'DEFECT_REVIEW', controls: {stop: refused('owner'), rerun: refused('owner'), retry: refused('owner')}});
  assert.equal(reader.stop.show, false);
});
