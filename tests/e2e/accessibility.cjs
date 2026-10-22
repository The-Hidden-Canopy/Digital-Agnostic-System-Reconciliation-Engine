/* Trusted browser-test instrumentation; never shipped in the application. */
const fs = require('fs');
const assert = require('assert');
const source = process.env.AXE_MODULE ? fs.readFileSync(require.resolve(process.env.AXE_MODULE), 'utf8') : null;
module.exports = async function audit(page, name) {
  if (!source) return;
  await page.evaluate(source);
  const result = await page.evaluate(async () => {
    return await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } });
  });
  const failures = result.violations.map((violation) => ({ id: violation.id, impact: violation.impact,
    description: violation.description, nodes: violation.nodes.map((node) => ({ target: node.target, failure: node.failureSummary })) }));
  assert.deepStrictEqual(failures, [], `Accessibility violations on ${name}: ${JSON.stringify(failures, null, 2)}`);
  console.log('accessibility: ' + JSON.stringify({ page: name, engine: result.testEngine, passed_rules: result.passes.length,
    manual_review: result.incomplete.map((item) => ({ id: item.id, targets: item.nodes.map((node) => node.target) })) }));
};
