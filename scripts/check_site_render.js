/**
 * Render the docs through the vendored markdown renderer and check the output.
 *
 * `scripts/build_site.py` verifies that every link resolves. It does not verify that
 * `site/assets/markdown.js` turns a table into a `<table>` or a fence into a code
 * block, because that check needs the same JavaScript the browser runs. This runs it:
 * the renderer is loaded into a VM sandbox exactly as a page would load it, a handful
 * of representative docs are pushed through, and the output is asserted to contain the
 * structures the source has.
 *
 *     node scripts/check_site_render.js
 *
 * Needs Node, so it is run by hand and never by `unittest`: the test suite runs
 * offline with nothing installed beyond requirements.txt, and Tacit does not install
 * Node. It caught two real things the first time it was run. A pipe at the start of a
 * line looked like an unrendered table row when it was an ASCII diagram inside a fence,
 * which was a bug in this check and not in the renderer, and the check that fixed it is
 * the reason `outsideCode()` below exists.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SRC = path.join(ROOT, 'site', 'assets', 'markdown.js');
const MD = path.join(ROOT, 'site', 'md') + path.sep;

const sandbox = { window: {}, console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox);
const render = sandbox.window.TacitMarkdown.md;

// Each entry: a doc that exercises a different part of the renderer. A page with no
// marker present means the renderer changed shape, not that the doc is short.
const cases = [
  ['index.md', ['<h1>Documentation</h1>', '<table>', 'configuration.md']],
  ['configuration.md', ['<h1>Configuration</h1>', 'TACIT_HOME', '<table>']],
  ['README.md', ['<h1>Tacit</h1>', '<table>', 'docs/index.md']],
  ['tools.md', ['<h1>Tools</h1>', 'memory_recall', '<table>']],
  ['testing-and-benchmarks.md', ['<h1>Testing and benchmarks</h1>', 'code-block', 'unittest']],
  ['isolation-and-audit.md', ['<h1>Isolation and audit</h1>', '<table>', 'backend/sandbox.py']],
];

// A pipe at the start of a line is only a failure outside a code block: an ASCII
// diagram inside a fence is content, and it must survive verbatim.
const outsideCode = html => html.replace(/<pre>[\s\S]*?<\/pre>/g, '');

let bad = 0;
for (const [name, markers] of cases) {
  const html = render(fs.readFileSync(MD + name, 'utf8'));
  const missing = markers.filter(m => !html.includes(m));
  const rawPipes = (outsideCode(html).match(/^\|.*$/gm) || []).slice(0, 3);
  const stray = (html.match(/&amp;(amp|lt|gt);/g) || []).slice(0, 3);
  if (missing.length || rawPipes.length || stray.length) {
    bad++;
    console.log('FAIL', name, JSON.stringify({ missing, rawPipes, stray }));
  } else {
    console.log('ok  ', name, html.length, 'chars,',
      (html.match(/<h2>/g) || []).length, 'sections,',
      (html.match(/<table>/g) || []).length, 'tables,',
      (html.match(/class="code-block"/g) || []).length, 'code blocks');
  }
}
process.exit(bad ? 1 : 0);
