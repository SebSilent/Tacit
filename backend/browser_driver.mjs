import { createRequire } from 'node:module';
import { writeSync } from 'node:fs';
import process from 'node:process';

const require = createRequire(import.meta.url);
const PKG = process.env.TACIT_PLAYWRIGHT_PATH;
if (!PKG) {
  writeSync(1, JSON.stringify({ id: null, ok: false, error: 'TACIT_PLAYWRIGHT_PATH is not set' }) + '\n');
  process.exit(1);
}

let chromium = null;
let loadError = '';
try {
  ({ chromium } = require(PKG));
} catch (e) {
  loadError = String((e && e.message) || e).slice(0, 300);
}

let context = null;
let browser = null;
let page = null;
let buf = '';

async function ensure() {
  if (page && !page.isClosed()) return page;
  browser = await chromium.launch({ headless: !process.env.TACIT_BROWSER_HEADFUL });
  context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  page = await context.newPage();
  page.setDefaultTimeout(30000);
  return page;
}

async function close() {
  try { if (browser) await browser.close(); } catch { /* */ }
  browser = null;
  context = null;
  page = null;
}

async function handle(cmd) {
  const action = String(cmd.action || '');
  if (loadError) {
    return { error: `playwright could not be loaded from ${PKG}: ${loadError}` };
  }
  if (action === 'open') {
    const p = await ensure();
    const res = await p.goto(String(cmd.url), { waitUntil: 'domcontentloaded', timeout: 45000 });
    return { url: p.url(), status: res ? res.status() : 0, title: await p.title(),
             text: await p.innerText('body') };
  }
  if (action === 'text') {
    if (!page || page.isClosed() || page.url() === 'about:blank') {
      return { error: 'no page is open - call browser(action="open", url=...)' };
    }
    const p = page;
    if (cmd.selector) {
      const el = await p.$(String(cmd.selector));
      if (!el) return { error: `no element matching ${cmd.selector}` };
      return { text: await el.innerText(), url: p.url() };
    }
    return { text: await p.innerText('body'), url: p.url(), title: await p.title() };
  }
  if (action === 'links') {
    if (!page || page.isClosed() || page.url() === 'about:blank') {
      return { error: 'no page is open - call browser(action="open", url=...)' };
    }
    const p = page;
    const rows = await p.$$eval('a[href]', (as) => as.map((a) => ({
      text: (a.innerText || '').trim().slice(0, 120), href: a.href,
    })).filter((x) => x.href && !x.href.startsWith('javascript:')));
    return { links: rows.slice(0, Number(cmd.limit) || 80) };
  }
  if (action === 'click') {
    const p = await ensure();
    await p.click(String(cmd.selector), { timeout: cmd.timeout || 20000 });
    return { url: p.url(), title: await p.title() };
  }
  if (action === 'type') {
    const p = await ensure();
    await p.fill(String(cmd.selector), String(cmd.text ?? ''));
    if (cmd.enter) await p.press(String(cmd.selector), 'Enter');
    return { url: p.url() };
  }
  if (action === 'screenshot') {
    const p = await ensure();
    await p.screenshot({ path: String(cmd.path), fullPage: !!cmd.full });
    return { saved: String(cmd.path) };
  }
  if (action === 'close') {
    await close();
    return { closed: true };
  }
  return { error: `unknown action '${action}'` };
}

function reply(obj) {
  writeSync(1, JSON.stringify(obj) + '\n');
}

process.stdin.setEncoding('utf8');
process.stdin.on('data', async (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    let cmd;
    try { cmd = JSON.parse(line); } catch { continue; }
    try {
      const out = await handle(cmd);
      reply({ id: cmd.id, ok: !out.error, ...out });
    } catch (e) {
      reply({ id: cmd.id, ok: false, error: String((e && e.message) || e).slice(0, 400) });
    }
  }
});

process.stdin.on('end', async () => { await close(); process.exit(0); });
process.on('SIGTERM', async () => { await close(); process.exit(0); });
