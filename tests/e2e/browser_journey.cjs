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
  await page.click('button:has-text("Select all")');
  await page.locator('li', { hasText: 'service-config.json' }).locator('input').check();
  await page.locator('li', { hasText: 'schema.sql' }).locator('input').check();
  await page.click('button:has-text("Start run")');
  await page.waitForSelector('text=Open findings', { timeout: 60000 });
  await page.click('text=Open findings >> nth=0');

  // 4. findings
  await page.waitForSelector('table tbody tr');
  assert.strictEqual(await page.locator('table tbody tr').count(), KEYS.length);
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
  await page.click('button:has-text("Select all")');
  await page.locator('li', { hasText: 'service-config.json' }).locator('input').check();
  await page.locator('li', { hasText: 'schema.sql' }).locator('input').check();
  await page.click('summary:has-text("Risk packs")');
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

  // 4e. purpose assessment from the UI: a torn package (two generations) is CONTRADICTED for the strong purpose
  const PUR = path.resolve(__dirname, '../../fixtures/purposes/') + '/';
  await page.goto(BASE + '#/import');
  await page.setInputFiles('#files', ['map.json', 'counters-g6.json', 'history.json'].map((f) => PUR + f));
  await page.click('button[type=submit]');
  await page.waitForFunction(() => !/QUEUED|PARSING/.test(document.querySelector('main').innerText) && /counters-g6\.json/.test(document.querySelector('main').innerText), null, { timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.goto(BASE + '#/run');
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

  // 5. export + independent checker
  await page.goto(BASE + '#/export');
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('button:has-text("Download package")')]);
  assert.ok((await download.suggestedFilename()).endsWith('.zip'));
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
})().catch((e) => { console.error('browser journey FAILED:', e.message); process.exit(1); });
