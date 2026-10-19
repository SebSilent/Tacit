/*
 * The interface, with no server behind it.
 *
 * `demo.html` is the app's own markup and the app's own stylesheet: `assets/app.css`
 * is a byte copy of `static/style.css`, and every class this file writes is a class
 * `static/app.js` writes to the same node. Nothing here restyles a surface, so what
 * you see is the thing rather than a picture of the thing. The invented part is the
 * transcript, which is a scripted turn; the theme list and the prompt texts are
 * copied out of the repository by `scripts/build_site.py` at build time.
 *
 * It plays on its own, and it answers you: type in the composer and press Enter.
 */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const md = (t) => (window.TacitMarkdown ? window.TacitMarkdown.md(t) : t);
  const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const clock = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const fmtTok = (n) => (n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n | 0));
  const comma = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');

  const messagesEl = $('#messages');
  const composerEl = $('#input');
  const tokMeter = $('#tokMeter'), tokCount = $('#tokCount'),
        tokFill = $('#tokFill'), tokPct = $('#tokPct'), tokAuto = $('#tokAuto');
  const planStrip = $('#planStrip');
  const taskStrip = $('#taskStrip');
  const asPanel = $('#asPanel'), dgPanel = $('#dgPanel');

  const CTX = 128000, RESERVE = 12000;

  const S = {
    busy: false,
    paused: false,
    speed: 1,
    tokens: 0,
    compacting: false,
    autoCompact: true,
    session: 'shell-timeout',
    run: 0,
  };

  // ── timing ─────────────────────────────────────────────────────────────
  // One gate decides whether the script may move, so Pause stops the animation
  // without losing its place and the speed control changes every wait at once.
  function wait(ms) {
    return new Promise((res) => {
      const step = () => {
        if (S.paused) { setTimeout(step, 90); return; }
        setTimeout(res, Math.max(4, ms / S.speed));
      };
      step();
    });
  }

  function scrollBottom(force) {
    const atEnd = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 120;
    if (force || atEnd || S.busy) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ── context meter ──────────────────────────────────────────────────────
  function setTokens(n) {
    S.tokens = Math.max(0, n);
    tokMeter.hidden = false;
    tokCount.textContent = fmtTok(S.tokens) + ' tok';
    tokAuto.hidden = !S.autoCompact;
    tokMeter.classList.toggle('compacting', !!S.compacting);
    const pct = Math.max(0, Math.min(100, (S.tokens / CTX) * 100));
    tokPct.textContent = S.compacting ? 'compacting…' : pct.toFixed(0) + '%';
    tokFill.style.width = pct + '%';
    const compactAt = (1 - RESERVE / CTX) * 100;
    tokFill.className = 'tok-fill' + (pct >= compactAt ? (S.autoCompact ? ' auto-zone' : ' alert')
      : pct >= compactAt - 20 ? ' warn' : '');
  }

  let toastEl = null, toastTimer = 0;
  function toast(text, isError) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.style.borderColor = isError ? 'var(--err)' : 'var(--line-2)';
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), isError ? 5000 : 2600);
  }

  // ── transcript rows ────────────────────────────────────────────────────
  // The app keeps rows inside `.messages-inner`, which is what carries the measure,
  // so the mock creates it the same way and appends to it.
  function inner() {
    let el = messagesEl.querySelector('.messages-inner');
    if (!el) {
      el = document.createElement('div');
      el.className = 'messages-inner';
      messagesEl.appendChild(el);
    }
    return el;
  }

  function timeHtml() {
    const d = new Date();
    return '<span class="msg-time" title="' + esc(d.toLocaleString()) + '">'
      + clock(d) + '</span>';
  }

  function addUser(text) {
    $('#emptyState').style.display = 'none';
    const wrap = document.createElement('div');
    wrap.className = 'msg user';
    wrap.innerHTML = '<div class="avatar">YOU</div><div class="msg-body">'
      + '<div class="msg-role"><span class="msg-role-name">You</span>' + timeHtml() + '</div>'
      + '<div class="msg-body-inner"><div class="msg-content"></div></div></div>';
    wrap.querySelector('.msg-content').textContent = text;
    const edit = document.createElement('button');
    edit.className = 'msg-edit';
    edit.type = 'button';
    edit.title = 'Edit. Rewinds the session to this message and continues from it';
    edit.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17.05V20z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
    wrap.querySelector('.msg-body').appendChild(edit);
    inner().appendChild(wrap);
    scrollBottom(true);
    return wrap;
  }

  function addAgent() {
    $('#emptyState').style.display = 'none';
    const wrap = document.createElement('div');
    wrap.className = 'msg assistant';
    wrap.innerHTML = '<div class="avatar">TC</div><div class="msg-body">'
      + '<div class="msg-role"><span class="msg-role-name">Tacit</span>' + timeHtml() + '</div>'
      + '<div class="msg-body-inner">'
      + '<div class="reason-block"><button class="reason-toggle" type="button">thinking…</button>'
      + '<div class="reason-body"></div></div><div class="tools"></div><div class="msg-content"></div>'
      + '</div></div>';
    inner().appendChild(wrap);
    scrollBottom(true);
    return wrap;
  }

  // The thinking block is open while it fills and collapses when the first real
  // token lands, which is what the app does. The reader can always reopen it.
  async function streamReason(row, text) {
    const blk = row.querySelector('.reason-block');
    const body = row.querySelector('.reason-body');
    const toggle = row.querySelector('.reason-toggle');
    blk.classList.add('open');
    toggle.textContent = 'thinking…';
    for (const piece of text.match(/.{1,3}/gs) || []) {
      body.textContent += piece;
      scrollBottom();
      await wait(10);
    }
    await wait(240);
    blk.classList.remove('open');
    toggle.textContent = 'thought process';
  }

  async function content(row, text) {
    const target = row.querySelector('.msg-content');
    for (let shown = 6; shown <= text.length; shown += 5) {
      target.innerHTML = md(text.slice(0, shown));
      scrollBottom();
      await wait(9);
    }
    target.innerHTML = md(text);
    scrollBottom();
  }

  // ── tool cards ─────────────────────────────────────────────────────────
  const RAIL_MIN = 30, RAIL_MAX = 30000, RAIL_LOG = Math.log(RAIL_MAX / RAIL_MIN), RAIL_FLOOR = 0.05;
  function railDur(ms) {
    const n = Number(ms);
    if (!Number.isFinite(n) || n <= 0) return 0;
    const c = Math.max(RAIL_MIN, Math.min(RAIL_MAX, n));
    return Math.max(RAIL_FLOOR, Math.log(c / RAIL_MIN) / RAIL_LOG);
  }
  function setRail(card, ms) {
    const rail = card.querySelector('.tool-rail');
    if (rail) rail.style.setProperty('--dur', railDur(ms).toFixed(3));
  }
  function fmtMs(ms) {
    if (ms === null || ms === undefined) return '';
    const n = Number(ms);
    return n < 1000 ? Math.round(n) + 'ms' : (n / 1000).toFixed(1) + 's';
  }
  function setToolMeta(card, text) {
    let meta = card.querySelector('.tool-meta');
    if (!meta) {
      meta = document.createElement('span');
      meta.className = 'tool-meta';
      card.querySelector('.tool-head').insertBefore(meta, card.querySelector('.tool-spin'));
    }
    meta.textContent = text;
  }
  // Same rule the app uses on a live card: newest end wins, KB past 1024 bytes, and
  // the number loses its decimal once the output is big enough that precision is noise.
  function sizeOf(chars) {
    return chars >= 1024
      ? (chars / 1024).toFixed(chars >= 10240 ? 0 : 1) + ' KB'
      : chars + ' B';
  }
  function railSum(tools) {
    if (!tools) return;
    let n = 0, total = 0, slow = 0;
    tools.querySelectorAll('.tool-card').forEach((c) => {
      const ms = Number(c.dataset.ms || 0);
      if (ms > 0) { n++; total += ms; slow = Math.max(slow, ms); }
    });
    let sum = tools.querySelector('.rail-sum');
    if (!n) { if (sum) sum.remove(); return; }
    if (!sum) { sum = document.createElement('div'); sum.className = 'rail-sum'; }
    const bits = [n + ' call' + (n === 1 ? '' : 's'), fmtMs(total) + ' total'];
    if (n > 1) bits.push('slowest ' + fmtMs(slow));
    sum.textContent = bits.join(' · ');
    tools.appendChild(sum);
  }

  let toolSeq = 0;
  async function tool(row, name, args, o) {
    o = o || {};
    const tools = row.querySelector('.tools');
    const card = document.createElement('div');
    card.className = 'tool-card run';
    card.dataset.id = 't' + (++toolSeq);
    card.innerHTML = '<div class="tool-head"><span class="tool-badge"></span>'
      + '<span class="tool-args"></span><span class="tool-spin"></span></div>'
      + '<div class="tool-rail"></div><div class="tool-body"><pre></pre></div>';
    card.querySelector('.tool-badge').textContent = name;
    card.querySelector('.tool-args').textContent = args;
    tools.appendChild(card);
    scrollBottom(true);

    const pre = card.querySelector('.tool-body pre');
    let elapsed = 0;
    for (const chunk of (o.live || [])) {
      await wait(chunk.at - elapsed);
      elapsed = chunk.at;
      pre.textContent += chunk.text + '\n';
      setToolMeta(card, Math.round(elapsed / 1000) + 's · ' + sizeOf(pre.textContent.length));
      setRail(card, elapsed);
      if (card.classList.contains('open')) {
        const body = card.querySelector('.tool-body');
        body.scrollTop = body.scrollHeight;
      }
      scrollBottom();
    }

    const ms = o.ms || 400;
    if (!(o.live || []).length) await wait(240);
    card.classList.toggle('err', !!o.err);
    card.classList.remove('run');
    const spin = card.querySelector('.tool-spin');
    if (spin) spin.remove();
    pre.textContent = o.result || (o.err ? 'error' : 'done');
    card.dataset.ms = ms;
    setRail(card, ms);
    setToolMeta(card, o.meta || fmtMs(ms));
    railSum(tools);
    if (o.tokens) setTokens(S.tokens + o.tokens);
    if (o.open) card.classList.add('open');
    scrollBottom();
    return card;
  }

  function snapshot(row, name, files) {
    const role = row.querySelector('.msg-role');
    if (!role) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'msg-snap';
    b.dataset.snap = name;
    b.textContent = 'snapshot';
    b.title = name + ' · ' + files + ' file(s) · taken before this turn\'s first edit';
    role.appendChild(b);
  }

  // ── strips ─────────────────────────────────────────────────────────────
  const PHASES = {
    decomposing: '1 · Decomposing request',
    exploring: '2 · Exploring the codebase',
    questions: '3 · Preparing questions',
    awaiting_answers: '4 · Your answers',
    synthesizing: '5 · Writing the plan',
  };
  const ORDER = ['decomposing', 'exploring', 'questions', 'synthesizing'];

  function planStatus(phase, detail, explorers) {
    const idx = ORDER.indexOf(phase);
    let html = '<div class="ps-head"><span class="ps-icon"></span>'
      + '<span class="ps-phase">' + esc(PHASES[phase] || phase) + '</span>';
    if (detail) html += '<span class="ps-detail">' + esc(detail) + '</span>';
    html += '<button class="ps-cancel" type="button">Cancel</button></div>';
    if (explorers && explorers.length) {
      html += '<div class="ps-explorers">' + explorers.map((e) => '<div class="ps-exp ' + e.state + '">'
        + '<span class="ps-exp-dot"></span><span class="ps-exp-label">' + esc(e.label) + '</span>'
        + '<span class="ps-exp-act">' + esc(e.activity) + '</span></div>').join('') + '</div>';
    }
    if (idx >= 0) {
      html += '<div class="ps-progress"><div class="ps-progress-bar" style="width:'
        + ((idx + 1) / ORDER.length * 100) + '%"></div></div>';
    }
    planStrip.hidden = false;
    planStrip.innerHTML = html;
  }

  function tasks(items) {
    const open = items.filter((t) => !t.done).length;
    taskStrip.hidden = false;
    $('#taskStripCount').textContent = open + '/' + items.length;
    const list = $('#taskStripList');
    list.hidden = false;
    list.innerHTML = items.map((t) => '<div class="task-strip-item' + (t.done ? ' done' : '') + '">'
      + '<span class="tick">' + (t.done ? '✓' : '○') + '</span>'
      + '<span class="task-text">' + esc(t.text) + '</span></div>').join('');
  }

  // ── Delegation panel ───────────────────────────────────────────────────
  async function delegate(label, activity, trace) {
    dgPanel.classList.add('open');
    const box = $('#dgMessages');
    if (box.querySelector('.as-empty')) box.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'card dg-card active';
    card.dataset.key = label;
    card.innerHTML = '<div class="card-head"><span class="dg-dot"></span>'
      + '<span class="dg-label">' + esc(label) + '</span><span class="dg-age">0s</span>'
      + '<button class="dg-toggle" type="button" aria-expanded="false" title="Expand/collapse">'
      + '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>'
      + '</button></div>'
      + '<div class="dg-activity">' + esc(activity) + '</div>'
      + '<div class="dg-budget">sub-agent · inherits the session model</div>'
      + '<div class="dg-trace" hidden></div>';
    box.appendChild(card);
    const traceEl = card.querySelector('.dg-trace');
    let age = 0;
    for (const ev of trace) {
      await wait(ev.at - age);
      age = ev.at;
      card.querySelector('.dg-age').textContent = Math.round(age / 1000) + 's';
      const row = document.createElement('div');
      if (ev.kind === 'tool') {
        row.className = 'dg-tool';
        row.innerHTML = '<span class="dg-tool-name">→ ' + esc(ev.name) + '</span>'
          + '<pre class="dg-args">' + esc(ev.args || '') + '</pre>';
      } else if (ev.kind === 'done') {
        row.className = 'dg-tool';
        row.innerHTML = '<span class="dg-tool-name">✓ ' + esc(ev.name) + '</span>'
          + '<pre class="dg-result">' + esc(ev.result || '') + '</pre>';
      } else {
        row.className = 'dg-text';
        row.textContent = ev.text;
      }
      traceEl.appendChild(row);
      if (traceEl.hidden === false) traceEl.scrollTop = traceEl.scrollHeight;
    }
    card.classList.remove('active');
    card.classList.add('done');
    card.querySelector('.dg-activity').textContent = 'reported back to the session';
  }
  $('#dgMessages').addEventListener('click', (e) => {
    const b = e.target.closest('.dg-toggle');
    if (!b) return;
    const card = b.closest('.dg-card');
    const trace = card.querySelector('.dg-trace');
    trace.hidden = !trace.hidden;
    b.setAttribute('aria-expanded', String(!trace.hidden));
  });

  // ── Assistant panel ────────────────────────────────────────────────────
  // The Assistant thread is its own markup, not the transcript's: `.as-msg.me` is the
  // reader's line and `.as-msg.them` is the assistant's, with markdown rendered into
  // `.as-body.md`.
  function asMsg(role, text) {
    const box = $('#asMessages');
    if (box.querySelector('.as-empty')) box.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'as-msg ' + (role === 'user' ? 'me' : 'them');
    el.innerHTML = '<div class="as-body' + (role === 'user' ? '' : ' md') + '"></div>';
    const body = el.querySelector('.as-body');
    if (role === 'user') body.textContent = text;
    else body.innerHTML = md(text);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  // ── sessions ───────────────────────────────────────────────────────────
  const SESSIONS = {
    'shell-timeout': {
      title: 'shell timeout leaves orphans',
      wd: 'C:\\Projects\\Tacit',
      model: 'glm-5.2 · fp8',
      tokens: 71040,
      ask: 'The shell tool kills a timed-out command but the children keep running. Find it and fix it.',
      script: scriptOrphans,
    },
    'audit-rotation': {
      title: 'audit ledger rotation',
      wd: 'C:\\Projects\\Tacit',
      model: 'glm-5.2 · fp8',
      tokens: 38420,
      ask: 'The audit ledger never rotates. Add size-based rotation without losing the current file.',
      script: scriptLedger,
    },
    'prompt-cost': {
      title: 'why is the prompt so big',
      wd: 'C:\\Projects\\Tacit',
      model: 'glm-5.2 · fp8',
      tokens: 12980,
      ask: 'How much of the fixed prompt is the tool list, and can I read it anywhere?',
      script: scriptAnswer,
    },
  };

  function loadSession(key, quiet) {
    const s = SESSIONS[key];
    S.session = key;
    $$('.session-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.key === key);
      el.classList.toggle('working', el.dataset.key === key && S.busy);
    });
    $('#topTitle').textContent = s.title;
    $('#wdLabel').textContent = s.wd;
    $('#topMeta').textContent = 'Agent · local / ' + s.model;
    const old = messagesEl.querySelector('.messages-inner');
    if (old) old.remove();
    $('#emptyState').style.display = quiet ? '' : 'none';
    if (!quiet) addUser(s.ask);
    setTokens(s.tokens);
    planStrip.hidden = true;
    planStrip.innerHTML = '';
    taskStrip.hidden = true;
    scrollBottom(true);
  }

  // ── scripted turns ─────────────────────────────────────────────────────
  async function scriptOrphans() {
    const row = addAgent();
    await streamReason(row, 'The user says a timed-out shell command leaves its children running. '
      + 'The kill path lives in the process tools, so start there and read how the sweep decides '
      + 'what to terminate. Then check whether the suite covers the child case at all.');

    setTokens(S.tokens + 640);
    await tool(row, 'grep', 'kill  backend/proctools.py', {
      ms: 90, meta: '90ms · read-only', tokens: 320,
      result: 'backend/proctools.py:112:  proc.kill()\nbackend/proctools.py:148:  reap(pid)',
    });
    await content(row, 'The kill is per-pid, so a command that spawned a process tree only loses its '
      + 'root. Reading the sweep.');
    await tool(row, 'read', 'backend/proctools.py', {
      ms: 140, meta: '140ms · read-only', tokens: 1480, open: true,
      result: 'def sweep():\n    for pid in tracked():\n        if expired(pid):\n'
        + '            kill(pid)   # root only\n            reap(pid)',
    });
    await content(row, 'Confirmed: `kill(pid)` terminates the tracked root and nothing else. On '
      + 'Windows the children outlive it because there is no job object behind the spawn.');

    tasks([{ text: 'Find the kill path', done: true }, { text: 'Kill the whole tree', done: false },
      { text: 'Cover it with a test', done: false }]);

    delegate('research: who else spawns', 'mapping every child process the shell leaves behind', [
      { kind: 'tool', at: 700, name: 'grep', args: '{"pattern": "Popen|spawn", "path": "backend"}' },
      { kind: 'text', at: 1500, text: 'four call sites, two of them start long-running watchers' },
      { kind: 'done', at: 2400, name: 'grep', result: 'backend/proctools.py:88  spawn()\nbackend/agent.py:412  _shell_run()' },
    ]);

    await tool(row, 'edit', 'backend/proctools.py', {
      ms: 60, meta: '60ms · 1 file · snapshot before edit', tokens: 520, open: true,
      result: '-            kill(pid)\n+            kill_tree(pid)\n             reap(pid)',
    });
    snapshot(row, 'snap-01', 2);
    tasks([{ text: 'Find the kill path', done: true }, { text: 'Kill the whole tree', done: true },
      { text: 'Cover it with a test', done: false }]);

    await tool(row, 'bash', 'pytest -q tests/test_proctools.py', {
      ms: 4200, tokens: 2100, meta: '4.2s · sandbox: none · exit 1', err: true, open: true,
      live: [{ at: 900, text: 'F' }, { at: 2100, text: '..' },
        { at: 3600, text: 'FAILED tests/test_proctools.py::test_child_dies_with_parent' }],
      result: 'assert child.poll() is None\nE   AssertionError: the grandchild is still running\n'
        + '1 failed, 12 passed in 4.18s',
    });
    toast('A test caught the race, not the fix: the child was still alive when the assertion ran.', true);
    await wait(700);

    await content(row, 'The assertion races the kill. Waiting on the child instead of polling once:');
    await tool(row, 'edit', 'tests/test_proctools.py', {
      ms: 50, meta: '50ms · 1 file', tokens: 380, open: true,
      result: '-    assert child.poll() is None\n+    assert child.wait(timeout=5) is not None',
    });
    await tool(row, 'bash', 'pytest -q tests/test_proctools.py', {
      ms: 3800, tokens: 1900, meta: '3.8s · sandbox: none · exit 0',
      live: [{ at: 1200, text: '.' }, { at: 2600, text: '.............' }],
      result: '13 passed in 3.79s',
    });
    tasks([{ text: 'Find the kill path', done: true }, { text: 'Kill the whole tree', done: true },
      { text: 'Cover it with a test', done: true }]);
    await content(row, 'Done. `kill_tree` walks the children before the root dies, and the test now '
      + 'waits for the grandchild instead of guessing. Two files changed, one snapshot taken, so '
      + 'the whole turn is undoable from the clock in the top bar.');
    finishTurn(11840, '$0.0193');
  }

  async function scriptLedger() {
    planStatus('decomposing', 'size-based rotation, keep the current file');
    const row = addAgent();
    await streamReason(row, 'Rotation has to be safe under concurrent writers, so this is a plan '
      + 'before it is an edit: find the writer, find who reads the ledger, then decide the boundary.');
    await wait(300);
    planStatus('exploring', '3 explorers', [
      { label: 'writer', state: 'done', activity: 'backend/audit.py:44' },
      { label: 'readers', state: 'run', activity: 'Settings > Isolation' },
      { label: 'tests', state: 'run', activity: 'tests/test_audit.py' },
    ]);
    await wait(1000);
    planStatus('exploring', '3 explorers', [
      { label: 'writer', state: 'done', activity: 'backend/audit.py:44' },
      { label: 'readers', state: 'done', activity: '2 readers, both tail the current file' },
      { label: 'tests', state: 'done', activity: 'no rotation test exists' },
    ]);
    await wait(800);
    planStatus('synthesizing', 'rename, reopen, fsync');
    await wait(900);
    planStrip.hidden = true;

    await content(row, 'The plan: append a size check to the writer, rename the full file to a dated '
      + 'name, reopen a fresh ledger, keep three. Readers only ever tail the current file, so '
      + 'nothing changes on that side.');
    tasks([{ text: 'Size check in the writer', done: false }, { text: 'Rotate and reopen', done: false },
      { text: 'Add a rotation test', done: false }]);

    await tool(row, 'read', 'backend/audit.py', {
      ms: 120, meta: '120ms · read-only', tokens: 1240,
      result: 'def append(event):\n    with open(LEDGER, "a", encoding="utf-8") as fh:\n'
        + '        fh.write(json.dumps(event) + "\\n")',
    });
    tasks([{ text: 'Size check in the writer', done: true }, { text: 'Rotate and reopen', done: false },
      { text: 'Add a rotation test', done: false }]);
    await tool(row, 'edit', 'backend/audit.py', {
      ms: 70, meta: '70ms · 1 file · snapshot before edit', tokens: 860, open: true,
      result: '+    if os.path.getsize(LEDGER) > MAX_LEDGER_BYTES:\n+        rotate(LEDGER, keep=3)\n'
        + '     with open(LEDGER, "a", encoding="utf-8") as fh:',
    });
    snapshot(row, 'snap-01', 1);
    await tool(row, 'bash', 'pytest -q tests/test_audit.py -k rotation', {
      ms: 2600, tokens: 1400, meta: '2.6s · sandbox: none · exit 0', open: true,
      live: [{ at: 1400, text: '.' }],
      result: '2 passed, 41 deselected in 2.51s',
    });
    tasks([{ text: 'Size check in the writer', done: true }, { text: 'Rotate and reopen', done: true },
      { text: 'Add a rotation test', done: true }]);
    await content(row, 'Rotated at 4 MB with three files kept, and the test that did not exist now '
      + 'does. The ledger never disappears mid-read because the rename happens before the new '
      + 'handle opens.');
    finishTurn(6120, '$0.0104');
  }

  async function scriptAnswer() {
    const row = addAgent();
    await streamReason(row, 'This is a question about the prompt, not a change to the codebase. '
      + 'The honest answer is the inventory, which the app already serves.');
    await tool(row, 'read', 'backend/prompts_catalog.py', {
      ms: 110, meta: '110ms · read-only', tokens: 980,
      result: '14 groups, 89 entries, 24,180 characters, about 6,000 tokens',
    });
    await content(row, 'The fixed cost is 14 groups and 89 entries, roughly 6,000 tokens, and it is '
      + 'no longer something you have to read out of the source. Settings has a **Prompts** tab '
      + 'that lists every one of them, and the block your current session is actually sending '
      + 'sits at the top of it.');
    await wait(500);
    openSettings('prompts');
    await content(row, 'That is the contract: if a sentence goes to a model, you can read it in the '
      + 'interface that sent it. The tab is on the right, and this page has the same texts copied '
      + 'out of the source that sends them.');
    finishTurn(2140, '$0.0037');
  }

  async function scriptGeneric(text) {
    const row = addAgent();
    await streamReason(row, 'Reading the request: ' + text.slice(0, 160)
      + '. Start with the files it names, then run whatever proves the change.');
    await tool(row, 'glob', '**/*.py', {
      ms: 60, meta: '60ms · read-only', tokens: 420, result: '111 files, 18,328 lines',
    });
    await content(row, 'Working through that. The project is one Python program serving plain pages, '
      + 'so a change like this lands in the backend and the transcript you are watching is the result.');
    tasks([{ text: 'Find the code the request names', done: true },
      { text: 'Change it', done: false }, { text: 'Run the suite', done: false }]);
    await tool(row, 'edit', 'backend/agent.py', {
      ms: 80, meta: '80ms · 1 file · snapshot before edit', tokens: 640, open: true,
      result: '+    # what the request asked for\n+    apply(change)',
    });
    snapshot(row, 'snap-01', 1);
    tasks([{ text: 'Find the code the request names', done: true },
      { text: 'Change it', done: true }, { text: 'Run the suite', done: false }]);
    await tool(row, 'bash', 'python -m unittest discover -s tests -t .', {
      ms: 5200, tokens: 2400, meta: '5.2s · sandbox: none · exit 0',
      live: [{ at: 1200, text: '.' }, { at: 2400, text: '........................' },
        { at: 3800, text: '.....' }],
      result: 'Ran 1269 tests in 84.127s\n\nOK (skipped=1)',
    });
    tasks([{ text: 'Find the code the request names', done: true },
      { text: 'Change it', done: true }, { text: 'Run the suite', done: true }]);
    await content(row, 'Green. Nothing on this page was written to your disk: there is no server '
      + 'behind it, which is the only honest thing a mock can say about itself.');
    finishTurn(4180, '$0.0071');
  }

  function finishTurn(spent, cost) {
    setTokens(S.tokens + spent);
    $('#topMeta').textContent = 'Agent · local / ' + SESSIONS[S.session].model
      + ' · ' + comma(spent) + ' tok · ' + cost;
    asPanel.classList.add('open');
    asMsg('assistant', 'Turn cost **' + cost + '**, ' + comma(spent) + ' tokens, 3 edits, 1 snapshot. '
      + 'The agent was never told this panel exists.');
    $('#asBudget').textContent = 'runs 2 · ' + comma(spent) + ' tok of 200,000';
  }

  // ── the runner ─────────────────────────────────────────────────────────
  async function runTurn(script, typed) {
    if (S.busy) return;
    S.busy = true;
    S.run += 1;
    const myRun = S.run;
    $('#sendBtn').hidden = true;
    $('#stopBtn').hidden = false;
    $('#connHint').textContent = 'working';
    $('#steerHint').hidden = false;
    $$('.session-item').forEach((el) => el.classList.toggle('working', el.dataset.key === S.session));
    if (typed) addUser(typed);
    try {
      await script(typed);
    } catch (err) {
      toast('mock stopped: ' + (err && err.message ? err.message : err), true);
    }
    if (S.run !== myRun) return;
    S.busy = false;
    $('#sendBtn').hidden = false;
    $('#stopBtn').hidden = true;
    $('#steerHint').hidden = true;
    $('#connHint').textContent = 'connected';
    $$('.session-item').forEach((el) => el.classList.remove('working'));
  }

  // The idle loop: after a turn the next session takes over, so the page is never a
  // still image. Compaction fires once, because a context that only grows is a demo
  // of nothing.
  const LOOP = ['shell-timeout', 'audit-rotation', 'prompt-cost'];
  let loopAt = 0, compacted = false;

  async function idleLoop() {
    while (true) {
      await wait(6500);
      if (S.busy) continue;
      loopAt = (loopAt + 1) % LOOP.length;
      if (!compacted && loopAt === 0) {
        compacted = true;
        S.compacting = true;
        setTokens(S.tokens);
        toast('compaction: summarising the middle of the transcript, keeping the tail', true);
        await wait(2800);
        S.compacting = false;
        setTokens(24100);
        toast('compacted: 24,100 tokens in context');
        await wait(1200);
      }
      loadSession(LOOP[loopAt], true);
      const ask = SESSIONS[LOOP[loopAt]].ask;
      await typeInto(ask);
      await runTurn(SESSIONS[LOOP[loopAt]].script, ask);
    }
  }

  // The ask is typed into the composer before it is sent, because watching the
  // message arrive already written is watching a screenshot, not a session.
  async function typeInto(text) {
    composerEl.value = '';
    for (const ch of text) {
      composerEl.value += ch;
      autoGrow();
      await wait(15);
    }
    await wait(420);
    composerEl.value = '';
    autoGrow();
  }

  // ── controls that are really the app's ─────────────────────────────────
  function autoGrow() {
    composerEl.style.height = 'auto';
    composerEl.style.height = Math.min(180, composerEl.scrollHeight) + 'px';
  }
  function send() {
    const text = composerEl.value.trim();
    if (!text || S.busy) return;
    composerEl.value = '';
    autoGrow();
    runTurn(scriptGeneric, text);
  }
  $('#sendBtn').addEventListener('click', send);
  composerEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  composerEl.addEventListener('input', autoGrow);
  $('#stopBtn').addEventListener('click', () => {
    S.run += 1;
    S.busy = false;
    $('#sendBtn').hidden = false;
    $('#stopBtn').hidden = true;
    toast('stopped: the turn was cut, the transcript stayed');
  });

  $('#newChatBtn').addEventListener('click', () => {
    if (S.busy) return;
    loadSession(LOOP[0]);
    runTurn(SESSIONS[LOOP[0]].script);
  });
  $('#sidebarToggle').addEventListener('click', () => $('#sidebar').classList.toggle('hidden'));
  $('#asToggle').addEventListener('click', () => asPanel.classList.toggle('open'));
  $('#asClose').addEventListener('click', () => asPanel.classList.remove('open'));
  $('#dgToggle').addEventListener('click', () => dgPanel.classList.toggle('open'));
  $('#dgClose').addEventListener('click', () => dgPanel.classList.remove('open'));
  $('#asClear').addEventListener('click', () => { $('#asMessages').innerHTML = ''; });
  $('#asSettingsBtn').addEventListener('click', () => $('#asSettingsWrap').hidden = !$('#asSettingsWrap').hidden);
  $('#dgSettingsBtn').addEventListener('click', () => {
    $('#dgSettingsWrap').hidden = !$('#dgSettingsWrap').hidden;
  });
  $('#asReset').addEventListener('click', () => { $('#asBudget').textContent = 'runs 0 · 0 tok of 200,000'; });
  $('#planToggle').addEventListener('click', (e) => {
    const on = !e.currentTarget.classList.contains('active');
    e.currentTarget.classList.toggle('active', on);
    composerEl.placeholder = on ? 'Describe what to plan…' : 'Message Tacit…';
  });
  $('#attachBtn').addEventListener('click', () => {
    $('#attachRow').hidden = false;
    $('#attachRow').innerHTML = '<span class="att-chip"><span>screenshot.png</span>'
      + '<button type="button" title="Remove">×</button></span>';
  });
  $('#modeSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    $$('#modeSeg button').forEach((x) => x.classList.toggle('active', x === b));
    $('#topMeta').textContent = (b.dataset.mode === 'agent' ? 'Agent' : 'Chat')
      + ' · local / ' + SESSIONS[S.session].model;
  });
  $('#taskStripClose').addEventListener('click', () => { taskStrip.hidden = true; });
  $('#jumpBtn').addEventListener('click', () => scrollBottom(true));
  messagesEl.addEventListener('scroll', () => {
    const far = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight > 320;
    $('#jumpBtn').hidden = !far;
  });

  $$('.session-item').forEach((el) => {
    el.addEventListener('click', () => {
      if (S.busy || !SESSIONS[el.dataset.key]) return;
      loopAt = LOOP.indexOf(el.dataset.key);
      loadSession(el.dataset.key);
    });
  });
  $('#suggestions').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    composerEl.value = chip.dataset.s;
    autoGrow();
    composerEl.focus();
  });

  // A tool card opens, a thinking block reopens: the app's own handlers.
  messagesEl.addEventListener('click', (e) => {
    const head = e.target.closest('.tool-head');
    if (head) { head.parentElement.classList.toggle('open'); return; }
    const toggle = e.target.closest('.reason-toggle');
    if (toggle) { toggle.closest('.reason-block').classList.toggle('open'); return; }
    if (e.target.closest('.msg-snap')) openSnapshots();
  });

  // ── workspace chip ─────────────────────────────────────────────────────
  const wdMenu = $('#wdMenu');
  $('#wdChip').addEventListener('click', (e) => {
    e.stopPropagation();
    wdMenu.hidden = !wdMenu.hidden;
  });
  document.addEventListener('click', () => { wdMenu.hidden = true; });

  // ── session menu ───────────────────────────────────────────────────────
  $('#sessMenuBtn').addEventListener('click', (e) => {
    e.stopPropagation();
    $('#sessMenu').classList.toggle('open');
  });
  $('#sessMenu').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    $('#sessMenu').classList.remove('open');
    if (b.dataset.act === 'compact') {
      S.compacting = true;
      setTokens(S.tokens);
      toast('compaction: summarising the middle of the transcript, keeping the tail', true);
      setTimeout(() => { S.compacting = false; setTokens(24100); toast('compacted: 24,100 tokens in context'); }, 2600);
    } else {
      toast(b.textContent + ' (mock): no server behind this page');
    }
  });

  // ── Settings overlay ───────────────────────────────────────────────────
  const settings = $('#harnessOverlay');
  function openSettings(tab) {
    settings.hidden = false;
    selectTab(tab || 'appearance');
  }
  $('#harnessBtn').addEventListener('click', () => {
    if (settings.hidden) openSettings(); else settings.hidden = true;
  });
  $('#hoClose').addEventListener('click', () => { settings.hidden = true; });

  function selectTab(tab) {
    $$('#hoTabs .ho-tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
    ['general', 'providers', 'tools', 'appearance', 'prompts'].forEach((id) => {
      const p = $('#panel-' + id);
      if (p) p.hidden = id !== tab;
    });
    $('#hoNote').textContent = tab === 'prompts'
      ? 'Every character that goes to a model, read from the constants that send it.'
      : 'Mock panel. The real Settings writes to the state folder.';
  }
  $('#hoTabs').addEventListener('click', (e) => {
    const b = e.target.closest('.ho-tab');
    if (b) selectTab(b.dataset.tab);
  });

  const THEMES = window.TACIT_THEMES || [];
  const TONES = [['dark', 'Dark'], ['light', 'Light'], ['aaa', 'High contrast']];
  function inTone(t, tone) { return tone === 'dark' ? t.tone === 'dark' : t.tone === tone; }
  function renderAppearance() {
    const cur = document.documentElement.dataset.theme || 'nocturne';
    const cards = (tone) => THEMES.filter((t) => inTone(t, tone)).map((t) =>
      '<button class="card theme-card' + (t.id === cur ? ' active' : '') + '" data-theme="'
      + esc(t.id) + '" type="button"><span class="theme-dot sw-' + esc(t.id) + '"></span>'
      + '<span class="theme-meta"><b>' + esc(t.name) + '</b><small>' + esc(t.hint) + '</small></span>'
      + '<span class="theme-check">' + (t.id === cur ? 'Active' : '') + '</span></button>').join('');
    $('#panel-appearance').innerHTML =
      '<div class="ho-toolbar"><span class="ho-sub">Pick a look, it applies instantly. This is the '
      + 'app\'s own theme sheet, so every surface on the page changes with it.</span></div>'
      + TONES.filter((pair) => THEMES.some((t) => inTone(t, pair[0]))).map((pair) =>
        '<div class="theme-group"><div class="theme-group-label">' + pair[1]
        + '<span class="theme-group-count">' + THEMES.filter((t) => inTone(t, pair[0])).length
        + '</span></div><div class="theme-cards">' + cards(pair[0]) + '</div></div>').join('');
    $$('#panel-appearance .theme-card').forEach((b) => {
      b.addEventListener('click', () => {
        document.documentElement.dataset.theme = b.dataset.theme;
        $('#meta-theme').setAttribute('content', INK[b.dataset.theme] || '#14171d');
        renderAppearance();
      });
    });
  }
  const INK = { nocturne: '#14171d', graphite: '#1b1c1e', kinpaku: '#f6f1e6',
    'kinpaku-dark': '#121016', vellum: '#f7f4ec', contrast: '#000000', 'contrast-light': '#ffffff',
    verdigris: '#141b1a', mono: '#131313' };

  const PROMPTS = window.TACIT_PROMPTS || [];
  function renderPrompts() {
    const panel = $('#panel-prompts');
    if (!PROMPTS.length) {
      panel.innerHTML = '<p class="ho-sub">The inventory is read from a running server.</p>';
      return;
    }
    const item = (name, text) => '<div class="pc-item"><div class="pc-name">' + esc(name) + '</div>'
      + '<pre class="pc-text">' + esc(text) + '</pre></div>';
    panel.innerHTML =
      '<div class="ho-toolbar"><span class="ho-sub">' + PROMPTS.length
      + ' entries, copied out of the source at build time. The live panel lists all 89, plus the '
      + 'block the current session is sending.</span></div>'
      + PROMPTS.map((g) => '<details class="pc-group"' + (g.id === 'system' ? ' open' : '') + '>'
        + '<summary>' + esc(g.label) + ' · ' + g.entries.length + '</summary>'
        + g.entries.map((e) => item(e.name, e.text)).join('') + '</details>').join('');
  }

  $('#panel-general').innerHTML =
    '<div class="ho-section">Session</div>'
    + '<div class="card"><div class="ctl-row"><span class="ctl-label">Mode</span>'
    + '<span class="ho-sub">Agent. Chat gives no tools and no sandbox.</span></div>'
    + '<div class="ctl-row"><span class="ctl-label">Workspace</span>'
    + '<span class="ho-sub">C:\\Projects\\Tacit</span></div>'
    + '<div class="ctl-row"><span class="ctl-label">Turn token budget</span>'
    + '<span class="ho-sub">200,000</span></div></div>'
    + '<div class="ho-section">Context</div>'
    + '<div class="card"><div class="ctl-row"><span class="ctl-label">Window</span>'
    + '<span class="ho-sub">128,000 tokens, 12,000 held back for compaction</span></div>'
    + '<div class="ctl-row"><span class="ctl-label">Auto compaction</span>'
    + '<span class="ho-sub">on</span></div></div>';

  $('#panel-providers').innerHTML =
    '<div class="ho-section">Providers</div>'
    + '<div class="card"><div class="ctl-row"><span class="ctl-label">local</span>'
    + '<span class="ho-sub">http://127.0.0.1:8080/v1, no key</span></div>'
    + '<div class="ctl-row"><span class="ctl-label">Model</span>'
    + '<span class="ho-sub">glm-5.2 · fp8 · 128k window</span></div></div>'
    + '<div class="card"><div class="ctl-row"><span class="ctl-label">Profiles</span>'
    + '<span class="ho-sub">default, cheap, thorough. Each one changes tools, thinking and budget, '
    + 'and every prompt it sends is listed under Prompts.</span></div></div>';

  $('#panel-tools').innerHTML =
    '<div class="ho-section">Tools offered</div>'
    + '<div class="card"><div class="ctl-row"><span class="ctl-label">20 of 22</span>'
    + '<span class="ho-sub">read, write, edit, glob, grep, bash, task, research, evidence, fetch, '
    + 'browser, bg_start, bg_output, bg_stop, skill, skill_run, skill_install, skill_remove, '
    + 'skill_disable, skill_enable</span></div></div>'
    + '<div class="ho-section">Skills</div>'
    + '<div class="card"><div class="ctl-row"><span class="ctl-label">3 installed</span>'
    + '<span class="ho-sub">Only names and triggers sit in the prompt. The body is read when a '
    + 'skill runs, which is what keeps 89 entries affordable.</span></div></div>';

  // ── Snapshots overlay ──────────────────────────────────────────────────
  const snapOverlay = $('#snapOverlay');
  function openSnapshots() {
    $('#snapSessionLabel').textContent = SESSIONS[S.session].title;
    $('#snapNote').textContent = 'Two snapshots this turn. Restore puts the files back and leaves '
      + 'the transcript alone.';
    $('#snapList').innerHTML =
      '<div class="card"><div class="ctl-row"><span class="ctl-label">snap-01</span>'
      + '<span class="ho-sub">2 files · ' + clock(new Date()) + '</span>'
      + '<button class="ho-btn small" type="button">Restore</button></div></div>'
      + '<div class="card"><div class="ctl-row"><span class="ctl-label">snap-00</span>'
      + '<span class="ho-sub">0 files · turn start</span>'
      + '<button class="ho-btn small" type="button">Compare</button></div></div>';
    snapOverlay.hidden = false;
  }
  $('#snapBtn').addEventListener('click', () => {
    if (snapOverlay.hidden) openSnapshots(); else snapOverlay.hidden = true;
  });
  $('#snapClose').addEventListener('click', () => { snapOverlay.hidden = true; });

  // ── Terminal overlay ───────────────────────────────────────────────────
  const termOverlay = $('#termOverlay');
  const termOut = $('#termOut'), termIn = $('#termIn');
  function termPrint(text, cls) {
    const line = document.createElement('div');
    line.className = cls || 'term-stdout';
    line.textContent = text;
    termOut.appendChild(line);
    termOut.scrollTop = termOut.scrollHeight;
  }
  // The terminal answers from a table, and `git status` reads the same state the Git
  // panel draws, so the two cannot tell the reader different stories about one tree.
  const TERM = {
    'git status': () => gitStatusLines(),
    'git log --oneline -3': () => GIT.log.slice(0, 3).map((c) => c.short + ' ' + c.subject),
    'pytest -q': ['Ran 1269 tests in 84.127s', '', 'OK (skipped=1)'],
    'python -v': ['Python 3.12.10'],
  };
  $('#termBtn').addEventListener('click', () => {
    termOverlay.hidden = !termOverlay.hidden;
    if (!termOverlay.hidden) {
      $('#termCwd').textContent = SESSIONS[S.session].wd;
      $('#termShellName').textContent = 'cmd.exe';
      if (!termOut.childElementCount) {
        termPrint('Tacit terminal. Commands run in the workspace', 'term-note');
        termPrint('This one is a mock: what you type is answered from a table.', 'term-note');
      }
      termIn.focus();
    }
  });
  $('#termClose').addEventListener('click', () => { termOverlay.hidden = true; });
  $('#termClear').addEventListener('click', () => { termOut.innerHTML = ''; });
  termIn.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const cmd = termIn.value.trim();
    termIn.value = '';
    if (!cmd) return;
    termPrint('> ' + cmd, 'term-echo');
    const rows = TERM[cmd.toLowerCase()];
    const out = typeof rows === 'function' ? rows() : rows;
    if (out) out.forEach((l) => termPrint(l, 'term-stdout'));
    else termPrint(cmd.split(' ')[0] + ': this terminal has no server behind it', 'term-stderr');
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    settings.hidden = true;
    snapOverlay.hidden = true;
    termOverlay.hidden = true;
    gitOverlay.hidden = true;
  });

  // ── Git overlay ────────────────────────────────────────────────────────
  // The panel is the app's own markup, and the repository it describes is the invented
  // part. Stage, commit, checkout and the command box all do something, because a panel
  // that opens and cannot be touched is a screenshot with a close button. Every row
  // shape here is the shape `static/git.js` writes for the same element.
  const gitOverlay = $('#gitOverlay');
  const GIT = {
    repo: 'C:/Users/you/Tacit',
    branch: 'master',
    upstream: 'origin/master',
    ahead: 3,
    behind: 0,
    branches: ['master', 'fix/shell-orphan-kill', 'audit/ledger-rotation'],
    files: [
      { path: 'backend/tools/shell.py', index: ' ', worktree: 'M' },
      { path: 'backend/audit.py', index: 'M', worktree: 'M' },
      { path: 'tests/test_shell_orphans.py', index: 'A', worktree: 'A' },
      { path: 'docs/isolation-and-audit.md', index: ' ', worktree: 'M' },
      { path: 'scratch/notes.md', index: ' ', worktree: ' ', untracked: true },
    ],
    log: [
      { short: '97888c6', subject: 'Update docs and measured figures', refs: 'HEAD -> master', author: 'Silent', date: '2026-10-19 16:33' },
      { short: 'a89916f', subject: 'Lint slop, pin cost figures', refs: 'origin/master', author: 'Silent', date: '2026-10-19 16:33' },
      { short: '91d11c6', subject: 'Show compaction, add fallback picker', author: 'Silent', date: '2026-10-19 16:33' },
      { short: '87e3966', subject: 'Drop em dashes from prompts', author: 'Silent', date: '2026-10-19 16:32' },
      { short: 'c75c444', subject: 'Persist compaction, fail unknown models', author: 'Silent', date: '2026-10-19 16:32' },
    ],
    remotes: [{ name: 'origin', url: 'https://github.com/SebSilent/Tacit.git' }],
  };
  let gitConsole = [];
  const isStaged = (f) => f.index !== ' ' && f.index !== '?';
  const gitGlyph = (f) => (f.untracked ? { g: 'U', cls: 'u' }
    : (f.index === 'A' || f.worktree === 'A') ? { g: 'A', cls: 'a' }
    : (f.index === 'D' || f.worktree === 'D') ? { g: 'D', cls: 'd' }
    : (f.index === 'R' || f.worktree === 'R') ? { g: 'R', cls: 'r' }
    : { g: 'M', cls: 'm' });

  function gitNote(text, err) {
    const el = $('#gitNote');
    el.textContent = text || '';
    el.classList.toggle('err', !!err);
  }
  function gitLine(text) {
    gitConsole.push(text);
    if (gitConsole.length > 200) gitConsole = gitConsole.slice(-200);
    const out = $('#gitOut');
    out.textContent = gitConsole.join('\n');
    out.scrollTop = out.scrollHeight;
  }
  function gitStatusLines() {
    const lines = ['On branch ' + GIT.branch,
      'Your branch is ahead of \'' + GIT.upstream + '\' by ' + GIT.ahead + ' commit'
      + (GIT.ahead === 1 ? '' : 's') + '.'];
    if (GIT.behind) lines.push('  (and ' + GIT.behind + ' behind: pull before you push)');
    lines.push('');
    if (!GIT.files.length) { lines.push('nothing to commit, working tree clean'); return lines; }
    const st = GIT.files.filter(isStaged), un = GIT.files.filter((f) => !isStaged(f) && !f.untracked);
    if (st.length) {
      lines.push('Changes to be committed:');
      st.forEach((f) => lines.push('        modified: ' + f.path));
    }
    if (un.length) {
      if (st.length) lines.push('');
      lines.push('Changes not staged for commit:');
      un.forEach((f) => lines.push('        modified: ' + f.path));
    }
    const rest = GIT.files.filter((f) => f.untracked);
    if (rest.length) {
      lines.push('', 'Untracked files:');
      rest.forEach((f) => lines.push('        ' + f.path));
    }
    return lines;
  }
  function gitReply(cmd) {
    const c = cmd.replace(/\s+/g, ' ').trim().toLowerCase();
    const url = GIT.remotes.length ? GIT.remotes[0].url : '';
    if (c === 'git status') return gitStatusLines();
    if (c === 'git log --oneline -5') return GIT.log.slice(0, 5).map((k) => k.short + ' ' + k.subject);
    if (c === 'git remote -v') {
      const rows = [];
      GIT.remotes.forEach((r) => {
        rows.push(r.name + '\t' + r.url + ' (fetch)', r.name + '\t' + r.url + ' (push)');
      });
      return rows.length ? rows : ['(no remotes)'];
    }
    if (c === 'git fetch' || c === 'git fetch origin') {
      return GIT.behind ? ['From ' + url, '   ' + GIT.log[0].short + '..' + 'f1e2d3c  master -> origin/master']
        : ['From ' + url];
    }
    if (c === 'git pull' || c === 'git pull origin master') {
      if (!GIT.behind) return ['Already up to date.'];
      GIT.behind = 0;
      return ['From ' + url, 'Merge made by the \'ort\' strategy.'];
    }
    if (c === 'git push' || c === 'git push origin master') {
      if (GIT.behind) return ['! [rejected]        master -> master (non-fast-forward)'];
      const head = GIT.log[0];
      GIT.upstream = 'origin/master';
      GIT.ahead = 0;
      head.refs = 'HEAD -> master, origin/master';
      renderGit();
      return ['To ' + url, '   ' + GIT.log[1].short + '..' + head.short + '  master -> master'];
    }
    if (c === 'git diff --cached --stat') {
      const rows = GIT.files.filter(isStaged).map((f) => ' ' + f.path + ' | 4 ++--');
      return rows.length ? rows.concat(['  ' + rows.length + ' file' + (rows.length === 1 ? '' : 's') + ' changed'])
        : ['(nothing staged)'];
    }
    return [cmd.split(' ')[0] + ': this mock answers git status, log, remote, fetch, pull, push and diff'];
  }
  function gitRun(cmd) {
    const c = String(cmd || '').trim();
    if (!c) return;
    gitLine('$ ' + c);
    gitReply(c).forEach((l) => gitLine(l));
    gitLine('');
  }
  function renderGitBranchBar() {
    const options = GIT.branches.map((b) => '<option value="' + esc(b) + '"'
      + (b === GIT.branch ? ' selected' : '') + '>' + esc(b) + '</option>').join('');
    const track = GIT.upstream
      ? '<span class="git-track">\u2191' + GIT.ahead + ' \u2193' + GIT.behind + ' \u00b7 ' + esc(GIT.upstream) + '</span>'
      : '<span class="git-track dim">no upstream</span>';
    $('#gitBranchBar').innerHTML =
      '<div class="git-current"><span class="git-branch-icon">\u2387</span><strong>'
      + esc(GIT.branch) + '</strong>' + track + '</div>'
      + '<div class="git-branch-row">'
      + '<select id="gitBranchSelect" class="git-select">' + options + '</select>'
      + '<button class="ho-btn small" id="gitCheckoutBtn">Checkout</button>'
      + '<button class="ho-btn small" id="gitNewBranchBtn">New branch</button>'
      + '</div>';
    $('#gitCheckoutBtn').addEventListener('click', () => {
      const b = $('#gitBranchSelect').value;
      if (!b || b === GIT.branch) return;
      gitRun('git checkout ' + b);
      GIT.branch = b;
      gitLine('Switched to branch \'' + b + '\'');
      renderGit();
    });
    $('#gitNewBranchBtn').addEventListener('click', () => {
      const name = window.prompt('New branch name:');
      if (!name || !name.trim()) return;
      const b = name.trim();
      gitRun('git checkout -b ' + b);
      if (GIT.branches.indexOf(b) < 0) GIT.branches.push(b);
      GIT.branch = b;
      GIT.upstream = '';
      renderGit();
    });
  }
  function renderGitChanges() {
    const el = $('#gitChanges');
    $('#gitChangeCount').textContent = String(GIT.files.length);
    if (!GIT.files.length) {
      el.innerHTML = '<div class="ho-empty sm">working tree clean</div>';
      return;
    }
    el.innerHTML = GIT.files.map((f, i) => {
      const glyph = gitGlyph(f);
      const action = isStaged(f)
        ? '<button class="git-file-btn" data-i="' + i + '" data-act="unstage" title="Unstage">-</button>'
        : '<button class="git-file-btn" data-i="' + i + '" data-act="stage" title="Stage">+</button>';
      return '<div class="git-file"><span class="git-glyph ' + glyph.cls + '">' + glyph.g + '</span>'
        + '<span class="git-file-path" data-i="' + i + '" data-act="diff" title="' + esc(f.path) + '">'
        + esc(f.path) + '</span>' + action + '</div>';
    }).join('');
    $$('[data-act]', el).forEach((node) => {
      node.addEventListener('click', (e) => {
        e.stopPropagation();
        const f = GIT.files[parseInt(node.dataset.i, 10)];
        if (!f) return;
        const act = node.dataset.act;
        if (act === 'diff') {
          gitRun('git diff ' + (isStaged(f) ? '--cached ' : '') + '-- ' + f.path);
          gitLine('@@ -12,6 +12,7 @@ def run(cmd, timeout=30):');
          gitLine('-        proc.kill()');
          gitLine('+        kill_tree(proc)');
          gitLine('');
          return;
        }
        if (act === 'stage') {
          f.index = f.worktree === 'A' ? 'A' : 'M';
          f.untracked = false;
          gitRun('git add -- ' + f.path);
        } else {
          f.index = ' ';
          gitRun('git restore --staged -- ' + f.path);
        }
        renderGit();
      });
    });
  }
  function renderGitLog() {
    $('#gitLog').innerHTML = GIT.log.map((c) => '<div class="git-commit-row">'
      + '<code class="git-hash">' + esc(c.short) + '</code>'
      + '<span class="git-subject" title="' + esc(c.subject) + '">' + esc(c.subject) + '</span>'
      + (c.refs ? '<span class="git-ref">' + esc(c.refs) + '</span>' : '')
      + '<span class="git-meta">' + esc(c.author) + ' \u00b7 ' + esc(c.date) + '</span>'
      + '</div>').join('');
  }
  function renderGitRemotes() {
    const el = $('#gitRemotes');
    el.innerHTML = GIT.remotes.map((r) => '<div class="git-remote"><strong>' + esc(r.name)
      + '</strong><code>' + esc(r.url) + '</code></div>').join('')
      || '<div class="ho-empty sm">no remotes</div>';
    el.innerHTML += '<div class="git-remote-add">'
      + '<input id="gitRemoteUrl" placeholder="https://github.com/user/repo.git" spellcheck="false">'
      + '<button class="ho-btn small" id="gitRemoteAddBtn">Add origin</button>'
      + '</div>';
    $('#gitRemoteAddBtn').addEventListener('click', () => {
      const u = $('#gitRemoteUrl').value.trim();
      if (!u) return;
      const name = GIT.remotes.some((r) => r.name === 'origin') ? 'upstream' : 'origin';
      gitRun('git remote add ' + name + ' ' + u);
      GIT.remotes.push({ name: name, url: u });
      $('#gitRemoteUrl').value = '';
      renderGit();
    });
  }
  function renderGitProjectStatus() {
    const parts = ['<span class="git-badge repo">\u2713 repository</span>',
      '<span class="git-badge branch">\u2387 ' + esc(GIT.branch) + '</span>'];
    if (GIT.upstream) {
      parts.push('<a class="git-badge link" href="https://github.com/SebSilent/Tacit"'
        + ' target="_blank" rel="noopener">SebSilent/Tacit \u2197</a>');
    } else {
      parts.push('<span class="git-badge none">local only, not on GitHub</span>');
    }
    $('#gitProjectStatus').innerHTML = parts.join('');
  }
  function renderGit() {
    renderGitBranchBar();
    renderGitChanges();
    renderGitLog();
    renderGitRemotes();
    renderGitProjectStatus();
  }
  function openGit() {
    gitOverlay.hidden = false;
    gitNote('');
    $('#gitRepo').textContent = GIT.repo;
    const wd = $('#gitWorkdir');
    if (wd && !wd.value) wd.value = GIT.repo;
    const name = $('#gitIdName'), email = $('#gitIdEmail');
    if (name && !name.value) name.value = 'Silent';
    if (email && !email.value) email.value = 'silent@users.noreply.github.com';
    if (!gitConsole.length) {
      gitLine('$ git status');
      gitStatusLines().forEach((l) => gitLine(l));
      gitLine('');
    }
    renderGit();
  }
  function closeGit() { gitOverlay.hidden = true; }

  $('#gitBtn').addEventListener('click', openGit);
  $('#gitClose').addEventListener('click', closeGit);
  gitOverlay.addEventListener('click', (e) => { if (e.target === gitOverlay) closeGit(); });
  $('#gitStageAllBtn').addEventListener('click', () => {
    if (!GIT.files.length) { gitNote('nothing to stage', true); return; }
    gitRun('git add -A');
    GIT.files.forEach((f) => { f.index = f.worktree === ' ' ? 'A' : f.worktree; f.untracked = false; });
    renderGit();
  });
  $('#gitCommitBtn').addEventListener('click', () => {
    const msg = $('#gitCommitMsg').value.trim();
    if (!msg) { gitNote('write a commit message first', true); return; }
    const keep = GIT.files.filter(isStaged);
    if (!keep.length) { gitNote('nothing staged', true); return; }
    gitRun('git commit -m "' + msg + '"');
    gitLine('[' + GIT.branch + ' ' + hashOf(msg).slice(0, 7) + '] ' + msg);
    gitLine(' ' + keep.length + ' file' + (keep.length === 1 ? '' : 's') + ' changed');
    GIT.files = GIT.files.filter((f) => !isStaged(f));
    GIT.log.unshift({
      short: hashOf(msg).slice(0, 7), subject: msg, refs: 'HEAD -> ' + GIT.branch,
      author: 'Silent', date: gitNow(),
    });
    GIT.ahead += 1;
    $('#gitCommitMsg').value = '';
    renderGit();
    gitNote('committed ' + keep.length + ' file' + (keep.length === 1 ? '' : 's'));
  });
  $('#gitRunBtn').addEventListener('click', () => {
    gitRun($('#gitCmd').value);
    $('#gitCmd').value = '';
  });
  $('#gitCmd').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    gitRun($('#gitCmd').value);
    $('#gitCmd').value = '';
  });
  [['gitRefreshBtn', 'git status'], ['gitFetchBtn', 'git fetch origin'],
   ['gitPullBtn', 'git pull'], ['gitPushBtn', 'git push origin master'],
   ['gitSyncBtn', 'git pull && git push origin master']].forEach((pair) => {
    const btn = $('#' + pair[0]);
    if (btn) btn.addEventListener('click', () => { gitRun(pair[1]); renderGit(); });
  });

  // A short hash that is at least a function of the message, so two commits with the
  // same message do not look like the same object.
  function hashOf(text) {
    let h = 5381;
    for (let i = 0; i < text.length; i += 1) h = ((h << 5) + h + text.charCodeAt(i)) >>> 0;
    return h.toString(16);
  }
  function gitNow() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
      + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  // ── page chrome that is not part of the app ────────────────────────────
  $('#demoPause').addEventListener('click', (e) => {
    S.paused = !S.paused;
    e.currentTarget.textContent = S.paused ? 'Resume' : 'Pause';
    e.currentTarget.classList.toggle('on', S.paused);
  });
  $('#demoSpeed').addEventListener('click', (e) => {
    S.speed = S.speed === 1 ? 2 : S.speed === 2 ? 4 : 1;
    e.currentTarget.textContent = S.speed + 'x';
  });
  $('#demoReplay').addEventListener('click', () => {
    if (S.busy) return;
    loadSession(S.session);
    runTurn(SESSIONS[S.session].script);
  });

  // ── boot ───────────────────────────────────────────────────────────────
  $('#brandVersion').hidden = false;
  $('#brandVersion').textContent = 'v' + (window.TACIT_VERSION || '');
  $('#providerSelect').innerHTML = '<option>local</option><option>openai-compatible</option>';
  $('#modelSelect').innerHTML = '<option>glm-5.2</option><option>llama-3.3-70b</option>';
  $('#thinkSelect').innerHTML = '<option>medium</option><option>low</option><option>high</option>';
  $('#wdMenu').innerHTML = '<button class="wd-item active" type="button"><span>C:\\Projects\\Tacit</span></button>'
    + '<button class="wd-item" type="button"><span>C:\\Projects\\scratch</span></button>'
    + '<button class="wd-item" type="button"><span>pick another folder…</span></button>';
  $('#dgMessages').innerHTML = '<div class="as-empty">Nothing is running. When the agent delegates '
    + 'to a sub-agent its trace appears here, in its own context.</div>'
    + '<span class="as-note">Sub-agents run in their own context; only their report reaches '
    + 'the session you are watching.</span>';
  $('#asBudget').textContent = 'runs 0 · 0 tok of 200,000';
  $('#connHint').textContent = 'connected';
  renderAppearance();
  renderPrompts();
  selectTab('appearance');
  settings.hidden = true;
  asPanel.classList.remove('open');
  dgPanel.classList.remove('open');
  $('#steerHint').hidden = true;
  $('#asMessages').innerHTML = '<div class="as-empty">Ask about this session. Draft a prompt, think an '
    + 'approach through, or ask what the agent just did.<br><br>'
    + '<span class="as-note">The agent cannot see this conversation.</span></div>';

  $('#asInput').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey) return;
    e.preventDefault();
    const v = e.currentTarget.value.trim();
    if (!v) return;
    e.currentTarget.value = '';
    asMsg('user', v);
    setTimeout(() => asMsg('assistant', 'Answered from the transcript, not a copy of it: the last '
      + 'turn changed two files and ran 13 tests. Nothing you ask me reaches the agent.'), 900);
  });

  // A system that asks for less motion gets a still frame instead of a turn that
  // writes itself: the strip starts paused, and Replay or Resume starts it on request.
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    S.paused = true;
    const p = $('#demoPause');
    p.textContent = 'Resume';
    p.classList.add('on');
  }

  loadSession(LOOP[0], true);
  setTimeout(async () => {
    if (S.busy) return;
    const ask = SESSIONS[LOOP[0]].ask;
    await typeInto(ask);
    runTurn(SESSIONS[LOOP[0]].script, ask);
  }, 1600);
  idleLoop();
})();
