import test from 'node:test';
import assert from 'node:assert/strict';
import {identity, identityNode} from '../public/identity.js';

const document = {createElement() { return {textContent: '', children: [], append(...nodes) {this.children.push(...nodes);}}; }};
test('project paths take precedence, ids remain muted, names are text', () => {
  const review = {project_id: 7, mr_iid: 2, project_path: 'group/<script>', project_name: 'Project'};
  const node = identityNode(review, document);
  assert.equal(node.textContent, 'group/<script>');
  assert.equal(node.children[0].className, 'muted');
  assert.equal(node.children[0].textContent, ' (7)');
  assert.equal(identity(review), 'group/<script> · !2');
});
test('legacy projects and documents retain useful fallback labels', () => {
  assert.equal(identityNode({project_id: 7, mr_iid: 2}, document).textContent, 'Project 7 · !2');
  assert.equal(identity({kind: 'document', subject: {space: 'DOC', title: 'Plan'}}), 'DOC · Plan');
});
