const test = require('node:test');
const assert = require('node:assert/strict');
const { choose, arrange, labels, groupKey } = require('../../src/verifier_lab/web/repository-layout.js');

test('neighborhood follows visible edges and keeps guided items within a density limit', () => {
  const nodes = Array.from({ length: 150 }, (_, i) => ({ id: String(i), kind: 'function', path: `f${i}.py` }));
  const edges = nodes.slice(1).map(n => ({ source: '0', target: n.id }));
  const visible = choose(nodes, edges, '0', ['149', '148'], 40, 'neighborhood');
  assert.equal(visible.length, 40);
  assert.deepEqual(visible.slice(0, 3).map(n => n.id), ['0', '149', '148']);
  assert.equal(new Set(visible.map(n => n.id)).size, 40);
  assert.deepEqual(choose(nodes, [], '0', [], 40, 'neighborhood').map(n => n.id), ['0']);
});

test('visible labels prioritize the selected item and omit overlapping text', () => {
  const nodes = [{ id: 'a', kind: 'function', name: 'A long name that will be abbreviated for the map' },
    { id: 'b', kind: 'function', label: 'My marker' }, { id: 'c', kind: 'file', name: 'other.py' }];
  const positions = new Map([['a', [890, 5]], ['b', [890, 5]], ['c', [100, 100]]]);
  const result = labels(nodes, positions, 'a', ['b']);
  assert.equal(result[0].id, 'a');
  assert(result[0].text.endsWith('…'));
  assert(!result.some(item => item.id === 'b'));
  assert(result.every(item => item.x >= 0 && item.x < 900 && item.y >= 0 && item.y <= 570));
});

test('overview samples across files without fabricating nodes or dropping the route', () => {
  const nodes = [{ id: 'repo', kind: 'repository' }];
  for (let i = 0; i < 12; i++) {
    nodes.push({ id: `file:${i}`, kind: 'file', path: `${i}.py` });
    nodes.push({ id: `fn:${i}`, kind: 'function', path: `${i}.py` });
  }
  const visible = choose(nodes, [], 'fn:0', ['fn:11'], 8, 'overview');
  assert.equal(visible.length, 8);
  assert(visible.some(n => n.id === 'fn:11'));
  assert(visible.filter(n => n.kind === 'file').length >= 4);
  assert.deepEqual(visible, choose(nodes, [], 'fn:0', ['fn:11'], 8, 'overview'));
  assert.deepEqual(choose(nodes, [], 'fn:0', ['fn:11'], 1, 'overview').map(n => n.id), ['fn:0', 'fn:11']);
});

test('file and type grouping only changes positions and stays within the canvas', () => {
  const nodes = [{ id: 'r', kind: 'repository' }, { id: 'a', kind: 'function', path: 'a.py' },
    { id: 'b', kind: 'callsite', path: 'a.py' }, { id: 'c', kind: 'function', path: 'b.py' }];
  assert.equal(groupKey(nodes[1], 'file'), groupKey(nodes[2], 'file'));
  assert.equal(groupKey(nodes[1], 'kind'), groupKey(nodes[3], 'kind'));
  for (const grouping of ['none', 'file', 'kind']) {
    const layout = arrange(nodes, grouping);
    assert.equal(layout.positions.size, nodes.length);
    for (const [x, y] of layout.positions.values()) assert(x >= 0 && x <= 900 && y >= 0 && y <= 570);
    assert.deepEqual([...layout.positions], [...arrange(nodes, grouping).positions]);
  }
});
