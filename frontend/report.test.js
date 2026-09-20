import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Node {
  constructor(tag, text = '') { this.tagName = tag.toUpperCase(); this.children = []; this.textContent = text; this.dataset = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this[name] = value; }
}
function harness() {
  const nodes = new Map();
  const buttons = ['view', 'text'].map(mode => { const button = new Node('button'); button.dataset.reportMode = mode; return button; });
  const context = vm.createContext({el: (tag, text) => new Node(tag, text), document: {createTextNode: text => new Node('#text', text)},
    $: id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); }, all: () => buttons});
  const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  vm.runInContext(source.slice(source.indexOf('function markdownPreview('), source.indexOf('function renderRecheck(')), context);
  return {context, nodes, buttons};
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }

test('preview defaults on, text is exact, toggles survive report refresh, and clearing hides both', () => {
  const {context, nodes, buttons} = harness();
  vm.runInContext("renderReport('## Report\\n\\n**Claim**');", context);
  assert.equal(nodes.get('report').hidden, true);
  assert.equal(nodes.get('report-preview').hidden, false);
  buttons[1].onclick();
  assert.equal(nodes.get('report').hidden, false);
  assert.equal(nodes.get('report-preview').hidden, true);
  assert.equal(nodes.get('report').textContent, '## Report\n\n**Claim**');
  vm.runInContext("renderReport('updated')", context);
  assert.equal(nodes.get('report').hidden, false);
  buttons[0].onclick();
  assert.equal(nodes.get('report-preview').hidden, false);
  vm.runInContext("renderReport('')", context);
  assert.equal(nodes.get('report-preview').hidden, true);
  assert.equal(nodes.get('report').hidden, true);
});

test('formats report tables and fences while leaving HTML and unsafe links inert', () => {
  const {context} = harness();
  context.input = '## Report\n\n| Key | Value |\n|---|---|\n| Impact | **High** |\n\n```js\n<script>alert(1)</script>\n```\n\n<img src=x onerror=alert(1)> [bad](javascript:alert) [good](https://example.com)';
  const nodes = descendants(vm.runInContext('markdownPreview(input)', context));
  for (const tag of ['H2', 'TABLE', 'STRONG', 'PRE', 'CODE']) assert.ok(nodes.some(node => node.tagName === tag));
  assert.equal(nodes.filter(node => node.tagName === 'A').length, 1);
  assert.equal(nodes.find(node => node.tagName === 'A').href, 'https://example.com');
  assert.ok(!nodes.some(node => ['SCRIPT', 'IMG'].includes(node.tagName)));
  assert.ok(nodes.some(node => node.textContent.includes('<img')));
});
