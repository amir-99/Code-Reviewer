import assert from 'node:assert/strict';
import {matchesFinding} from './findings.js';

const findings = [
  {severity: 'REQUIRED', impact_level: 'HIGH'},
  {severity: 'REQUIRED', impact_level: 'LOW'},
  {severity: 'FYI', impact_level: 'HIGH'},
  {severity: 'NIT'},
];
assert.deepEqual(findings.filter(f => matchesFinding(f, 'REQUIRED', 'HIGH')), [findings[0]]);
assert.deepEqual(findings.filter(f => matchesFinding(f, 'all', 'HIGH')), [findings[0], findings[2]]);
assert.deepEqual(findings.filter(f => matchesFinding(f, 'all', 'unknown')), [findings[3]]);
assert.deepEqual(findings.filter(f => matchesFinding(f, 'REQUIRED', 'all')), findings.slice(0, 2));
assert.deepEqual(findings.filter(f => matchesFinding(f, 'all', 'all')), findings);
assert.equal(matchesFinding({severity: 'NIT', impact_level: null}, 'NIT', 'unknown'), true);

const {commentActions, bulkCommentKeys} = await import('./findings.js');
assert.deepEqual(commentActions({status: 'drafted'}, true), ['edit', 'remove']);
assert.deepEqual(commentActions({status: 'committed', thread_status: 'open'}, true), ['edit', 'remove', 'resolve']);
assert.deepEqual(commentActions({status: 'committed', thread_status: 'not_applicable'}, true), ['edit', 'remove']);
assert.deepEqual(commentActions({status: 'removed'}, true), []);
assert.deepEqual(commentActions({status: 'drafted'}, false), []);
assert.deepEqual(commentActions({status: 'committed', conflict: 'changed'}, true), []);
const comments = [
  {key: 'open', status: 'committed', thread_status: 'open'},
  {key: 'resolved', status: 'committed', thread_status: 'resolved'},
  {key: 'draft', status: 'drafted'},
  {key: 'removed', status: 'removed'},
  {key: 'summary', status: 'not_published', body: 'Report'},
  {key: 'summary-only', status: 'not_published', body: ''},
  {key: 'pending-delete', status: 'drafted', intent: 'remove'},
];
assert.deepEqual(bulkCommentKeys(comments, 'resolve_all'), ['open']);
assert.deepEqual(bulkCommentKeys(comments, 'publish_all'), ['draft', 'summary']);
