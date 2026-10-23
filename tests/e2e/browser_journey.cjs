/* Browser journey for the workbench (not part of `unittest`; needs Playwright + Chromium).
 *
 *   verifier-lab --workspace /tmp/ws serve --port 8765 &
 *   WORKBENCH_URL=http://127.0.0.1:8765/ node tests/e2e/browser_journey.cjs
 *
 * Covers import -> review -> run -> findings -> export -> independent checker through the real UI,
 * plus a keyboard-only pass over the five-step navigation. Exits non-zero on the first failed assertion.
 */
const path = require('path');
const assert = require('assert');
const audit = require('./accessibility.cjs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const BASE = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765/';
const FIX = path.resolve(__dirname, '../../fixtures/tutorial/') + '/';
const KEYS = ['limits.max_upload_kb', 'limits.retries', 'timeout.seconds', 'region', 'audit.enabled', 'sql.table.users.column.email.not_null'];
const EXPECT = { SUPPORTED: 4, CONTRADICTED: 1, INSUFFICIENT_EVIDENCE: 1 };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });

  // 1. import (SQL dialect declared, never guessed)
  await page.goto(BASE + '#/import');
  await page.setInputFiles('#files', ['product-spec.md', 'service-config.json', 'schema.sql'].map((f) => FIX + f));
  await page.selectOption('#dialect', 'postgresql');
  await page.click('button[type=submit]');
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length >= 3 && !/QUEUED|PARSING/.test(document.querySelector('main').innerText), null, { timeout: 30000 });
  await page.waitForFunction(() => /PARTIALLY_PARSED/.test(document.querySelector('main').innerText));  // markdown HTML comment stays visible as unsupported
  await audit(page, 'import');
  if (process.env.AXE_MODULE) {
    await page.evaluate(() => { const control = document.createElement('button'); control.id = 'axe-negative-control'; document.body.append(control); });
    await assert.rejects(() => audit(page, 'negative-control'), /button-name/);
    await page.evaluate(() => document.getElementById('axe-negative-control').remove());
    console.log('accessibility negative control: detected');
  }

  // 2. review: approve the six spec claims one by one
  for (const k of KEYS) {
    await page.goto(BASE + '#/review');
    await page.selectOption('#f-st', 'PENDING');
    await page.fill('#f-q', k); await page.dispatchEvent('#f-q', 'change');
    try {
      await page.waitForFunction((key) => { const b = document.querySelector('.list button.item'); return b && b.innerText.split('\n')[0].trim() === key; }, k, { timeout: 10000 });
    } catch (e) {
      console.error('list never showed', k, '| q =', await page.inputValue('#f-q'), '| visible:', (await page.innerText('main')).slice(0, 700).replace(/\n/g, ' | '));
      throw e;
    }
    const item = page.locator('.list li', { hasText: 'product-spec.md' }).locator('button.item').first();
    await item.click();
    await page.waitForFunction((key) => { const h = document.querySelector('#cand-h'); return h && h.textContent === key; }, k, { timeout: 10000 });
    assert.ok((await page.textContent('mark')).length > 0, 'source token is highlighted');
    if (k === KEYS[0]) await audit(page, 'review');
    await page.click('button:has-text("Record decision")');
    try {
      await page.waitForFunction((key) => !Array.from(document.querySelectorAll('.list button.item')).some((b) => b.innerText.startsWith(key) && b.innerText.includes('product-spec.md')), k, { timeout: 10000 });
    } catch (e) {
      console.error('decision for', k, 'did not take effect | visible:', (await page.innerText('main')).slice(0, 1200).replace(/\n/g, ' | '));
      throw e;
    }
  }

  // 3. run
  await page.goto(BASE + '#/run');
  await page.waitForSelector('summary:has-text("Risk packs")');       // the page renders after several parallel fetches
  await audit(page, 'run');
  await page.locator('details:has(summary:has-text("Risk packs"))').evaluate((d) => (d.open = true));
  await page.fill('#ctx', 'service.kind = web');
  await page.click('button:has-text("Select all")');                     // must not wipe what is already filled in
  assert.strictEqual(await page.inputValue('#ctx'), 'service.kind = web');
  await page.locator('li', { hasText: 'service-config.json' }).locator('input').check();
  await page.locator('li', { hasText: 'schema.sql' }).locator('input').check();
  await page.click('button:has-text("Start run")');
  await page.waitForSelector('text=Open findings', { timeout: 60000 });
  await page.click('text=Open findings >> nth=0');

  // 4. findings
  await page.waitForSelector('table tbody tr');
  assert.strictEqual(await page.locator('table tbody tr').count(), KEYS.length);
  await audit(page, 'findings');
  for (const [verdict, n] of Object.entries(EXPECT)) {
    const label = { SUPPORTED: 'Supported', CONTRADICTED: 'Contradicted', INSUFFICIENT_EVIDENCE: 'Insufficient evidence' }[verdict];
    assert.strictEqual(await page.locator('tbody tr', { hasText: label }).count(), n, verdict);
  }
  await page.locator('tr', { hasText: 'limits.max_upload_kb' }).locator('a:has-text("Inspect")').click();
  await page.waitForSelector('#f-h');
  assert.ok((await page.textContent('#fdetail')).includes('service-config.json'), 'evidence source is named');
  assert.strictEqual(await page.textContent('#fdetail mark'), '200');

  // 4b. risk pack with stated context: every obligation shown, applicability facts cited, unknown stays unknown
  await page.goto(BASE + '#/run');
  await page.waitForSelector('summary:has-text("Risk packs")');       // the page renders after several parallel fetches
  await page.locator('details:has(summary:has-text("Risk packs"))').evaluate((d) => (d.open = true));
  await page.fill('#ctx', 'service.kind = web');
  await page.click('button:has-text("Select all")');                     // must not wipe what is already filled in
  assert.strictEqual(await page.inputValue('#ctx'), 'service.kind = web');
  await page.locator('li', { hasText: 'service-config.json' }).locator('input').check();
  await page.locator('li', { hasText: 'schema.sql' }).locator('input').check();
  await page.locator('details:has(summary:has-text("Risk packs"))').evaluate((d) => (d.open = true));
  await page.locator('label', { hasText: 'Synthetic upload-service controls' }).locator('input').check();
  await page.fill('#ctx', ['service.kind = web', 'service.accepts_uploads = true', 'service.makes_remote_calls = true',
    'data.stores_personal_email = true', 'service.mutates_state = true', 'service.tier = modern'].join('\n'));
  await page.click('button:has-text("Start run")');
  await page.waitForSelector('text=Open findings', { timeout: 60000 });
  await page.click('text=Open findings >> nth=0');
  await page.waitForSelector('text=Risk-pack obligations');
  const obRows = page.locator('h3:has-text("synthetic-upload-controls") ~ .tablewrap tbody tr');
  assert.strictEqual(await obRows.count(), 6);
  const rowFor = (id) => page.locator('tr', { has: page.locator('code', { hasText: new RegExp('^' + id + '$') }) });
  assert.ok((await rowFor('UPLOAD-LIMIT').innerText()).includes('Contradicted'));
  assert.ok((await rowFor('REQUIRED-EMAIL').innerText()).includes('Supported'));
  assert.ok((await rowFor('LEGACY-MODE').innerText()).includes('Not applicable'));
  assert.ok((await rowFor('LEGACY-MODE').innerText()).includes('service.tier="modern"'));      // cites the facts that establish it
  assert.ok((await rowFor('DATA-CLASS').innerText()).includes('Human review required'));        // unknown applicability is not "not applicable"
  assert.ok((await rowFor('DATA-CLASS').innerText()).includes('applicability UNKNOWN'));

  // 4c. advanced page: inject a defect, minimize it, see the fresh-process verification and the scope statement
  await page.goto(BASE + '#/advanced');
  await page.selectOption('#x-defect', 'reset_next_id');
  await page.fill('#x-seed', 'seed-3');
  await page.click('button:has-text("Minimize the failure")');
  await page.waitForSelector('text=Fresh-process reproduction: VERIFIED', { timeout: 60000 });
  assert.ok((await page.innerText('main')).includes('not global minimality'));
  await page.click('button:has-text("Enumerate every crash position")');
  await page.waitForSelector('text=All recoveries valid');

  for (const [system, defectName] of [['pipeline', 'stale_cache'], ['scheduler', 'reverse_ties']]) {
    await page.selectOption('#x-system', system);
    await page.fill('#x-ops', '12');
    await page.click('button:has-text("Run three-way experiment")');
    await page.waitForSelector('text=No divergence');
    await page.selectOption('#x-defect', defectName);
    await page.click('button:has-text("Run three-way experiment")');
    await page.waitForSelector('.notice.bad');
    assert.ok((await page.innerText('main')).includes('state.'));
    await page.click('button:has-text("Minimize the failure")');
    await page.waitForSelector('text=Fresh-process reproduction: VERIFIED', { timeout: 60000 });
    const [counterexample] = await Promise.all([page.waitForEvent('download'), page.click('text=Download counterexample')]);
    const stored = JSON.parse(require('fs').readFileSync(await counterexample.path(), 'utf8'));
    assert.strictEqual(stored.schema, 'verifier-lab/laboratory-counterexample/1');
    assert.strictEqual(stored.reduced.case.system, system);
  }
  await audit(page, 'advanced');

  // 4d. campaign page: the real verifier passes; a defective one fails visibly; a forged report is rejected
  await page.goto(BASE + '#/campaign');
  await page.click('button:has-text("Run campaign")');
  await page.waitForSelector('text=Campaign passed');
  assert.ok((await page.innerText('main')).includes('every operator and relation ran at least once'));
  await page.selectOption('#k-kernel', 'always_supported');
  await page.click('button:has-text("Run campaign")');
  await page.waitForSelector('text=Campaign FAILED');
  await page.click('button:has-text("Forge the passing total")');
  await page.waitForSelector('text=Forged report: REJECTED');
  await audit(page, 'campaign');

  // 4e. purpose assessment from the UI: a torn package (two generations) is CONTRADICTED for the strong purpose
  const PUR = path.resolve(__dirname, '../../fixtures/purposes/') + '/';
  await page.goto(BASE + '#/import');
  await page.setInputFiles('#files', ['map.json', 'counters-g6.json', 'history.json'].map((f) => PUR + f));
  await page.click('button[type=submit]');
  await page.waitForFunction(() => ['map.json', 'counters-g6.json', 'history.json'].every((name) =>
    [...document.querySelectorAll('main table tbody tr')].some((row) => row.innerText.includes(name) && row.innerText.includes('EXTRACTED'))),
  null, { timeout: 60000 });
  await page.goto(BASE + '#/run');
  await page.waitForSelector('summary:has-text("Purpose")');
  for (const n of ['map.json', 'counters-g6.json', 'history.json']) await page.locator('li', { hasText: n }).locator('input').check();
  await page.click('summary:has-text("Purpose (optional)")');
  await page.locator('label', { hasText: 'Resume exactly from a checkpoint' }).locator('input').check();
  const BIND = { entries_count: 'entries_count', allocator_next: 'allocator_next', random_cursor: 'random_cursor', operation_cursor: 'operation_cursor', semantics_version: 'semantics_version' };
  for (const [r, k] of Object.entries(BIND)) await page.fill(`#role-resume-exactly-${r}`, 'checkpoint.' + k);
  await page.click('button:has-text("Start run")');
  await page.waitForSelector('text=Open findings', { timeout: 60000 });
  await page.click('text=Open findings >> nth=0');
  await page.waitForSelector('text=Purpose assessments');
  assert.ok((await page.innerText('main')).includes('sources from different generations'));
  assert.ok((await page.innerText('main')).includes('g6'));

  // 4f. composition page: units mismatch is contradicted with the edge named; the converted version composes; targeting scopes it
  const COMP = path.resolve(__dirname, '../../fixtures/composition/') + '/';
  await page.goto(BASE + '#/import');
  await page.setInputFiles('#files', ['units-mismatch.json', 'units-converted.json', 'scoped.json'].map((f) => COMP + f));
  await page.click('button[type=submit]');
  await page.waitForFunction(() => ['units-mismatch.json', 'units-converted.json', 'scoped.json'].every((name) =>
    [...document.querySelectorAll('main table tbody tr')].some((row) => row.innerText.includes(name) && row.innerText.includes('EXTRACTED'))),
  null, { timeout: 60000 });
  await page.goto(BASE + '#/composition');
  await page.waitForSelector('#c-src');
  await page.selectOption('#c-src', { label: 'units-mismatch.json' });
  await page.waitForSelector('text=gives "milliseconds"');
  assert.ok((await page.innerText('main')).includes('CONTRADICTED'));
  await page.selectOption('#c-src', { label: 'units-converted.json' });
  await page.waitForSelector('.notice.ok');
  await page.selectOption('#c-src', { label: 'scoped.json' });
  await page.waitForSelector('text=INSUFFICIENT EVIDENCE');
  await page.selectOption('#c-target', { label: 'only ancestors of report' });
  await page.waitForSelector('.notice.ok');
  assert.ok((await page.innerText('main')).includes('unresolved, not verified'));
  await audit(page, 'composition');

  // 4g. PDF follows the same review/run path; image-only pages stay visibly partial.
  await page.goto(BASE + '#/import');
  await page.setInputFiles('#files', FIX + 'ops-note.pdf');
  await page.click('button[type=submit]');
  await page.waitForFunction(() => {
    const row = [...document.querySelectorAll('tbody tr')].find((r) => r.innerText.includes('ops-note.pdf'));
    return row && row.innerText.includes('PARTIALLY_PARSED');
  }, null, { timeout: 30000 });
  await page.goto(BASE + '#/review');
  await page.selectOption('#f-st', 'PENDING');
  await page.fill('#f-q', 'limits.max_upload_kb');
  await page.dispatchEvent('#f-q', 'change');
  await page.locator('.list li', { hasText: 'ops-note.pdf' }).locator('button.item').click();
  await page.waitForSelector('#cand-h');
  assert.strictEqual(await page.textContent('mark'), '150');
  await page.click('button:has-text("Record decision")');
  await page.waitForFunction(() => ![...document.querySelectorAll('.list li')].some((r) => r.innerText.includes('ops-note.pdf')));
  await page.goto(BASE + '#/run');
  await page.waitForSelector('summary:has-text("Risk packs")');
  const checked = page.locator('main input[type=checkbox]:checked');
  while (await checked.count()) await checked.first().uncheck();
  await page.locator('li', { hasText: 'limits.max_upload_kb fact.equals 150' }).locator('input').check();
  await page.locator('li', { hasText: 'service-config.json' }).locator('input').check();
  await page.click('button:has-text("Start run")');
  await page.waitForSelector('text=Open findings', { timeout: 60000 });
  await page.click('text=Open findings >> nth=0');
  await page.waitForSelector('table tbody tr');
  assert.strictEqual(await page.locator('table tbody tr').count(), 1);
  assert.ok((await page.innerText('table tbody')).includes('Contradicted'));
  await page.locator('tr', { hasText: 'limits.max_upload_kb' }).locator('a:has-text("Inspect")').click();
  await page.waitForSelector('#f-h');
  assert.strictEqual(await page.textContent('#fdetail mark'), '200');

  // 5. export the PDF-backed claim + independent checker
  await page.goto(BASE + '#/export');
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('button:has-text("Download package")')]);
  assert.ok((await download.suggestedFilename()).endsWith('.zip'));
  await page.click('button:has-text("Verify with the independent checker")');
  await page.waitForSelector('.notice.ok, .notice.bad');
  assert.strictEqual(await page.textContent('.notice.ok strong'), 'VERIFIED');
  await audit(page, 'export');

  // 5b. A human can propose a claim from prose; source selection stays distinct from evidence.
  const authoredText = 'Résumé: the maximum allowed quantity is one hundred.\n';
  await page.goto(BASE + '#/import');
  await page.setInputFiles('#files', [
    { name: 'reviewer-note.md', mimeType: 'text/markdown', buffer: Buffer.from(authoredText) },
    { name: 'reviewer-evidence.json', mimeType: 'application/json', buffer: Buffer.from('{"author":{"limit":200}}') }
  ]);
  await page.click('button[type=submit]');
  await page.waitForFunction(() => !/QUEUED|PARSING/.test(document.querySelector('main').innerText) && /reviewer-note\.md/.test(document.querySelector('main').innerText), null, { timeout: 30000 });
  await page.locator('a', { hasText: 'reviewer-note.md' }).click();
  await page.locator('summary', { hasText: 'Create a claim from this text' }).click();
  await page.locator('#a-source').evaluate((el) => { el.focus(); el.setSelectionRange(8, el.value.length - 1); });
  await page.click('button:has-text("Use selected passage")');
  assert.ok((await page.textContent('#a-selected')).startsWith('the maximum'));
  await page.fill('#a-subject', 'author.limit');
  await page.selectOption('#a-kind', 'integer');
  await page.fill('#a-value', '100');
  await page.fill('#a-reason', 'Interpret the written quantity as an exact maximum.');
  await audit(page, 'authoring');
  await page.click('button:has-text("Create candidate for review")');
  await page.waitForSelector('#cand-h');
  assert.strictEqual(await page.textContent('#cand-h'), 'author.limit');
  assert.ok((await page.innerText('main')).includes('Semantic entailment from this passage is not machine-verified'));
  assert.ok((await page.textContent('mark')).startsWith('the maximum'));
  await page.click('button:has-text("Record decision")');
  await page.waitForFunction(() => ![...document.querySelectorAll('.list li')].some((el) => el.innerText.includes('author.limit') && el.innerText.includes('reviewer-note.md')));
  await page.goto(BASE + '#/run');
  await page.waitForSelector('summary:has-text("Risk packs")');
  while (await checked.count()) await checked.first().uncheck();
  await page.locator('li', { hasText: 'author.limit fact.equals 100' }).locator('input').check();
  await page.locator('li', { hasText: 'reviewer-evidence.json' }).locator('input').check();
  await page.click('button:has-text("Start run")');
  await page.waitForSelector('text=Open findings', { timeout: 60000 });
  await page.click('text=Open findings >> nth=0');
  await page.waitForSelector('table tbody tr');
  assert.strictEqual(await page.locator('table tbody tr').count(), 1);
  assert.ok((await page.innerText('table tbody')).includes('Contradicted'));
  await page.goto(BASE + '#/export');
  await page.click('button:has-text("Verify with the independent checker")');
  await page.waitForSelector('.notice.ok, .notice.bad');
  assert.strictEqual(await page.textContent('.notice.ok strong'), 'VERIFIED');

  // 6. keyboard only: first Tab stop is the skip link; Enter moves focus to <main>; Tab order then walks the five steps
  let kp = await browser.newPage();
  kp.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  await kp.goto(BASE + '#/import');
  await kp.waitForSelector('h1');
  await kp.keyboard.press('Tab');
  assert.strictEqual(await kp.evaluate(() => document.activeElement.className), 'skip');
  await kp.keyboard.press('Enter');
  assert.strictEqual(await kp.evaluate(() => document.activeElement.id), 'main');
  await kp.close();
  kp = await browser.newPage();
  await kp.goto(BASE + '#/import');
  await kp.waitForSelector('h1');
  const order = [];
  for (let i = 0; i < 8; i++) {
    await kp.keyboard.press('Tab');
    const step = await kp.evaluate(() => document.activeElement.dataset.step || '');
    if (step) order.push(step);
    if (step === 'findings') break;
  }
  assert.deepStrictEqual(order, ['import', 'review', 'run', 'findings']);
  await kp.keyboard.press('Enter');
  await kp.waitForFunction(() => { const cur = document.querySelector('[aria-current=page]'); return cur && cur.dataset.step === 'findings'; });

  await kp.close();

  // 7. deep link survives refresh
  const link = await page.evaluate(() => location.hash);
  await page.reload();
  await page.waitForSelector('h1');
  assert.strictEqual(await page.evaluate(() => location.hash), link);

  assert.deepStrictEqual(problems, [], problems.join('\n'));
  console.log('browser journey: OK');
  await browser.close();
})().catch((e) => { console.error('browser journey FAILED:', e.stack || e.message); process.exit(1); });
