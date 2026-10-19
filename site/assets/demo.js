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
    pending: null,
  };

  // ── what this page is showing ──────────────────────────────────────────
  // `demo.html` with no query string is the whole running mock: the pointer, the turns,
  // the Assistant, the Delegation panel, Settings. A documentation page asks for one
  // part of it with `?view=name`, and gets a short scene of that part only. The attribute
  // is what `demo.css` reads to drop the sidebar and the control strip.
  const VIEW = (new URLSearchParams(window.location.search).get('view') || '').trim();
  const FRAGMENT = !!VIEW;
  if (FRAGMENT) document.documentElement.dataset.view = VIEW;

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

  // ── the pointer ────────────────────────────────────────────────────────
  // A cursor that belongs to this page. It travels along an arc, presses, and then
  // clicks the real node, so the handler that runs is the one the mock ships: nothing is
  // faked by sliding a picture over a button. It obeys the same Pause and speed gate as
  // the script, and a reader who moves their own mouse is left alone for a few seconds.
  const REDUCED = !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const cur = document.createElement('div');
  cur.className = 'demo-cursor';
  cur.innerHTML = '<svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true">'
    + '<path d="M5 2.5 18.6 10 12.3 11.6 9.4 18.9z" stroke="var(--bg)" stroke-width="1.4"'
    + ' stroke-linejoin="round" fill="var(--text)"/></svg><span class="demo-cursor-ring"></span>';
  document.body.appendChild(cur);

  const curPos = { x: window.innerWidth * 0.55, y: window.innerHeight * 0.3 };
  let curBow = 1, curAnim = 0, curHideUntil = 0;
  curPlace();

  function curPlace() {
    cur.style.transform = 'translate3d(' + curPos.x.toFixed(1) + 'px,'
      + curPos.y.toFixed(1) + 'px,0)';
  }
  function curShow() {
    if (Date.now() >= curHideUntil) cur.classList.add('on');
  }
  const easeInOut = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);

  function curMove(x, y, ms) {
    curShow();
    if (REDUCED) {
      curPos.x = x; curPos.y = y; curPlace();
      return Promise.resolve();
    }
    return new Promise((res) => {
      const fx = curPos.x, fy = curPos.y;
      const dist = Math.hypot(x - fx, y - fy);
      const dur = ms || Math.min(1500, 200 + dist * 2.4);
      // A straight line is mechanical. The path bows sideways, alternating which side,
      // by an amount that scales with the distance: a long throw arcs, a nudge does not.
      curBow = -curBow;
      const nx = -(y - fy) / (dist || 1), ny = (x - fx) / (dist || 1);
      const bow = Math.min(64, dist * 0.16) * curBow;
      let spent = 0, last = 0;
      function frame(now) {
        if (!last) last = now;
        // Pause freezes it in place rather than letting it teleport on resume.
        if (S.paused) { last = now; curAnim = requestAnimationFrame(frame); return; }
        spent += (now - last) * S.speed;
        last = now;
        const p = Math.min(1, spent / dur), e = easeInOut(p);
        curPos.x = fx + (x - fx) * e + nx * bow * Math.sin(Math.PI * p);
        curPos.y = fy + (y - fy) * e + ny * bow * Math.sin(Math.PI * p);
        curPlace();
        if (p < 1) { curAnim = requestAnimationFrame(frame); return; }
        curAnim = 0;
        res();
      }
      if (curAnim) cancelAnimationFrame(curAnim);
      curAnim = requestAnimationFrame(frame);
    });
  }

  async function curTo(node, opt) {
    if (!node) return;
    const o = opt || {};
    // Bring it into view inside its own scroller, then measure: a node that is off-screen
    // has no viewport point worth aiming at.
    if (node.scrollIntoView) node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    await wait(70);
    const r = node.getBoundingClientRect();
    if (r.width < 2 && r.height < 2) return;
    const x = r.left + r.width * (o.fx == null ? 0.5 : o.fx) + (o.dx || 0);
    const y = r.top + r.height * (o.fy == null ? 0.5 : o.fy) + (o.dy || 0);
    await curMove(x, y, o.ms);
  }

  async function curClick(node, opt) {
    if (!node) return;
    await curTo(node, opt);
    cur.classList.add('down');
    await wait(120);
    node.click();
    cur.classList.remove('down');
    cur.classList.add('ping');
    setTimeout(() => cur.classList.remove('ping'), 480);
    await wait(160);
  }

  // A cursor held still looks broken, so it breathes: a few pixels now and then in the
  // direction it last went, never while a move is in flight.
  setInterval(() => {
    if (S.paused || curAnim || !cur.classList.contains('on')) return;
    curMove(curPos.x + (Math.random() * 16 - 8), curPos.y + (Math.random() * 12 - 6), 1300);
  }, 5200);

  window.addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    curHideUntil = Date.now() + 4500;
    cur.classList.remove('on');
  }, true);

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
  // The thread is the app's own markup, not the transcript's: `.as-msg.me` is the
  // reader's line, `.as-msg.them` the assistant's, markdown into `.as-body.md`, and the
  // assistant's own tool calls in `.as-tools`. An answer that ends in a fenced block
  // grows the same `.as-acts` row the real panel does, with "Send to agent" on it, and
  // that button is the whole point of the panel: what it carries starts a turn.
  const AS_MSGS = [];
  let AS_TOOLS = [];

  function asToolsHtml() {
    if (!AS_TOOLS.length) return '';
    return '<div class="as-tools">' + AS_TOOLS.map((t) =>
      '<div class="tool-card' + (t.open ? ' open' : '') + (t.err ? ' err' : '') + '">'
      + '<div class="tool-head"><span class="tool-badge">' + esc(t.name) + '</span>'
      + '<span class="tool-args">' + esc(t.args) + '</span></div>'
      + '<div class="tool-body"><pre>' + esc(t.text || 'running\u2026') + '</pre></div>'
      + '</div>').join('') + '</div>';
  }

  function asRender() {
    const box = $('#asMessages');
    if (!AS_MSGS.length) {
      box.innerHTML = '<div class="as-empty">Ask about this session. Draft a prompt, think an '
        + 'approach through, or ask what the agent just did.<br><br>'
        + '<span class="as-note">The agent cannot see this conversation.</span></div>';
      return;
    }
    box.innerHTML = AS_MSGS.map((m, i) => {
      const mine = m.role === 'user';
      // The same rule the app uses: only an answer that actually offers a fenced prompt
      // can be handed over, so the button never sends prose the agent would misread.
      const offers = /```[\s\S]*?\S[\s\S]*?```/.test(m.content || '');
      const acts = mine ? '' : '<div class="as-acts">'
        + (offers ? '<button class="ho-btn small" data-send="' + i
          + '" title="Hand this prompt to the agent now">Send to agent</button>' : '')
        + '<button class="ho-btn small" data-del="' + i
        + '" title="Remove this message from the assistant\'s memory">delete</button></div>';
      return '<div class="as-msg ' + (mine ? 'me' : 'them') + '">'
        + (i === AS_MSGS.length - 1 ? asToolsHtml() : '')
        + '<div class="as-body' + (mine ? '' : ' md') + '">'
        + (mine ? esc(m.content) : md(m.content)) + '</div>' + acts + '</div>';
    }).join('');
    box.scrollTop = box.scrollHeight;
  }

  // The fenced block is what travels, exactly as on the server: the index travels to the
  // handler and the text is pulled out of the stored message, so the transcript and the
  // panel cannot drift into showing two different prompts.
  function asFenced(text) {
    const m = /```[\s\S]*?\n([\s\S]*?)```/.exec(text || '');
    return m ? m[1].trim() : '';
  }

  function asHandoff(index) {
    const prompt = asFenced((AS_MSGS[index] || {}).content);
    if (!prompt) return;
    if (S.busy) { toast('a turn is already running in this session', true); return; }
    runTurn(SESSIONS[S.session].script, prompt);
  }

  // One delegated handler over the thread, like the app's: the list re-renders on every
  // change, so a listener per button would have to be re-attached each time.
  $('#asMessages').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]');
    if (del) { AS_MSGS.splice(parseInt(del.dataset.del, 10), 1); asRender(); return; }
    const btn = e.target.closest('[data-send]');
    if (btn) asHandoff(parseInt(btn.dataset.send, 10));
  });

  function asAskText(text) {
    AS_MSGS.push({ role: 'user', content: text });
    asRender();
  }

  // The answer streams into the last body, which is what the app does while a run is in
  // flight; the full re-render at the end is what attaches `.as-acts`.
  async function asAnswer(lead, prompt, tools) {
    AS_TOOLS = [];
    if (tools) {
      AS_TOOLS = tools.map((t) => ({ name: t.name, args: t.args, text: '', open: true }));
      AS_MSGS.push({ role: 'assistant', content: '' });
      asRender();
      for (let i = 0; i < AS_TOOLS.length; i += 1) {
        await wait(520);
        AS_TOOLS[i].text = tools[i].text;
        asRender();
      }
      await wait(320);
    }
    AS_MSGS.push({ role: 'assistant', content: '' });
    asRender();
    const full = lead + '\n\n```text\n' + prompt + '\n```';
    const body = $$('#asMessages .as-body').pop();
    let shown = '';
    for (let i = 0; i < full.length; i += 4) {
      shown = full.slice(0, i + 4);
      body.innerHTML = md(shown);
      $('#asMessages').scrollTop = $('#asMessages').scrollHeight;
      await wait(16);
    }
    AS_TOOLS = [];
    AS_MSGS[AS_MSGS.length - 1].content = full;
    asRender();
  }

  // What a reader can do by hand, the pointer does on its own, through the same handler:
  // open the panel, type the question, press Send, wait for the answer to finish
  // streaming, then click "Send to agent". The turn starts from that click, so this
  // function never calls runTurn and never has to know which session is open.
  async function consultAssistant(spec) {
    if (!asPanel.classList.contains('open')) await curClick($('#asToggle'));
    await curTo($('#asInput'));
    await typeIntoEl($('#asInput'), spec.ask);
    await curClick($('#asSend'));
    // The button exists only once the answer has landed with a fenced block in it, so
    // waiting for it is waiting for the stream, with no second clock to keep in sync.
    let btn = null;
    for (let i = 0; i < 300 && !btn; i += 1) {
      await wait(120);
      btn = $('#asMessages').querySelector('[data-send]');
    }
    if (!btn) return spec.prompt;
    await wait(900);
    await curClick(btn);
    return spec.prompt;
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

  // What the assistant is asked, and the prompt it hands back. The prompt is the fenced
  // block, so the text the transcript receives is the text on screen rather than a second
  // copy of it that could disagree.
  const CONSULT = {
    'shell-timeout': {
      ask: 'What should I do first here? The shell tool times out but the children keep running.',
      tools: [{
        name: 'read',
        args: 'backend/tools/shell.py:88-140',
        text: 'def run(cmd, timeout=180):\n    proc = Popen(argv, ...)\n'
          + '    try:\n        proc.wait(timeout)\n    except TimeoutExpired:\n'
          + '        proc.kill()          # the parent only\n',
      }],
      lead: 'Two things are already true: the timeout path kills the parent and nothing walks '
        + 'the tree. Fix the failing case first, because the test is what tells you the '
        + 'difference between a kill and a cleanup.\n\nName the behaviour precisely: a command '
        + 'that outlives its timeout must leave no descendant behind. Then replace the single '
        + '`proc.kill()` with a tree kill and bound the drain, so a child that ignores the '
        + 'signal cannot hold the turn open forever.',
      prompt: 'The shell tool kills a timed-out command but the children keep running. Write a '
        + 'failing test that proves a descendant survives the timeout, then replace proc.kill() '
        + 'with a process-tree kill and bound the drain. Run the full suite last.',
    },
    'audit-rotation': {
      ask: 'The audit ledger never rotates. Where do I start without losing the current file?',
      tools: [{
        name: 'grep',
        args: 'ledger path in backend/',
        text: 'backend/audit.py:42   def append(event)\n'
          + 'backend/audit.py:58   LEDGER = home / ".tacit" / "audit.jsonl"\n',
      }],
      lead: 'Rotation belongs on the write path, not in a background job nobody installs. '
        + 'The constraint that decides the design is that a reader must never see a torn '
        + 'line, so the switch is a rename and not a truncate.\n\nMove the full file to a '
        + 'dated sibling, start an empty ledger, and keep a fixed number of old files so the '
        + 'folder cannot grow forever. The race to test is two appends crossing the size '
        + 'threshold at once.',
      prompt: 'The audit ledger never rotates. Add size-based rotation to the append path: '
        + 'rename a full file to a dated sibling, start a fresh ledger, keep the last four, '
        + 'and never leave a torn line readable. Test the rename race.',
    },
    'prompt-cost': {
      ask: 'How much of the fixed prompt is the tool list, and can I read it anywhere?',
      tools: [{
        name: 'grep',
        args: 'prompts_catalog.py',
        text: 'backend/prompts_catalog.py:1   GROUPS = [...]\n'
          + 'backend/prompts_catalog.py:88   def live_blocks(session)\n',
      }],
      lead: 'The fixed prompt is 14 groups. The tool list is the largest single one, and it '
        + 'is measured rather than guessed: the same counter the session uses writes the '
        + 'number down at build time.\n\nWhat you can read is everything that goes out. There '
        + 'is a tab for it in Settings, and it is generated from the constants that send it, '
        + 'so it cannot show you a friendlier version of the prompt than the model sees. Ask '
        + 'for the breakdown by group and you get the same figures the meter uses.',
      prompt: 'Break the fixed prompt down by group and say how many entries and roughly how '
        + 'many tokens the tool list alone costs. Point at the file that holds each group, and '
        + 'show me where in the interface I can read the exact text that goes out.',
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
    finishTurn(11840);
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
    finishTurn(6120);
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
    finishTurn(2140);
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
    finishTurn(4180);
  }

  // The turn ends in the transcript, not in the Assistant. The panel used to be opened by
  // the script to announce a cost it had not been asked about, which is the one thing the
  // real assistant never does: it answers when it is spoken to.
  function finishTurn(spent) {
    setTokens(S.tokens + spent);
    $('#topMeta').textContent = 'Agent · local / ' + SESSIONS[S.session].model
      + ' · ' + comma(spent) + ' tok';
    $('#asBudget').textContent = 'runs ' + S.run + ' · ' + comma(S.tokens)
      + ' tok of ' + comma(CTX);
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
  let loopAt = 0, compacted = false, toured = false;
  // Each session is consulted through the Assistant once. After that the loop types the
  // ask itself, so both routes through the interface are on the page every few minutes.
  const CONSULTED = {};

  let firstPass = true;

  // A short walk through the advanced half of Settings: open it, switch to General, turn
  // on the mid-turn sounds, make the transcript roomy, put it back, close. Every step
  // goes through the same handler a reader's own click would hit, and the density change
  // really does change the transcript, because it sets the attribute the app's stylesheet
  // reads.
  async function settingsTour() {
    await curClick($('#harnessBtn'));
    await curClick($('#hoTabs [data-tab="general"]'));
    await wait(400);
    await curClick($('#panel-general [data-perm="alert-sound"] .tgl'));
    await wait(700);
    await curClick($('#densitySeg [data-dens="roomy"]'));
    await wait(1400);
    await curClick($('#densitySeg [data-dens="default"]'));
    await curClick($('#hoClose'));
  }

  async function idleLoop() {
    while (true) {
      await wait(6500);
      if (S.busy) continue;
      // The first pass is the session the page booted on, so the loop only moves once it
      // has run something.
      if (!firstPass) loopAt = (loopAt + 1) % LOOP.length;
      firstPass = false;
      if (!compacted && S.run > 0 && loopAt === 0) {
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
      const key = LOOP[loopAt];
      if (!toured) { toured = true; await settingsTour(); }
      loadSession(key, true);
      if (CONSULT[key] && !CONSULTED[key]) {
        CONSULTED[key] = true;
        // The pointer asks the assistant, reads the answer, and clicks "Send to agent".
        // That click is what starts the turn, so nothing below has to know it happened.
        await consultAssistant(CONSULT[key]);
        continue;
      }
      await curTo(composerEl);
      await typeIntoEl(composerEl, SESSIONS[key].ask);
      S.pending = SESSIONS[key].script;
      await curClick($('#sendBtn'));
    }
  }

  // The ask is typed before it is sent, because watching a message arrive already written
  // is watching a screenshot, not a session. The composer and the Assistant input share
  // this: one typer, whichever field the pointer is standing over. Nothing here clears
  // the field: the button does that, the same way it does for a reader's own typing.
  async function typeIntoEl(el, text) {
    el.value = '';
    if (el.focus) el.focus();
    for (const ch of text) {
      el.value += ch;
      if (el === composerEl) autoGrow();
      await wait(15);
    }
    await wait(420);
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
    // The scripted loop types into this field and clicks this same button, so it leaves
    // behind the turn it wants run. A reader's own message gets the generic answer.
    const script = S.pending || scriptGeneric;
    S.pending = null;
    runTurn(script, text);
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
    hoNote(tab === 'prompts'
      ? 'Every character that goes to a model, read from the constants that send it.'
      : 'Mock panel. The real Settings writes to the state folder.');
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

  // ── Settings, the advanced half ────────────────────────────────────────
  // These tabs are the app's own structure, taken from `static/platform.js` and
  // `static/harness.js`: `.ho-section` headers, `.prof-row` profiles, `.tool-row`
  // permissions with a `.tgl` switch, the turn budget as number inputs with the bounds
  // the server clamps to, `.prov-card` providers over a `.model-list`, and the built-in
  // tool list with its shell-overlap select. The tool names are the real 22 from
  // `backend/agent.py`; the numbers and the shorter descriptions are invented, because
  // there is no server here to ask. Every switch moves: a settings panel you cannot
  // change is a brochure.
  const CFG = {
    profile: 'default',
    profiles: [
      { name: 'default', label: 'Default', cost: '14,800',
        desc: 'The balance every figure in the docs is measured against.' },
      { name: 'cheap', label: 'Cheap', cost: '6,200',
        desc: 'Fewer tools offered, thinking off, a smaller budget per turn.' },
      { name: 'thorough', label: 'Thorough', cost: '31,400',
        desc: 'Every tool, thinking on, roomy budget, sub-agents encouraged.' },
    ],
    perms: [
      { key: 'version-control', name: 'Version control', on: false,
        desc: 'Off by default. Left to itself a model commits and pushes far more than '
          + 'anyone asked for. The Git panel is the deliberate route.' },
      { key: 'deliver-guarantee', name: 'Delivery guarantee', on: true,
        desc: 'On by default. When a turn would end without the file the task names, the '
          + 'harness orders it written and spends the last rounds landing it.' },
      { key: 'turn-sound', name: 'Turn-end sound', on: true,
        desc: 'A short two-note chime when a turn finishes: rising for a finished turn, '
          + 'lower for one that ended on an error.' },
      { key: 'alert-sound', name: 'Mid-turn sounds', on: false,
        desc: 'Off by default. A drop in pitch at the budget warning, one tick each time '
          + 'an autonomous cycle starts. These repeat on their own.' },
      { key: 'autoupdater-enabled', name: 'Auto-updater', on: true,
        desc: 'Checks GitHub for new commits every hour and can pull changed files. Tacit '
          + 'has no releases, so every push is an update.' },
    ],
    budget: [
      { key: 'max_steps', label: 'Steps per turn', value: 60, min: 4, max: 400,
        hint: 'Tool calls one turn may take before it is asked to wrap up.' },
      { key: 'subagent_steps', label: 'Steps per sub-agent', value: 30, min: 1, max: 200,
        hint: 'The same cap, applied to one delegated task.' },
      { key: 'research_steps', label: 'Steps per research aspect', value: 12, min: 1, max: 40,
        hint: 'The same cap, applied to one aspect of a research sweep.' },
      { key: 'turn_token_budget', label: 'Token cap per turn', value: 200000,
        min: 0, max: 2000000, hint: 'Tokens one turn may bill before it stops and reports. 0 means no cap.' },
    ],
    density: 'default',
    overlap: 'block',
    fallback: '',
    processes: [
      { role: 'server', pid: '18224', started: '16:31' },
      { role: 'turn worker', pid: '18902', started: '16:33' },
    ],
    tools: [
      ['list_files', 'List a folder, relative paths resolve against the workspace.', true],
      ['read_file', 'Read a file as numbered lines, with a window and a continuation offset.', true],
      ['write_file', 'Create or overwrite a file.', true],
      ['edit_file', 'Replace one snippet that appears exactly once.', true],
      ['glob_files', 'Find files by name pattern.', true],
      ['grep_files', 'Search file contents with a regular expression.', true],
      ['run_shell', 'Run a command in the workspace, with a timeout and a reported exit code.', true],
      ['skill', 'Read a skill\'s full instructions before following it.', true],
      ['bg_start', 'Start a long-running command in the background.', true],
      ['bg_output', 'Read a background job and whether it is still running.', true],
      ['bg_stop', 'Stop a background job.', true],
      ['fetch', 'Fetch a URL and return its text.', true],
      ['browser', 'Drive a real browser: open, click, type, screenshot.', true],
      ['research', 'Bounded multi-source check, then one cited report.', true],
      ['evidence', 'Record and re-read a citable fact.', true],
      ['task', 'Delegate a focused investigation to a sub-agent.', true],
      ['mcp_list_servers', 'One of four helper schemas, hidden until an MCP server is added.', false],
      ['mcp_search_tools', 'Hidden with no server configured.', false],
      ['mcp_activate_tools', 'Hidden with no server configured.', false],
      ['mcp_call', 'Hidden with no server configured.', false],
      ['memory_recall', 'Read the long-term memory store.', false],
      ['memory_add', 'Write to it. Off unless the memory mode is on.', false],
    ],
  };
  const SKILLS = [
    { name: 'commit-message-style', kind: 'skill',
      desc: 'Writes a commit message the way this repository does it: one short clause, no ticket noise.',
      body: '# Steps\n\n1. Name what changed in three words or fewer.\n'
        + '2. Never write "fix bug" without naming the bug.\n3. No body unless the why is not obvious.\n' },
    { name: 'prose-denylist', kind: 'skill',
      desc: 'Checks documentation prose against scripts/check_prose.py before a commit.',
      body: '# Rule\n\nEm dashes, hollow adjectives and hedged claims are banned in prose.\n'
        + 'Run the checker on the files you touched, not only the docs set.\n' },
    { name: 'theme-tokens', kind: 'knowledge',
      desc: 'Where the palette lives and which test measures its contrast.',
      body: 'static/style.css holds one token block per theme.\n'
        + 'tests/test_style_themes.py measures contrast and hue distance; it counts themes, so a new\n'
        + 'palette has to be added to its expected number.\n' },
  ];
  const PERM = {};
  CFG.perms.forEach((p) => { PERM[p.key] = p.on; });
  const TOOL = CFG.tools.map((t) => ({ name: t[0], desc: t[1], on: t[2] }));
  const mcpLive = false;

  function hoNote(text, err) {
    const el = $('#hoNote');
    el.textContent = text;
    el.classList.toggle('err', !!err);
  }

  function permRow(p) {
    const on = PERM[p.key];
    return '<div class="tool-row ' + (on ? '' : 'off') + '" data-perm="' + esc(p.key) + '">'
      + '<span class="tool-name">' + esc(p.name) + '</span>'
      + '<span class="tool-desc">' + esc(p.desc) + '</span>'
      + '<button class="tgl ' + (on ? 'on' : '') + '" data-act="toggle-perm" role="switch"'
      + ' aria-checked="' + on + '" title="' + (on ? 'Disable' : 'Enable') + '">'
      + '<span class="tgl-knob"></span></button></div>';
  }

  function renderGeneral() {
    const profiles = CFG.profiles.map((p) => '<div class="prof-row '
      + (p.name === CFG.profile ? 'on' : '') + '" data-name="' + esc(p.name) + '">'
      + '<span class="prof-name">' + esc(p.label) + '</span>'
      + '<span class="badge">' + esc(p.cost) + ' tokens</span>'
      + (p.name === CFG.profile ? '<span class="badge on">active</span>'
        : '<button class="ho-btn small" data-act="apply">Apply</button>')
      + '<span class="prof-desc">' + esc(p.desc) + '</span></div>').join('');
    const budget = CFG.budget.map((b) => '<label class="ho-sub sm" title="' + esc(b.hint) + '">'
      + esc(b.label) + '<input class="mcp-in sm" id="lim-' + esc(b.key) + '" type="number"'
      + ' min="' + b.min + '" max="' + b.max + '" value="' + b.value + '"></label>').join('');
    const procs = '<div class="tok-table">' + CFG.processes.map((p) =>
      '<div class="tok-row"><span class="tok-label">' + esc(p.role) + '</span>'
      + '<span class="tok-value">pid ' + esc(p.pid) + '</span>'
      + '<span class="tok-hint">started ' + esc(p.started) + '</span></div>').join('')
      + '</div>';

    $('#panel-general').innerHTML =
      '<div class="ho-section">Profile <span class="ho-sub sm">a named bundle of cost and containment</span></div>'
      + '<div class="prof-list">' + profiles + '</div>'
      + '<div class="mcp-add"><input class="mcp-in" id="profName"'
      + ' placeholder="save current setup as…" spellcheck="false">'
      + '<button class="ho-btn small" id="profSave">Save profile</button>'
      + '<span class="ho-sub sm">Switching a profile applies immediately.</span></div>'
      + '<div class="ho-section">Permissions <span class="ho-sub sm">what the agent may do on its own</span></div>'
      + CFG.perms.slice(0, 2).map(permRow).join('')
      + '<div class="tool-row" data-perm="fallback-model"><span class="tool-name">Fallback model</span>'
      + '<span class="tool-desc">No fallback by default. A session whose model is gone runs on '
        + 'this one instead; with none the turn stops and says which model it could not find.</span>'
      + '<div class="as-row as-pickers"><select class="ctl-select as-sel" id="fbProvider">'
      + '<option value="">(no fallback)</option><option value="local">local</option>'
      + '<option value="openrouter">openrouter</option></select>'
      + '<select class="ctl-select as-sel" id="fbModel"><option value="">(no fallback)</option>'
      + '<option value="local:glm-5.2">local/glm-5.2</option>'
      + '<option value="local:qwen2.5-coder:14b">local/qwen2.5-coder:14b</option></select></div></div>'
      + '<div class="ho-section">Interface</div>'
      + CFG.perms.slice(2, 4).map(permRow).join('')
      + '<div class="tool-row" data-perm="chime-audition"><span class="tool-name">Chime audition</span>'
      + '<span class="tool-desc">Plays all four past both switches, so you hear what they control.'
      + ' This page makes no sound.</span>'
      + '<button class="ho-btn small" id="chimePreviewBtn">Hear them</button></div>'
      + '<div class="tool-row" data-perm="density"><span class="tool-name">Transcript density</span>'
      + '<span class="tool-desc">How much vertical room the transcript gives its rows. A view '
        + 'preference, stored in this browser only.</span>'
      + '<div class="seg" id="densitySeg"><button data-dens="default">Default</button>'
      + '<button data-dens="compact">Compact</button><button data-dens="roomy">Roomy</button></div></div>'
      + '<div class="ho-section">Server</div>'
      + '<div class="tool-row" data-perm="restart"><span class="tool-name">Restart</span>'
      + '<span class="tool-desc">listening on 127.0.0.1:8571 · pid 18224. Stop this process and '
        + 'start a fresh one on the same port; a turn that is running is cut off.</span>'
      + '<button class="ho-btn small danger" id="srvRestartBtn">Restart</button></div>'
      + '<div class="tool-row" data-perm="mcp-helpers"><span class="tool-name">MCP helper tools</span>'
      + '<span class="tool-desc">Hidden, no MCP server configured. The four helper schemas cost '
        + 'nothing and appear the moment a server is added in the MCP tab.</span>'
      + '<span class="badge">off</span></div>'
      + '<div class="ho-section">Turn budget</div>'
      + '<div class="mcp-add">' + budget
      + '<button class="ho-btn small" id="limSave">Save</button></div>'
      + '<div class="ho-section">Tacit processes <span class="ho-count">'
      + CFG.processes.length + '</span></div>' + procs
      + '<div class="ho-section">Auto-Updater</div>' + permRow(CFG.perms[4])
      + '<div class="tool-row" data-perm="autoupdater-check"><span class="tool-name">Check for updates</span>'
      + '<span class="tool-desc">Manually check GitHub for new commits and changed files.</span>'
      + '<button class="ho-btn small" id="auCheckBtn">Check now</button></div>';

    $$('#densitySeg button').forEach((b) => b.classList.toggle('active', b.dataset.dens === CFG.density));
    wireGeneral();
  }

  function applyDensity(id) {
    CFG.density = (id === 'compact' || id === 'roomy') ? id : 'default';
    if (CFG.density === 'default') delete document.documentElement.dataset.density;
    else document.documentElement.dataset.density = CFG.density;
    $$('#densitySeg button').forEach((b) => b.classList.toggle('active', b.dataset.dens === CFG.density));
    hoNote('density: ' + CFG.density + ' (this browser only, nothing about the session changes)');
  }

  function wireGeneral() {
    $$('#panel-general [data-act="toggle-perm"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const row = btn.closest('.tool-row');
        const key = row.dataset.perm;
        PERM[key] = !PERM[key];
        row.classList.toggle('off', !PERM[key]);
        btn.classList.toggle('on', PERM[key]);
        btn.setAttribute('aria-checked', String(PERM[key]));
        btn.title = PERM[key] ? 'Disable' : 'Enable';
        hoNote(key + ' ' + (PERM[key] ? 'on' : 'off') + ' (mock: nothing is written to disk)');
      });
    });
    $$('#panel-general [data-act="apply"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const row = btn.closest('.prof-row');
        CFG.profile = row.dataset.name;
        renderGeneral();
        hoNote('profile ' + CFG.profile + ' applied to new turns');
      });
    });
    $('#profSave').addEventListener('click', () => {
      const name = $('#profName').value.trim();
      if (!name) { hoNote('name the profile first', true); return; }
      CFG.profiles.push({ name: name, label: name, cost: '14,800',
        desc: 'Saved from the mock interface; it is not on your disk.' });
      $('#profName').value = '';
      renderGeneral();
      hoNote('saved profile ' + name);
    });
    $('#chimePreviewBtn').addEventListener('click', () => {
      hoNote('this page makes no sound: the four chimes are finished, error, budget warning and cycle tick');
    });
    $('#densitySeg').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) applyDensity(b.dataset.dens);
    });
    $('#srvRestartBtn').addEventListener('click', () => {
      hoNote('a mock cannot restart a server that is not running', true);
    });
    $('#limSave').addEventListener('click', () => {
      const saved = CFG.budget.map((b) => {
        const el = $('#lim-' + b.key);
        const clamped = Math.max(b.min, Math.min(b.max, parseInt(el.value, 10) || 0));
        b.value = clamped;
        el.value = String(clamped);
        return b.label.toLowerCase() + ' ' + comma(clamped);
      }).join(', ');
      hoNote('saved: ' + saved + ' (mock, nothing is written to disk)');
    });
    $('#auCheckBtn').addEventListener('click', () => {
      hoNote('up to date, version ' + (window.TACIT_VERSION || '') + ', nothing to pull');
    });
    $('#fbProvider').addEventListener('change', (e) => {
      CFG.fallback = e.target.value;
      hoNote(e.target.value ? 'fallback provider ' + e.target.value
        + ' chosen, pick a model beside it' : 'no fallback: a missing model stops the turn and says so');
    });
    $('#fbModel').addEventListener('change', (e) => {
      CFG.fallback = e.target.value;
      hoNote(e.target.value ? 'fallback ' + e.target.value + ' (mock)' : 'no fallback');
    });
  }

  const PROV = {
    name: 'local',
    base: 'http://127.0.0.1:8080/v1',
    api: 'openai',
    models: [{ id: 'glm-5.2', ctx: '128k', def: true },
      { id: 'qwen2.5-coder:14b', ctx: '32k', def: false }],
    probe: '',
  };

  function renderProviders() {
    const rows = PROV.models.map((m) => '<div class="model-row '
      + (m.def ? 'is-default' : '') + '" data-model="' + esc(m.id) + '">'
      + '<span class="model-id">' + esc(m.id) + '</span>'
      + '<span class="model-ctx">' + esc(m.ctx || '') + '</span><span class="model-spacer"></span>'
      + (m.def ? '<span class="badge default">default</span>'
        : '<button class="ho-btn small" data-act="set-default">Set default</button>')
      + '<button class="ho-btn small danger" data-act="del-model" title="Remove">×</button></div>'
      ).join('');
    $('#panel-providers').innerHTML =
      '<div class="ho-toolbar"><span class="ho-sub">Registry: <code>~/.tacit/models.json</code></span>'
      + '<button class="ho-btn primary" id="pvAddBtn">Add provider</button></div>'
      + '<div class="card prov-card" data-prov="' + esc(PROV.name) + '">'
      + '<div class="card-head prov-head"><div class="prov-name">' + esc(PROV.name)
      + ' <span class="badge key">no-key</span></div>'
      + '<div class="card-actions"><button class="ho-btn small" data-act="probe">Re-scan</button>'
      + '<button class="ho-btn small danger" data-act="del">Delete</button></div></div>'
      + '<div class="prov-meta"><code>' + esc(PROV.base) + '</code> · ' + esc(PROV.api) + '</div>'
      + '<div class="model-list">' + (rows
        || '<div class="ho-empty sm">No models discovered, hit Re-scan to pull the live list from the endpoint.</div>')
      + '</div><div class="model-add"><input class="model-add-id" placeholder="model id, e.g. qwen2.5-coder:7b"'
      + ' spellcheck="false"><input class="model-add-name" placeholder="display name (optional)"'
      + ' spellcheck="false"><input class="model-add-ctx" placeholder="context window (tokens)"'
      + ' spellcheck="false"><label class="chk"><input type="checkbox" class="model-add-reason" checked>'
      + ' reasoning</label><button class="ho-btn small primary" data-act="add-model">Add model</button></div>'
      + '<div class="probe-results"' + (PROV.probe ? '' : ' hidden') + '><span class="ho-sub">'
      + esc(PROV.probe) + '</span></div></div>';

    $$('#panel-providers [data-act="set-default"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.closest('.model-row').dataset.model;
        PROV.models.forEach((m) => { m.def = m.id === id; });
        renderProviders();
        hoNote('default model ' + id + ' (mock registry, nothing is written to disk)');
      });
    });
    $$('#panel-providers [data-act="del-model"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.closest('.model-row').dataset.model;
        PROV.models = PROV.models.filter((m) => m.id !== id);
        if (!PROV.models.some((m) => m.def) && PROV.models.length) PROV.models[0].def = true;
        renderProviders();
        hoNote('removed ' + id + ' from the mock registry');
      });
    });
    $('#panel-providers [data-act="probe"]').addEventListener('click', () => {
      PROV.probe = 'Endpoint reachable, ' + PROV.models.length
        + ' model(s), all already added.';
      renderProviders();
      hoNote('re-scanned ' + PROV.base);
    });
    $('#panel-providers [data-act="add-model"]').addEventListener('click', () => {
      const id = $('#panel-providers .model-add-id').value.trim();
      if (!id) { hoNote('a model id first', true); return; }
      PROV.models.push({ id: id, ctx: '', def: false });
      renderProviders();
      hoNote('added ' + id + ' to the mock registry');
    });
    $('#pvAddBtn').addEventListener('click', () => {
      hoNote('one provider is enough for a mock: the real form asks for a base URL and a key', true);
    });
  }

  function renderTools() {
    const row = (t) => {
      const gated = t.name.indexOf('mcp_') === 0 && !mcpLive;
      return '<div class="tool-row ' + (t.on && !gated ? '' : 'off') + '" data-name="'
        + esc(t.name) + '"><span class="tool-name">' + esc(t.name) + '</span>'
        + '<span class="tool-desc">' + esc(t.desc)
        + (gated ? ' <b>Hidden from the agent, no MCP server configured.</b>' : '')
        + '</span>'
        + (gated ? '<span class="badge">gated</span>'
          : '<button class="tgl ' + (t.on ? 'on' : '') + '" data-act="toggle" role="switch"'
            + ' aria-checked="' + t.on + '" title="' + (t.on ? 'Disable' : 'Enable') + '">'
            + '<span class="tgl-knob"></span></button>') + '</div>';
    };
    const offered = TOOL.filter((t) => t.on && !(t.name.indexOf('mcp_') === 0)).length;
    $('#panel-tools').innerHTML =
      '<div class="ho-toolbar"><span class="ho-sub">22 tools in the registry, '
      + offered + ' offered to this session. A profile and four rules decide the rest.</span></div>'
      + '<div class="ho-section">Shell overlap <span class="ho-sub sm">commands that re-implement a tool</span></div>'
      + '<div class="mcp-add"><select class="git-select" id="toolOverlap">'
      + '<option value="block">Block, refuse the command, return the tool call</option>'
      + '<option value="advise">Advise, run it, append one line</option>'
      + '<option value="off">Off, no interception</option></select>'
      + '<button class="ho-btn small" id="toolOverlapSave">Save</button></div>'
      + '<div class="ho-section">Built-in tools <span class="ho-count">' + TOOL.length + '</span></div>'
      + '<div class="tool-list">' + TOOL.map(row).join('') + '</div>'
      + '<div class="ho-section">Skills <span class="ho-count">' + SKILLS.length + '</span></div>'
      + SKILLS.map((s) => '<div class="card skill-card" data-name="' + esc(s.name) + '">'
        + '<div class="card-head skill-head"><span class="skill-name">' + esc(s.name) + '</span>'
        + '<span class="badge">' + esc(s.kind) + '</span>'
        + '<button class="ho-btn small danger skill-del" title="Delete">Delete</button></div>'
        + '<div class="skill-desc">' + esc(s.desc) + '</div>'
        + '<details class="skill-body"><summary>content</summary><pre>' + esc(s.body)
        + '</pre></details></div>').join('');
    $('#toolOverlap').value = CFG.overlap;

    $$('#panel-tools [data-act="toggle"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const r = btn.closest('.tool-row');
        const t = TOOL.find((x) => x.name === r.dataset.name);
        t.on = !t.on;
        hoNote(t.name + ' ' + (t.on ? 'enabled' : 'disabled') + ' (applies to new agent sessions)');
        renderTools();
      });
    });
    $('#toolOverlapSave').addEventListener('click', () => {
      CFG.overlap = $('#toolOverlap').value;
      hoNote('shell overlap: ' + CFG.overlap + ' (applies to new agent turns)');
    });
    $$('#panel-tools .skill-del').forEach((btn) => {
      btn.addEventListener('click', () => {
        const name = btn.closest('.skill-card').dataset.name;
        SKILLS.splice(SKILLS.findIndex((s) => s.name === name), 1);
        renderTools();
        hoNote('removed ' + name + ' (mock: nothing is deleted from your disk)');
      });
    });
  }

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

  // ── fragments for the documentation pages ──────────────────────────────
  // A docs page asks for one view with `?view=name` and gets a short scene of that part
  // and nothing else. No Assistant, no Delegation, no idle loop: a 320-pixel frame
  // cannot show a whole session honestly, and a scene that needs the rest of the app to
  // make sense is a screenshot nobody can read.
  async function sceneBasic() {
    loadSession('shell-timeout', true);
    await curTo(composerEl);
    await typeIntoEl(composerEl, SESSIONS['shell-timeout'].ask);
    S.pending = scriptLite;
    await curClick($('#sendBtn'));
  }

  async function scriptLite() {
    const row = addAgent();
    await streamReason(row, 'The ask names one behaviour, so start where the timeout is handled '
      + 'rather than in the tests. Read what it does to the children, then write the case that '
      + 'proves it wrong.');
    await tool(row, 'grep_files', 'pattern: "TimeoutExpired" in backend/', {
      ms: 1100, tokens: 620, meta: '1.1s · 3 files',
    });
    await tool(row, 'read_file', 'backend/tools/shell.py, offset 96', {
      ms: 900, tokens: 1180, meta: '0.9s · 62 lines',
    });
    snapshot(row, 'snap-01', 2);
    await tool(row, 'edit_file', 'backend/tools/shell.py:112', {
      ms: 800, tokens: 240, meta: '0.8s · replaced 1 line',
    });
    await tool(row, 'run_shell', 'python -m unittest discover -s tests -t .', {
      ms: 3600, tokens: 2100, meta: '3.6s · exit 0',
      live: [{ at: 1100, text: '.' }, { at: 2200, text: '................' }],
      result: 'Ran 1315 tests in 84.419s\n\nOK (skipped=1)',
    });
    await content(row, 'The timeout path now kills the process tree and bounds the drain, so a '
      + 'child that ignores the signal cannot hold the turn open. The new test fails without the '
      + 'change and passes with it.');
    finishTurn(5240);
  }

  async function sceneTranscript() {
    loadSession('prompt-cost', true);
    const row = addAgent();
    await streamReason(row, 'The question is about size, so measure it instead of recalling it: '
      + 'the prompt is a list of groups, and the counter the session uses is the same one.');
    await content(row, 'The fixed prompt is 14 groups and 89 entries. The tool list is the largest '
      + 'single group, and every character of it is readable under Settings, because the panel is '
      + 'generated from the constants that send it.');
    finishTurn(2140);
  }

  async function sceneTools() {
    loadSession('shell-timeout', true);
    const row = addAgent();
    await tool(row, 'grep_files', 'pattern: "TimeoutExpired" in backend/', {
      ms: 900, tokens: 480, meta: '0.9s · 3 files',
    });
    await tool(row, 'read_file', 'backend/tools/shell.py', {
      ms: 700, tokens: 1180, meta: '0.7s · 62 lines', open: true,
      result: '96\t    try:\n97\t        proc.wait(timeout)\n98\t    except TimeoutExpired:\n'
        + '99\t        proc.kill()   # the parent only\n',
    });
    await tool(row, 'browser', 'open https://example.com', {
      err: true, ms: 600, meta: '0.6s · no browser installed',
      result: 'browser: this tool needs a driver you install yourself; nothing was fetched',
    });
    await tool(row, 'edit_file', 'backend/tools/shell.py:112', {
      ms: 500, tokens: 240, meta: '0.5s · replaced 1 line',
    });
  }

  async function sceneTestrun() {
    loadSession('audit-rotation', true);
    const row = addAgent();
    await tool(row, 'run_shell', 'python -m unittest tests.test_audit_rotation', {
      ms: 1400, tokens: 900, meta: '1.4s · exit 1', err: true,
      result: 'AssertionError: the ledger was truncated, not rotated\n\nFAILED (failures=1)',
    });
    await content(row, 'The first run is the finding: a reader mid-write saw a torn line. Rotate by '
      + 'rename, then start the fresh file.');
    await tool(row, 'run_shell', 'python -m unittest tests.test_audit_rotation', {
      ms: 1200, tokens: 640, meta: '1.2s · exit 0',
      result: 'Ran 4 tests in 0.31s\n\nOK',
    });
    finishTurn(3100);
  }

  async function sceneMeter() {
    loadSession('shell-timeout', true);
    setTokens(41000);
    for (const n of [58000, 74000, 92000, 108000, 116400]) {
      await wait(900);
      setTokens(n);
    }
    await wait(900);
    S.compacting = true;
    setTokens(S.tokens);
    toast('compaction: summarising the middle of the transcript, keeping the tail', true);
    await wait(2600);
    S.compacting = false;
    setTokens(24100);
    toast('compacted: 24,100 tokens in context');
  }
  sceneMeter.repeat = true;

  async function scenePlan() {
    loadSession('shell-timeout', true);
    const row = addAgent();
    planStatus('executing', 'phase 2 of 3, 2 sub-agents finished', 2);
    tasks([{ text: 'Find the code the request names', done: true },
      { text: 'Write the failing test', done: true },
      { text: 'Change it', done: false }, { text: 'Run the suite', done: false }]);
    await tool(row, 'read_file', 'backend/tools/shell.py', { ms: 700, tokens: 1180, meta: '0.7s · 62 lines' });
    tasks([{ text: 'Find the code the request names', done: true },
      { text: 'Write the failing test', done: true },
      { text: 'Change it', done: true }, { text: 'Run the suite', done: false }]);
  }

  async function sceneSettings() {
    openSettings('general');
    await wait(400);
  }

  async function sceneBudget() {
    openSettings('general');
    const el = $('#lim-turn_token_budget');
    if (el) el.closest('.mcp-add').scrollIntoView({ block: 'center' });
  }

  async function sceneSkills() {
    openSettings('tools');
    // The third section header in that tab is Skills. Count the headers rather than
    // trusting a positional selector, which counts element types and not classes.
    const el = $$('#panel-tools .ho-section')[2];
    if (el) el.scrollIntoView({ block: 'start' });
  }

  async function sceneMcp() {
    openSettings('tools');
    const el = $('#panel-tools .tool-row[data-name="mcp_call"]');
    if (el) el.scrollIntoView({ block: 'center' });
  }

  async function sceneGit() {
    openGit();
    await wait(400);
  }

  async function sceneSnapshots() {
    loadSession('shell-timeout', true);
    const row = addAgent();
    snapshot(row, 'snap-02', 3);
    await content(row, 'Two files changed and one added. Restore puts the workspace back and '
      + 'leaves the transcript alone, which is the only reason an agent can be trusted to edit.');
    openSnapshots();
  }

  async function sceneEmpty() {
    // Nothing to build: the shell already boots on its empty state, with the four asks
    // under it. This is what a first run of Tacit looks like before a workspace is picked.
    $('#emptyTag').textContent = 'Pick a workspace and say what you need.';
  }

  const SCENES = {
    basic: sceneBasic,
    transcript: sceneTranscript,
    tools: sceneTools,
    testrun: sceneTestrun,
    meter: sceneMeter,
    plan: scenePlan,
    settings: sceneSettings,
    budget: sceneBudget,
    skills: sceneSkills,
    mcp: sceneMcp,
    git: sceneGit,
    snapshots: sceneSnapshots,
    empty: sceneEmpty,
  };

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
  renderGeneral();
  renderProviders();
  renderTools();
  selectTab('appearance');
  settings.hidden = true;
  asPanel.classList.remove('open');
  dgPanel.classList.remove('open');
  $('#steerHint').hidden = true;
  asRender();

  // A reader who types to the assistant gets what the pointer gets: an answer that ends in
  // a fenced prompt, and a "Send to agent" button that starts a turn with it.
  function asSend() {
    const box = $('#asInput');
    const text = box.value.trim();
    if (!text) return;
    box.value = '';
    asAskText(text);
    const c = CONSULT[S.session] || CONSULT[LOOP[0]];
    setTimeout(() => { asAnswer(c.lead, c.prompt, c.tools); }, 620);
  }
  $('#asSend').addEventListener('click', asSend);
  $('#asInput').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey) return;
    e.preventDefault();
    asSend();
  });

  // A system that asks for less motion gets a still frame instead of a turn that
  // writes itself: the strip starts paused, and Replay or Resume starts it on request.
  if (REDUCED) {
    S.paused = true;
    const p = $('#demoPause');
    p.textContent = 'Resume';
    p.classList.add('on');
  }

  if (FRAGMENT) {
    // One scene, then quiet. A scene that repeats is one whose point is the motion
    // (the meter climbing and compacting), not a result.
    const scene = SCENES[VIEW] || sceneBasic;
    const run = () => {
      if (S.busy) return;
      try {
        Promise.resolve(scene()).catch((err) => {
          toast('mock stopped: ' + (err && err.message ? err.message : err), true);
        });
      } catch (err) {
        toast('mock stopped: ' + (err && err.message ? err.message : err), true);
      }
    };
    run();
    if (scene.repeat) setInterval(run, 14000);
  } else {
    // Everything after this is the loop: it moves the pointer, asks, and runs the turn.
    loadSession(LOOP[0], true);
    setTimeout(idleLoop, 1600);
  }
})();
