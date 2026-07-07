// Browser smoke test: boots against a running server (default :8778), loads
// the page, clicks every tab, opens the status panel, and fails on any page
// or console error. Works offline — every tab degrades gracefully without
// network, so this only asserts the frontend itself is healthy.
//
// Usage:  node tests/smoke.mjs
//   PORT=8778           server port (default 8778)
//   PW_CHROMIUM=/path   use an existing Chromium instead of the bundled one

let chromium;
try {
  ({ chromium } = await import('playwright'));
} catch {
  ({ chromium } = await import('playwright-core'));
}

const PORT = process.env.PORT || '8778';
const BASE = `http://localhost:${PORT}`;
const TABS = ['youtube', 'shorts', 'ai', 'reels', 'x', 'threads', 'tiktok', 'saved'];

const browser = await chromium.launch({
  executablePath: process.env.PW_CHROMIUM || undefined,
});
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
const errors = [];
page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
page.on('console', m => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1200);

for (const tab of TABS) {
  await page.click(`.tab[data-tab="${tab}"]`);
  await page.waitForTimeout(600);
  const visible = await page.evaluate(() => {
    const views = ['videoView', 'aiView', 'reelsView', 'xView', 'threadsView', 'tiktokView', 'savedView'];
    return views.filter(id => document.getElementById(id).style.display !== 'none');
  });
  if (visible.length !== 1) errors.push(`tab ${tab}: expected exactly one visible view, got [${visible}]`);
}

// Status panel opens and renders all source rows
await page.click('#statusBtn');
await page.waitForTimeout(600);
const rows = await page.locator('#statusPanel .st-row').count();
if (rows < 7) errors.push(`status panel: expected 7 source rows, got ${rows}`);

// Region selector is populated and set to a valid region
const regionOpts = await page.locator('#regionSel option').count();
if (regionOpts < 5) errors.push(`region selector: expected several options, got ${regionOpts}`);

// The sort menu opens and contains the Hot option
await page.click('.tab[data-tab="youtube"]');
await page.click('#vidSortMenu .sorttoggle');
const hot = await page.locator('#vidSortMenu .sortlist button', { hasText: 'Hot' }).count();
if (!hot) errors.push('video sort menu: Hot option missing');

await browser.close();

// The favicon request is served, so any console error is a real defect.
if (errors.length) {
  console.error('SMOKE TEST FAILED:');
  errors.forEach(e => console.error(' -', e));
  process.exit(1);
}
console.log('Smoke test passed: all tabs render, status panel OK, no page errors.');
