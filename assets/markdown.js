/* Tacit's markdown renderer, one copy for the page to load.

   Moved verbatim out of app.js when a second consumer needed the same drawing
   (share the transport and the renderer, fork only the chrome). A second copy of a
   renderer is a second set of escaping bugs, and this one has a test file of its own
   precisely because escaping is where model text becomes HTML:
   tests/test_md_escapes.py extracts the block below and runs it under node.

   app.js keeps three one-line wrappers so its ~40 call sites are untouched.
*/
(function () {
  'use strict';

  function esc(t) { return String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

  // inline markdown on already-escaped text: bold, italic, strike, code, links
  function inlineMd(t) {
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
         .replace(/(^|[\s>])\*([^*\n]+)\*/g, '$1<em>$2</em>')
         .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
         .replace(/`([^`\n]+)`/g, '<code>$1</code>');
    return t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  // pipe-table helpers (GFM-flavoured: header | --- | body rows)
  function splitTableRow(line) {
    let l = line.trim();
    if (l.startsWith('|')) l = l.slice(1);
    if (l.endsWith('|')) l = l.slice(0, -1);
    return l.split('|').map(c => c.trim());
  }
  const TBL_SEP = /^:?-{3,}:?$/;
  const isTableSepRow = cells => cells.length > 0 && cells.every(c => TBL_SEP.test(c));

  function md(src) {
    const blocks = [];
    const tables = [];
    let t = src.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const i = blocks.length;
      blocks.push({ lang, code });
      return `\x00CODE${i}\x00`;
    });
    // extract pipe tables before escaping so cells keep their markdown
    t = t.replace(/(^|\n)((?:[^\S\n]*\|[^\n]*\n?)+)/g, (m, prefix, raw) => {
      const lines = raw.replace(/\n$/, '').split('\n').filter(l => l.includes('|'));
      if (lines.length < 2) return m;
      const head = splitTableRow(lines[0]);
      const sep = splitTableRow(lines[1]);
      if (!isTableSepRow(sep) || sep.length > head.length) return m;
      const aligns = sep.map(c =>
        c.startsWith(':') && c.endsWith(':') ? 'center'
        : c.endsWith(':') ? 'right'
        : c.startsWith(':') ? 'left' : '');
      const rows = lines.slice(2).map(splitTableRow);
      const i = tables.length;
      tables.push({ head, aligns, rows });
      return prefix + `\x00TBL${i}\x00`;
    });
    t = esc(t);
    t = t.replace(/^#### (.*)$/gm, '<h4>$1</h4>')
         .replace(/^### (.*)$/gm, '<h3>$1</h3>')
         .replace(/^## (.*)$/gm, '<h2>$1</h2>')
         .replace(/^# (.*)$/gm, '<h1>$1</h1>');
    t = inlineMd(t);
    t = t.replace(/^&gt; (.*)$/gm, '<blockquote>$1</blockquote>');
    t = t.replace(/(^(?:[-*] .*(?:\n|$))+)/gm, block => {
      const items = block.trim().split(/\n/).map(l => `<li>${l.replace(/^[-*] /, '')}</li>`).join('');
      return `<ul>${items}</ul>\n`;
    });
    t = t.replace(/(^(?:\d+\. .*(?:\n|$))+)/gm, block => {
      const items = block.trim().split(/\n/).map(l => `<li>${l.replace(/^\d+\. /, '')}</li>`).join('');
      return `<ol>${items}</ol>\n`;
    });
    t = t.split(/\n{2,}/).map(p => {
      const pt = p.trim();
      if (/^<(h\d|ul|ol|blockquote|pre|table)/.test(pt)) return p;
      if (/^\x00(?:CODE|TBL)\d+\x00$/.test(pt)) return p;
      return `<p>${p.replace(/\n/g, '<br>')}</p>`;
    }).join('\n');
    t = t.replace(/\x00CODE(\d+)\x00/g, (_, i) => {
      const b = blocks[+i];
      return `<div class="code-block"><div class="code-head"><span>${esc(b.lang || 'code')}</span><button class="copy-btn">Copy</button></div><pre><code>${esc(b.code.replace(/\n$/, ''))}</code></pre></div>`;
    });
    t = t.replace(/\x00TBL(\d+)\x00/g, (_, i) => {
      const b = tables[+i];
      const cell = (c, tag, align) =>
        `<${tag}${align ? ` style="text-align:${align}"` : ''}>${inlineMd(esc(c))}</${tag}>`;
      return `<table><thead><tr>${b.head.map((c, j) => cell(c, 'th', b.aligns[j])).join('')}</tr></thead>` +
        `<tbody>${b.rows.map(r => `<tr>${r.map((c, j) => cell(c, 'td', b.aligns[j])).join('')}</tr>`).join('')}</tbody></table>`;
    });
    return t;
  }

  // ---- end of the renderer; everything below is the export, not the renderer ----
  window.TacitMarkdown = { esc, inlineMd, md };
})();
