/* Tacit /mobile — the phone chrome.

   Deliberately thin, and deliberately not a second implementation of anything:
   the socket URL, the id generator, the reconnect loop and the event/command names come
   from transport.js, and markdown comes from markdown.js. What lives here is drawing:
   one column, a drawer, a composer. Nothing in this file builds a `/ws/` string or
   invents a `type:` literal - tests/test_mobile_protocol.py fails the page if it tries.

   Cut on purpose, per PLAN item 8: the side panels (memory, plan, git, files), the
   multi-pane layout, and every hover affordance. Hover does not exist here, which is
   also why timestamps are drawn permanently rather than on a pointer (item 5).
*/
(() => {
  'use strict';

  const T = window.TacitTransport;
  const MD = window.TacitMarkdown;
  const $ = (s) => document.querySelector(s);

  const thread = $('#mThread'), drawer = $('#mDrawer'), scrim = $('#mScrim');
  const listEl = $('#mSessions'), input = $('#mInput'), statusEl = $('#mStatus');

  let info = null;
  let sessions = [];
  let activeId = null;
  let sock = null;
  let busy = false;
  let booted = false;

  // ---- prefs: the same keys the desktop page writes, so one browser has one set ----
  const pref = (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } };
  const save = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };

  const cur = () => sessions.find((s) => s.id === activeId) || null;

  function timeOf(ts) {
    // Epoch seconds on the wire (store._now), rendered as a local clock. Never stored
    // localised: this machine's timezone must not end up in the record or the export.
    if (!ts) return '';
    const d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  // ---- the undo point a turn left behind (PLAN item 4) ----
  // The same link the desktop page uses: the backend writes the row's index beside
  // the snapshot, and a snapshot with no marker gets no control here either. Mobile
  // shows the file list rather than a compare, because compare and restore need a
  // workspace picked, and this page deliberately has none -- a subset, on purpose.
  let snaps = [];
  let snapSeq = 0;

  async function loadSnapshots(sid) {
    snapSeq++;
    const mine = snapSeq;
    snaps = [];
    if (!sid) { renderThread(); return; }
    try {
      const d = await T.getJSON('/api/snapshots?session=' + encodeURIComponent(sid));
      if (mine !== snapSeq) return;              // a newer session already won
      snaps = (d && d.snapshots) || [];
      renderThread();
    } catch (e) { /* no control beats one that opens nothing */ }
  }

  const snapshotsFor = (i) => snaps.filter((s) => s.message === i);

  // ---- drawer: open, and the three ways out (tap the scrim, Escape, pick a session) ----
  function openDrawer() { drawer.classList.add('open'); scrim.classList.add('on'); }
  function closeDrawer() { drawer.classList.remove('open'); scrim.classList.remove('on'); }
  $('#mMenu').addEventListener('click', () => {
    drawer.classList.contains('open') ? closeDrawer() : openDrawer();
  });
  scrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  // ---- rendering ----
  function fold(label, body, isErr) {
    const w = document.createElement('div');
    w.className = 'm-fold';
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    const inner = document.createElement('div');
    inner.className = 'm-fold-body' + (isErr ? ' err' : '');
    inner.textContent = body || '';
    b.addEventListener('click', () => w.classList.toggle('open'));
    w.appendChild(b); w.appendChild(inner);
    return w;
  }

  function row(msg, i) {
    const el = document.createElement('div');
    el.className = 'm-msg ' + (msg.role === 'user' ? 'you' : 'asst');

    const meta = document.createElement('div');
    meta.className = 'm-meta';
    const role = document.createElement('span');
    role.className = 'm-role';
    role.textContent = msg.role === 'user' ? 'you' : 'tacit';
    meta.appendChild(role);

    const t = timeOf(msg.ts);
    if (t) {                                  // no ts, no time: never a fabricated one
      const when = document.createElement('span');
      when.className = 'm-time';
      when.textContent = t;
      meta.appendChild(when);
    }
    el.appendChild(meta);

    if (msg.reason) el.appendChild(fold('thinking', msg.reason, false));

    for (const tool of (msg.tools || [])) {
      const label = tool.name + (tool.is_error ? ' - failed' : '');
      el.appendChild(fold(label, String(tool.result || ''), !!tool.is_error));
    }

    const body = document.createElement('div');
    body.className = 'm-bubble';
    if (msg.role === 'user') {
      body.textContent = msg.content || '';   // user text never enters innerHTML
    } else {
      body.innerHTML = MD.md(msg.content || '');
    }
    el.appendChild(body);
    // The undo points this row caused, tap to expand. A snapshot with no marker
    // has no control, and a session with no snapshots draws nothing at all.
    for (const s of snapshotsFor(i)) {
      el.appendChild(fold(`snapshot · ${s.file_count} file(s)`,
                          (s.files || []).join('\n') || '(no files listed)', false));
    }
    return el;
  }

  function renderThread() {
    const s = cur();
    // One read per session, triggered by a session change: this covers boot, the
    // drawer and New chat without each of them having to remember to ask, and
    // renderThread runs many times a turn, hence the flag rather than a fetch.
    if (s && !s._snapLoaded) { s._snapLoaded = true; loadSnapshots(s.id); }
    thread.textContent = '';
    if (!s || !s.messages || !s.messages.length) {
      const empty = document.createElement('div');
      empty.className = 'm-meta';
      empty.textContent = s ? 'Nothing said yet.' : 'Pick or start a session.';
      thread.appendChild(empty);
      return;
    }
    // The index is the coordinate the snapshot marker uses, so an undo point lands
    // on the row that caused it. mobile draws one row per stored message, so the
    // loop position is that index directly -- no grouping to reconcile against.
    s.messages.forEach((m, i) => thread.appendChild(row(m, i)));
    thread.scrollTop = thread.scrollHeight;
  }

  function renderSessions() {
    listEl.textContent = '';
    for (const s of sessions) {
      const li = document.createElement('li');
      if (s.id === activeId) li.className = 'active';
      if (s._busy) li.classList.add('busy');
      const dot = document.createElement('span'); dot.className = 'dot';
      const t = document.createElement('span'); t.className = 't';
      t.textContent = s.title || 'untitled';
      li.appendChild(dot); li.appendChild(t);
      li.addEventListener('click', () => { switchTo(s.id); closeDrawer(); });
      listEl.appendChild(li);
    }
    const s = cur();
    $('#mTitle').textContent = (s && s.title) || 'Tacit';
    if (s) {
      document.querySelectorAll('#mMode button').forEach((b) =>
        b.classList.toggle('active', b.dataset.mode === s.mode));
    }
  }

  function setBusy(v) {
    busy = v;
    $('#mSend').hidden = v;
    $('#mStop').hidden = !v;
    $('#mSend').disabled = v;
  }

  // ---- transport ----
  function attach(s) {
    if (sock) { sock.close(); sock = null; }
    if (!s) return;
    sock = T.openSocket(s, {
      onopen: () => { statusEl.textContent = s.model || ''; },
      onmessage: (m) => { if (m.sid && m.sid !== s.id) return; handle(m); },
      onclose: () => { statusEl.textContent = 'disconnected. Retrying'; },
      onunknown: () => {
        // 4404: the server has never heard of this id. One re-attach with create=1
        // (a session this page genuinely just made), and then it stops - a second
        // refusal means the id is stale, and reconnecting forever would re-grow rows.
        if (!s._adopted && (s._new || s._adopt)) {
          s._adopted = true; s._adopt = true; s._new = false;
          statusEl.textContent = 'could not be restored, reopening';
          attach(s);
        } else {
          statusEl.textContent = 'could not be restored';
        }
      }
    });
  }

  function lastAssistant() {
    const s = cur();
    if (!s) return null;
    let m = s.messages[s.messages.length - 1];
    if (!m || m.role !== 'assistant') {
      m = { role: 'assistant', content: '', reason: '', tools: [], ts: Date.now() / 1000 };
      s.messages.push(m);
    }
    return m;
  }

  function handle(m) {
    switch (m.type) {
      case 'text': {
        const a = lastAssistant();
        a.content += m.delta || '';
        renderThread();
        break;
      }
      case 'reason': {
        const a = lastAssistant();
        a.reason = (a.reason || '') + (m.delta || '');
        break;                                    // folded; redrawn when the turn ends
      }
      case 'tool_start': {
        const a = lastAssistant();
        (a.tools || (a.tools = [])).push({ id: m.id, name: m.name, args: m.args });
        renderThread();
        break;
      }
      case 'tool_end': {
        const a = cur() && lastAssistant();
        const t = ((a && a.tools) || []).find((x) => x.id === m.id);
        if (t) { t.result = m.result; t.is_error = m.is_error; }
        renderThread();
        break;
      }
      case 'tool_progress': {
        const a = cur() && lastAssistant();
        const t = ((a && a.tools) || []).find((x) => x.id === m.id);
        if (t) t.result = (t.result || '') + (m.text || '');
        break;
      }
      case 'user_message': {
        const s = cur();
        if (s) s.messages.push({ role: 'user', content: m.text || '', ts: m.ts });
        renderThread();
        break;
      }
      case 'state_delta': {
        const s = cur();
        if (s && m.busy != null) s._busy = !!m.busy;
        if (s && m.name) s.title = m.name;
        if (m.busy != null) setBusy(!!m.busy);
        renderSessions();
        break;
      }
      case 'usage': {
        const c = m.context || {};
        statusEl.textContent = c.percent != null ? c.percent + '% of window' : '';
        break;
      }
      case 'done':
      case 'session_ready': {
        setBusy(false);
        renderThread();
        break;
      }
      case 'error': {
        statusEl.textContent = m.message || 'error';
        setBusy(false);
        break;
      }
      case 'notify': {
        statusEl.textContent = m.text || m.message || '';
        break;
      }
      case 'replay': {
        for (const ev of (m.events || [])) handle(ev);
        break;
      }
      default:
        break;                                    // hello/rpc_response/... nothing to draw
    }
  }

  // ---- session list, straight from the server's own registry ----
  async function load() {
    const reg = await T.getJSON('/api/sessions/registry');
    if (reg && Array.isArray(reg.sessions)) {
      // The server's row names the workspace `project`; the client shape is `workdir`,
      // and wsUrl() sends that. Normalising is not cosmetic: connect without it and
      // every socket sends an empty workdir, which the server reads as "clear it".
      sessions = reg.sessions.map((s) => ({ ...s, workdir: s.workdir || s.project || '' }))
                             .sort((a, b) => (b.created || 0) - (a.created || 0));
      if (reg.active && sessions.find((s) => s.id === reg.active)) activeId = reg.active;
    }
    if (!activeId && sessions.length) activeId = sessions[0].id;
  }

  function switchTo(id) {
    if (id === activeId) { renderThread(); return; }   // never creates anything
    activeId = id;
    save('tacit.active', id);
    renderSessions();
    renderThread();
    attach(cur());
  }

  function newSession() {
    const s = {
      id: T.uuid(),
      title: 'New session',
      model: pref('tacit.model', (info && info.default) || ''),
      mode: pref('tacit.mode', 'chat'),
      thinking: pref('tacit.thinking', 'default'),
      workdir: '',
      created: Date.now(),
      messages: [],
      _new: true                                   // the only thing that may send create=1
    };
    sessions.unshift(s);
    activeId = s.id;
    save('tacit.active', s.id);
    renderSessions(); renderThread(); attach(s); closeDrawer();
  }

  // ---- mode: changes the session in view, in place. Never a new session (item 2). ----
  async function setMode(mode) {
    const s = cur();
    if (!s || s.mode === mode) return;
    s.mode = mode;
    save('tacit.mode', mode);
    renderSessions();
    await T.postJSON(`/api/sessions/${s.id}/metadata`, { mode });
    // No socket re-create: the turn reads the mode off the record, so the next prompt
    // on this same socket runs the new mode.
  }

  async function setModel(model) {
    const s = cur();
    if (!s) return;
    s.model = model;
    save('tacit.model', model);
    await T.postJSON(`/api/sessions/${s.id}/metadata`, { model });
  }

  function send() {
    const text = (input.value || '').trim();
    if (!text) return;
    const s = cur();
    if (!s) { newSession(); }
    if (!sock || !sock.open()) attach(cur());
    if (sock && sock.send({ type: 'prompt', message: text })) {
      const st = cur();
      if (st) st.messages.push({ role: 'user', content: text, ts: Date.now() / 1000 });
      input.value = '';
      input.style.height = 'auto';
      setBusy(true);
      renderThread();
    } else {
      statusEl.textContent = 'not connected';
    }
  }

  // ---- wiring ----
  $('#mComposer').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  $('#mStop').addEventListener('click', () => { if (sock) sock.send({ type: 'abort' }); });
  $('#mNew').addEventListener('click', newSession);
  $('#mMode').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mode]');
    if (b) setMode(b.dataset.mode);
  });
  $('#mModel').addEventListener('change', (e) => setModel(e.target.value));
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + 'px';
  });
  input.addEventListener('keydown', (e) => {
    // Enter sends on a phone keyboard only with no Shift: Shift+Enter is the newline
    // people actually have, since there is no second key to reach for.
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  function fillModels() {
    const sel = $('#mModel');
    sel.textContent = '';
    const rows = (info && info.models) || [];
    const names = rows.map((r) => (typeof r === 'string' ? r : (r.ref || r.id || ''))).filter(Boolean);
    for (const n of names) {
      const o = document.createElement('option');
      o.value = n; o.textContent = n;
      sel.appendChild(o);
    }
    const s = cur();
    sel.value = (s && s.model) || (info && info.default) || '';
  }

  (async function boot() {
    if (!T || !MD) { statusEl.textContent = 'transport or markdown failed to load'; return; }
    info = await T.getJSON('/api/info');
    await load();
    fillModels();
    renderSessions();
    renderThread();
    if (activeId) attach(cur());
    booted = true;
    statusEl.textContent = (info && info.version) ? 'v' + info.version : '';
  })();
})();
