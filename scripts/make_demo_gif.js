// Regenerate the README's demo GIF: record the interface mock, then encode one window of it.
//
// Run by hand from the repo root. It needs Node with Playwright, ffmpeg on PATH, and the
// site built first (`python scripts/build_site.py`), because it records the published page:
//
//     python -m http.server 8021 --directory site      # in one window
//     node scripts\make_demo_gif.js                    # in another
//
// It is a sibling of `check_site_render.js` in kind: Node-only, never called from
// `unittest`, which stays offline and installs nothing.
//
// ── why it records instead of screenshotting ─────────────────────────────────────────
// The first version polled `page.screenshot()` on a timer. That blocks the page's main
// thread, which starves the CSS transitions and the pointer animation being recorded, so
// the GIF of a smooth page came out stepped. `recordVideo` samples in the compositor and
// leaves the page alone. This is the reason the file exists rather than a manual capture.
//
// ── why 1440x810 ─────────────────────────────────────────────────────────────────────
// The top bar is the thing that has to read as still. Measured at this width, the main
// column is 1174px at rest, 775 with the Assistant panel open and 540 with both panels
// open, and the eight controls end 18px inside the window in all three. At 1280 the same
// column hits its 540px floor with the panels at 238px each, which is a cramped app; at
// 1920 the panels keep their 400px but the UI renders smaller on the page. GitHub shows a
// README image at roughly 1012 CSS px, so what matters is the ratio of that display width
// to the layout it was captured from: 1440 to 1152 renders the app at 0.70 of its CSS
// size, where the 760px GIF this replaced, captured at 1280, rendered it at 0.59.
//
// ── why the window is 14s to 26s ──────────────────────────────────────────────────────
// The loop spends its first fourteen seconds idle. Sampling the live page at this width
// once every half second: the ask row appears at 14.5s, the first tool card at 16s, the
// delegation panel opens at 17s, the context meter reads 57% at 16.5s, 58% at 17s, 61% at
// 24.5s and 70% at 25s, and the loop restarts near 30s. A window starting at 16s was built
// and measured against this one: 9% of its frames move against 11% here, and the top bar
// changes in 6% against 9%, so the earlier start is the livelier file.
//
// ── what "verified" means for the icons ──────────────────────────────────────────────
// Encoded frames were read as pixel rows, not looked at. The meter's fill spans 19px of
// its 28px track at 3.5s into the GIF, 20px at 7s, 21px at 11s and 24px at 11.5s, which is
// the 56% to 70% the caption claims, and it is five pixels: real, and easy to miss. The
// right-hand control cluster holds one x position for the whole window except the single
// 233px step at 17s when the delegation panel opens, which is the app responding to a
// panel and not the jitter this file used to be a record of.
const { chromium } = require('@playwright/test');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const VIEWPORT = { width: 1440, height: 810 };
const RECORD_SECONDS = 34;          // one full loop, plus margin for the restart
const WINDOW_START = 14;            // seconds in, where the ask lands
const WINDOW_SECONDS = 12;          // what the README calls twelve seconds
const GIF_FPS = 20;
const GIF_SIZE = { width: 1152, height: 648 };   // 16:9, and 0.8 of the capture
const OUT = path.join(__dirname, '..', 'site', 'assets', 'demo.gif');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'tacit-gif-'));

async function record() {
  const dir = path.join(TMP, 'video');
  fs.mkdirSync(dir);
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir, size: VIEWPORT },   // Playwright writes 25fps regardless
  });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message));
  await page.goto('http://127.0.0.1:8021/demo.html');
  await page.waitForTimeout(RECORD_SECONDS * 1000);
  await ctx.close();            // closing the context is what flushes the webm
  await browser.close();
  const webm = fs.readdirSync(dir).filter((f) => f.endsWith('.webm'))[0];
  if (!webm) throw new Error('no video written to ' + dir);
  if (errs.length) throw new Error('the page threw during recording: ' + errs.join('; '));
  return path.join(dir, webm);
}

function encode(webm) {
  // stats_mode=diff is not decoration. A palette generated over the whole frame spends its
  // 256 entries on the transcript's background and discards the few brightness steps the
  // context meter moves through; generated over what changes, it keeps them. Dithering off,
  // because a dithered 1px-tall bar reads as noise in a GIF and as nothing at all at 0.7x.
  const vf = [
    'fps=' + GIF_FPS,
    'scale=' + GIF_SIZE.width + ':' + GIF_SIZE.height + ':flags=lanczos',
    'split[a][b]',
    '[a]palettegen=stats_mode=diff:max_colors=256[p]',
    '[b][p]paletteuse=dither=none',
  ].join(',');
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-ss', String(WINDOW_START),
    '-t', String(WINDOW_SECONDS), '-i', webm, '-vf', vf, '-gifflags', '+transdiff', OUT],
  { stdio: 'inherit' });
}

(async () => {
  const webm = await record();
  console.log('recorded', webm, fs.statSync(webm).size.toLocaleString(), 'bytes');
  encode(webm);
  const frames = execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height,nb_frames', '-of', 'csv=p=0', OUT],
  { encoding: 'utf8' }).trim();
  console.log('wrote', OUT, fs.statSync(OUT).size.toLocaleString(), 'bytes;', frames);
  console.log('frames are 1/' + GIF_FPS + 's apart, so the file runs ' +
    (Number(frames.split(',')[2]) / GIF_FPS) + 's');
})().catch((e) => { console.error(String(e.message || e)); process.exit(1); });
