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
