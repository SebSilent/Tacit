/* Tacit Web — the chat client, spoken to over a WebSocket */
(() => {
'use strict';

// crypto.randomUUID is undefined on plain-HTTP LAN origins (insecure context)
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
  : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    }));

const $ = s => document.querySelector(s);
const messagesEl = $('#messages');
const inputEl = $('#input');
const sendBtn = $('#sendBtn');
const stopBtn = $('#stopBtn');
const sessionListEl = $('#sessionList');
const providerSelect = $('#providerSelect');
const modelSelect = $('#modelSelect');
const thinkSelect = $('#thinkSelect');
const emptyState = $('#emptyState');
const jumpBtn = $('#jumpBtn');

// ── state ────────────────────────────────────────────────────────────
let info = { models: [], default: null, llm_online: false, llm_url: '' };
// safe load: one corrupt/quota-truncated write must never nuke the whole app
let sessions = [];
try {
  const parsed = JSON.parse(localStorage.getItem('tacit.sessions') || '[]');
  if (Array.isArray(parsed)) sessions = parsed.filter(s => s && s.id);
} catch (e) {
  try { localStorage.removeItem('tacit.sessions'); } catch (e2) {}
}
// sessions: [{id, title, model, mode, created, messages:[{role, content, tools?, reason?}]}]
let activeId = localStorage.getItem('tacit.active') || null;
let ws = null;
let streaming = null;   // {msg, el} current assistant message being streamed
let busy = false;
let reconnectTimer = null;
let wsDead = false;
let sessionStarting = false;
let pendingSend = null;
let planModeOn = false;          // next send runs plan orchestration
let planLive = false;            // an orchestration is currently running
let attachments = [];            // {data(base64), mimeType, name}
let autoCompactionOn = true;
let stickToBottom = true;
let llmWarned = false;
let serverSyncTimer = null;
let serverRegistryLoaded = false;

// ── themes (apply before first paint) ────────────────────────────────
const THEMES = [
  { id: 'midnight', name: 'Midnight', hint: 'deep space violet' },
  { id: 'daybreak', name: 'Daybreak', hint: 'clean light' },
  { id: 'vapor',    name: 'Vapor',    hint: 'neon cyber' },
  { id: 'ocean',    name: 'Ocean',    hint: 'abyssal blue' },
  { id: 'ember',    name: 'Ember',    hint: 'warm forge' },
];
let theme = localStorage.getItem('tacit.theme') || 'midnight';
if (!THEMES.some(t => t.id === theme)) theme = 'midnight';
document.documentElement.dataset.theme = theme;

function applyTheme(id) {
  if (!THEMES.some(t => t.id === id)) return;
  theme = id;
  document.documentElement.dataset.theme = id;
  localStorage.setItem('tacit.theme', id);
}
// consumed by the harness "Appearance" panel
window.TacitTheme = { themes: THEMES, current: () => theme, apply: applyTheme };

// NOTE: the "ensure at least one session" bootstrap deliberately does NOT run
// here. newSession() -> renderTokMeter() touches `const tokMeter`, declared
// further down (~line 1164), so calling it at this point threw a TDZ
// ReferenceError that aborted the ENTIRE module: no event listeners, no
// boot(), no WebSocket — the UI sat on the static "connecting..." forever.
// It only ever fired on a browser with no stored sessions.
// It now runs just before boot() below, once every const is initialised.

function cur() { return sessions.find(s => s.id === activeId); }
function persist(opts) {
  const localOnly = opts && opts.localOnly;
  try {
    localStorage.setItem('tacit.sessions', JSON.stringify(sessions.map(s => ({
      ...s, messages: s.messages.slice(-120)
    }))));
    localStorage.setItem('tacit.active', activeId);
  } catch (e) {
    // quota exceeded — the server registry sync below still carries the data
  }
  if (!localOnly) scheduleServerSync();
}

function scheduleServerSync() {
  if (serverSyncTimer) clearTimeout(serverSyncTimer);
  serverSyncTimer = setTimeout(() => {
    syncServerRegistry().catch(() => {});
  }, 800);
}

async function syncServerRegistry() {
  try {
    await fetch('/api/sessions/registry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        // bounded payload: the server holds the full transcript anyway
        sessions: sessions.map(s => ({ ...s, messages: (s.messages || []).slice(-200) })),
        active: activeId, version: 1
      })
    });
  } catch (e) {
    // offline: will retry on next persist
  }
}

async function loadServerRegistry() {
  try {
    const r = await fetch('/api/sessions/registry');
    if (!r.ok) return;
    const reg = await r.json();
    // Server wins for cross-device consistency; keep local messages only
    // for sessions the server does not know about yet.
    const byId = new Map((reg.sessions || []).map(s => [s.id, s]));
    let changed = false;
    for (const ls of sessions) {
      const ss = byId.get(ls.id);
      if (!ss) {
        if (ls.messages && ls.messages.length) { byId.set(ls.id, ls); changed = true; }
      } else if ((ls.messages || []).length > (ss.messages || []).length) {
        // local holds a longer transcript (offline edits, or the server was
        // restarted before the first persist) — keep the richer copy
        byId.set(ls.id, ls);
        changed = true;
      }
    }
    if (byId.size) {
      // Server rows carry the workspace as `project`; the client shape is
      // `workdir`. Normalising here is what makes the chip and the picker read
      // the session's own value instead of falling back to a global default.
      sessions = Array.from(byId.values())
        .map(s => ({ ...s, workdir: s.workdir || s.project || '' }))
        .sort((a, b) => (b.created || 0) - (a.created || 0));
      if (reg.active && sessions.find(s => s.id === reg.active)) activeId = reg.active;
      // when this browser held richer copies, push them so the server can
      // adopt the history (a plain page load rescues browser-only sessions)
      persist(changed ? {} : { localOnly: true });
    }
  } catch (e) {
    // fall back to localStorage
  } finally {
    serverRegistryLoaded = true;
  }
}

async function syncSessionMetadata(s) {
  try {
    await fetch(`/api/sessions/${s.id}/metadata`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: s.model, mode: s.mode, thinking: s.thinking,
        name: s.title, allowed_tools: s.allowed_tools,
        excluded_tools: s.excluded_tools, permission_mode: s.permission_mode
      })
    });
  } catch (e) {}
}

async function initSessions() {
  await loadServerRegistry();
  if (!activeId || !sessions.find(s => s.id === activeId)) {
    activeId = sessions.length ? sessions[0].id : null;
  }
  renderSessionList();
  renderTranscript(cur());
  renderModelChip();
  renderTokMeter();
  // The assistant panel learns which session is active only here. Without
  // this call its sid stays empty at boot and the panel shows nothing until
  // the user switches away and back — which is the only other place
  // attachCurrent runs.
  attachCurrent();
}

function newSession(silent) {
  const s = {
    id: uuid(), title: 'New session',
    model: localStorage.getItem('tacit.model') || info.default || '',
    mode: localStorage.getItem('tacit.mode') || 'chat',
    thinking: localStorage.getItem('tacit.thinking') || info.default_thinking || 'default',
    // No project ⇒ no working directory at all. Nothing directory-related is
    // sent to the server or injected into the prompt; the model picks its own
    // paths. The workspace belongs to the session: a new one starts with none
    // and inherits nothing from the last session or from localStorage.
    workdir: '',
    created: Date.now(), messages: [], _new: true
  };
  sessions.unshift(s);
  activeId = s.id;
  persist();
  renderTokMeter();
  renderSessionList();
  if (!silent) attachCurrent();
  return s;
}

// ── session auto-namer (rule-based, zero AI tokens) ──────────────────
// Derives a sidebar title from the first user message with pure string
// heuristics: strip filler/politeness prefixes, handle question openers,
// detect an imperative intent verb ("Fix: …", "Refactor: …"), trim to a
// short readable phrase. Never calls the LLM. A manual rename
// (s.namedByUser) always wins and is never overwritten by this.
const TITLE_VERBS = ['fix','add','create','build','write','implement','refactor','debug','explain','review','update','upgrade','remove','delete','optimize','optimise','improve','test','document','translate','summarize','summarise','analyze','analyse','design','install','configure','deploy','migrate','investigate','plan','convert','rename','check','find','make','setup','generate','clean','move','change','replace'];
const TITLE_FILLER = [
  /^(please|hey|hi|hello|yo|ok|okay|so|now|well)[,;!\s.]+/i,
  /^(can|could|would|will)\s+you\s+(please\s+)?/i,
  /^i\s+(really\s+)?(want|need|would\s+like|'d\s+like)\s+(you\s+to\s+)?/i,
  /^i'?m\s+trying\s+to\s+/i,
  /^help\s+me\s+(to\s+|out\s+)?/i,
  /^let'?s\s+/i
];
const TITLE_STOP_LEAD = /^(the|a|an|this|that|my|our|me|it)\s+/i;

function autoTitle(text) {
  let t = (text || '')
    .replace(/```[\s\S]*?```/g, ' ')        // drop fenced code blocks
    .replace(/`([^`]+)`/g, '$1')            // unwrap inline code, keep identifiers
    .replace(/https?:\/\/\S+/g, ' ')        // drop URLs
    .split('\n')[0]                          // first line only
    .replace(/\s+/g, ' ').trim();
  if (!t) return 'New session';

  // strip conversational filler repeatedly: "Hey, can you please add…" -> "add…"
  let prev;
  do {
    prev = t;
    for (const re of TITLE_FILLER) t = t.replace(re, '');
    t = t.trim();
  } while (t !== prev && t);

  // question openers: "How do I reverse a list?" -> "How to reverse a list"
  t = t.replace(/^how\s+(do|does|did|can|could|should|would)\s+(i|we|you)\s+(to\s+)?/i, 'how to ')
       .replace(/^(what|why|when|where|which)\s+(is|are|was|were)\s+/i, '')
       .replace(/\?+\s*$/, '')
       .trim();
  if (!t) return 'New session';

  // imperative intent verb at the head -> "Verb: rest"
  const m = t.match(/^([A-Za-z]+)\b\s*:?-?\s*(.*)$/);
  let verb = '', rest = t;
  if (m && TITLE_VERBS.includes(m[1].toLowerCase())) { verb = m[1].toLowerCase(); rest = m[2].trim(); }
  if (TITLE_STOP_LEAD.test(rest) && rest.split(' ').length > 2) rest = rest.replace(TITLE_STOP_LEAD, '');

  // cap length: 8 words / 48 chars, break on a word boundary
  rest = rest.split(' ').filter(Boolean).slice(0, 8).join(' ');
  if (rest.length > 48) rest = rest.slice(0, 48).replace(/\s+\S*$/, '');
  rest = rest.replace(/\s+(a|an|the|in|on|of|to|for|with|and|or|at|by|from)$/i, '');
  rest = rest.replace(/[\s.,;:!?(\/-]+$/, '').trim();

  let title = verb ? (rest ? cap(verb) + ': ' + rest : cap(verb)) : rest;
  if (!title) return 'New session';
  title = title.charAt(0).toUpperCase() + title.slice(1);
  if (title.length > 56) title = title.slice(0, 56) + '…';
  return title;
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// ── session rename (manual title always wins over auto-naming) ───────
function renameSession(id, title) {
  const s = sessions.find(x => x.id === id);
  if (!s) return;
  title = (title || '').trim().slice(0, 60);
  if (title && title !== s.title) {
    s.title = title;
    s.namedByUser = true;      // lock: auto-namer must not touch this again
    persist();
    syncSessionMetadata(s);
    // keep the agent-side session snapshot in sync (server echoes state_delta)
    if (id === activeId && ws && ws.readyState === 1)
      ws.send(JSON.stringify({ type: 'set_session_name', name: title }));
  }
  renderSessionList();
  if (s.id === activeId) $('#topTitle').textContent = s.title;
}

// swaps `host` content for an inline input; Enter/blur commits, Esc cancels
function inlineRename(host, current, commit) {
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.value = current;
  input.maxLength = 60;
  input.spellcheck = false;
  host.replaceChildren(input);
  input.focus();
  input.select();
  let done = false;
  const finish = ok => {
    if (done) return;
    done = true;
    commit(ok ? input.value : current);
  };
  input.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('click', e => e.stopPropagation());
  input.addEventListener('dblclick', e => e.stopPropagation());
}

const EMPTY_TAGS = [
  'Pick a workspace and say what you need.',
  'Read first. Then change one thing.',
  'Small steps, each one checked.',
  'Understand it before you change it.',
  'Ask a real question, get a real answer.',
  'One change at a time.',
  'Say what you want, then check it yourself.',
  'Slow is smooth. Smooth is fast.',
  'Your machine, your files, your call.',
  'Nothing here is watching you.',
  'The best bug is the one you never shipped.',
  'Good tools stay out of the way.',
  'Start where you are.',
  'Write it down before you build it.',
  'A clear question is half the work.',
  'Measure twice, edit once.',
];

function pickTagline() {
  const el = document.getElementById('emptyTag');
  if (!el) return;
  el.textContent = EMPTY_TAGS[Math.floor(Math.random() * EMPTY_TAGS.length)];
}

function startRenameTop() {
  const s = cur();
  if (!s) return;
  inlineRename($('#topTitle'), s.title, v => renameSession(s.id, v));
}

// ── websocket ────────────────────────────────────────────────────────
// ONE transport per tab: the socket stays open for the whole page life.
// Switching sessions is an in-band `switch` command (server answers with a
// `hello` carrying starting=true/false), never a reconnect.
function wsUrl(s) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  // 'localhost' resolves to ::1 first on Windows; uvicorn is IPv4-bound, so
  // every handshake would stall ~2s on the fallback. Pin the loopback v4.
  const host = location.hostname === 'localhost'
    ? '127.0.0.1' + (location.port ? ':' + location.port : '')
    : location.host;
  const think = s.thinking ? `&thinking=${s.thinking}` : '';
  // Always send workdir (even empty) so clearing a workspace reaches the server.
  // The server treats missing param as "leave alone", empty string as "clear".
  const wd = `&workdir=${encodeURIComponent(s.workdir || '')}`;
  // create=1 rides on a socket the New session button just asked for, and on
  // the single re-attach after the server said it has never heard of this
  // session (see onclose). It is not sent casually: _adopt is set once per
  // session, so this cannot re-grow the list the way the old blind
  // merge-on-connect did.
  const mk = (s._new || s._adopt) ? '&create=1' : '';
  // A restored session already has a title the user chose; a brand new one
  // gets the server default, which is fine.
  const nm = (s._adopt && s.title) ? `&name=${encodeURIComponent(s.title)}` : '';
  return `${proto}://${host}/ws/${s.id}?model=${encodeURIComponent(s.model)}&mode=${s.mode}${think}${wd}${mk}${nm}`;
}

function connect() {
  clearTimeout(reconnectTimer);
  if (ws && ws.readyState === 1) return;           // transport already live
  if (ws) { try { ws.onclose = null; ws.close(); } catch (e) {} ws = null; }
  const s = cur();
  if (!s) { setConn(''); return; }        // nothing to attach to yet
  setConn('connecting…');
  ws = new WebSocket(wsUrl(s));
  ws.onopen = () => { wsDead = false; setConn(''); if (s) delete s._new; if (s) delete s._adopt; };
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.sid && m.sid !== activeId) { bgIngest(m); return; }
    handle(m);
  };
  ws.onclose = ev => {
    wsDead = true;
    // 4404 is a different failure from a dead network: the server answered and
    // said it does not know this session. That happens when the store is
    // rebuilt or relocated underneath a browser that still holds its ids, and
    // retrying the identical request forever just prints "retrying" on the
    // status line while nothing can ever change. Ask once for the server to
    // adopt the id this tab already has; if it still refuses, stop.
    if (ev && ev.code === 4404 && s) {
      if (!s._adopted) {
        s._adopted = true;
        s._adopt = true;
        setConn('session not on server — re-creating');
        scheduleReconnect();
        return;
      }
      setConn('session could not be restored — start a new one');
      return;
    }
    // Everything else stays on the old behaviour: the server is restarting or
    // the link dropped, and the next attempt may genuinely succeed.
    setConn('disconnected — retrying');
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

// attach current session over the already-open socket
function attachCurrent() {
  if (window.TacitAssistant && window.TacitAssistant.sessionChanged) {
    window.TacitAssistant.sessionChanged(activeId);
  }
  refreshTaskStrip();
  const s = cur();
  if (!s) return;
  // A brand-new session must ride a socket that is allowed to create it, so
  // drop the current one and reconnect rather than switching over it. Switching
  // never created anything, which is why pressing New session did nothing.
  if (s._new && ws && ws.readyState === 1) {
    try { ws.onclose = null; ws.close(); } catch (e) {}
    ws = null;
  }
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ type: 'switch', sid: s.id, model: s.model,
      mode: s.mode, thinking: s.thinking || '' }));
  } else {
    connect();
  }
}

// background stream events for a non-active session: keep its transcript alive
function bgIngest(m) {
  const s = sessions.find(x => x.id === m.sid);
  if (!s) return;
  if (m.type === 'text') {
    let last = s.messages[s.messages.length - 1];
    if (!last || last.role !== 'assistant') {
      last = { role: 'assistant', content: '', reason: '', tools: [] };
      s.messages.push(last);
    }
    last.content += m.delta;
    persist();
  } else if (m.type === 'done' || m.type === 'proc_exit') {
    persist();
  }
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => { if (cur()) connect(); }, 1200);
}

function setConn(txt) { $('#connHint').textContent = txt; }
function showBootDot(on) { $('#connHint').classList.toggle('booting', on); }
function flushPending() {
  if (pendingSend && ws && ws.readyState === 1) {
    ws.send(pendingSend);
    pendingSend = null;
  }
}

// re-attach to an assistant response still streaming in this session
function resumeLiveStream() {
  const s = cur();
  if (!s) return;
  let last = s.messages[s.messages.length - 1];
  if (!last || last.role !== 'assistant') {
    last = { role: 'assistant', content: '', reason: '', tools: [] };
    s.messages.push(last);
  }
  emptyState.style.display = 'none';
  // renderTranscript already drew the partial bubble (bgIngest kept it fresh);
  // reuse it instead of appending a duplicate.
  const rendered = messagesEl.querySelectorAll('.msg.assistant');
  let body;
  if (rendered.length) {
    body = rendered[rendered.length - 1].querySelector('.msg-body-inner');
  } else {
    body = appendMsg('assistant', '');
    const c = body.querySelector('.msg-content');
    c._raw = last.content || '';
    c.innerHTML = md(last.content || '');
    if (last.reason) body.querySelector('.reason-body').textContent = last.reason;
  }
  streaming = { msg: last, el: body };
  setBusy(true);
}

function handle(m) {
  switch (m.type) {
    case 'hello':
      if (m.starting) {
        showBootDot(true);
      } else {
        flushPending();
      }
      sessionStarting = !!m.starting;
      if (m.usage) {
        const s = cur();
        if (s) s.usage = m.usage;
        renderTokMeter();
      }
      if (m.model) {
        const s = cur();
        if (s) { s.model = m.model; renderModelPickers(); renderModelChip(); }
      }
      if (m.workdir) { const s = cur(); if (s) s.workdir = m.workdir; }
      if (m.thinking) {
        const s = cur();
        if (s && s.thinking !== m.thinking) { s.thinking = m.thinking; persist(); }
        renderThinkSelect();
      }
      if (m.allowed_tools != null || m.excluded_tools != null || m.permission_mode != null) {
        const s = cur();
        if (s) {
          if (m.allowed_tools != null) s.allowed_tools = m.allowed_tools;
          if (m.excluded_tools != null) s.excluded_tools = m.excluded_tools;
          if (m.permission_mode != null) s.permission_mode = m.permission_mode;
        }
      }
      if (m.busy && !m.starting && !streaming) resumeLiveStream();
      break;
    case 'session_ready':
      sessionStarting = false;
      showBootDot(false);
      flushPending();
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'get_state' }));
      break;
    case 'respawned':
      break;
    case 'truncated': {
      // server rewound the transcript — mirror it locally and continue from
      // the edited message
      const pe = pendingEdit;
      clearTimeout(editAckTimer);
      pendingEdit = null;
      const s = cur();
      if (!pe || !s || pe.sid !== s.id || pe.userTurnIndex !== m.userTurnIndex) break;
      if (Number.isFinite(m.rev)) s.rev = m.rev;
      let seen = -1, cut = -1;
      for (let i = 0; i < s.messages.length; i++) {
        if (s.messages[i].role === 'user') { seen++; if (seen === pe.userTurnIndex) { cut = i; break; } }
      }
      if (cut >= 0) s.messages = s.messages.slice(0, cut);
      endStreamQuiet();
      renderTranscript(s);
      renderSessionList();
      persist();
      send(pe.text);   // continue the session from the edited message
      break;
    }
    case 'truncate_failed':
      clearTimeout(editAckTimer);
      pendingEdit = null;
      toast('Edit failed: ' + (m.error || 'unknown'), true);
      { const s = cur(); if (s) renderTranscript(s); }
      break;
    case 'state_delta':
      {
        const s = cur();
        if (!s) break;
        if (m.name && !s.namedByUser) { s.title = m.name; renderSessionList(); persist(); if (s.id === activeId) $('#topTitle').textContent = m.name; }
        if (m.model) { s.model = m.model; renderModelPickers(); renderModelChip(); persist(); }
        if (m.thinking) { s.thinking = m.thinking; renderThinkSelect(); persist(); }
        if (m.allowed_tools != null) s.allowed_tools = m.allowed_tools;
        if (m.excluded_tools != null) s.excluded_tools = m.excluded_tools;
        if (m.permission_mode != null) s.permission_mode = m.permission_mode;
      }
      break;
    case 'text':
      if (streaming) { streaming.msg.content += m.delta; appendText(m.delta); }
      break;
    case 'reason':
      if (!streaming) ensureStreamingBubble();
      if (streaming) {
        streaming.msg.reason = (streaming.msg.reason || '') + m.delta;
        appendReason(m.delta);
      }
      break;
    case 'tool_start':
      if (streaming) { addToolCard(m); streaming.msg.tools.push({ id: m.id, name: m.name, args: m.args }); }
      // a tasks call can add items, so the strip is scheduled on the start
      // as well as refreshed on the end
      if (m.name === 'tasks') scheduleTaskStrip();
      // A delegation launch is invisible in the transcript — the sub-agent's
      // calls never reach it — so the panel that shows the work in flight
      // opens itself here, before the first delegation_activity arrives.
      if (window.TacitDelegation && window.TacitDelegation.maybeLaunch) {
        window.TacitDelegation.maybeLaunch(m.name);
      }
      break;
    case 'tool_end':
      finishToolCard(m);
      if (streaming) {
        const t = streaming.msg.tools.find(t => t.id === m.id);
        if (t) { t.result = m.result; t.is_error = m.is_error; }
      }
      refreshTaskStrip();
      break;
    case 'done':
      endStream();
      break;
    case 'plan_status':
      renderPlanStatus(m);
      break;
    case 'plan_questions':
      renderPlanQuestions(m.questions || []);
      break;
    case 'plan_ready':
      planLive = false;
      setBusy(false);
      renderPlanReady(m.plan || '');
      break;
    case 'plan_approved':
      if (m.saved) {
        // plan persisted → offer the /implement equivalent
        planStrip.hidden = false;
        planStrip.innerHTML = `
          <div class="ps-head"><span class="ps-icon" style="animation:none;background:#3fb950"></span>
            <span class="ps-phase">Plan approved & saved</span>
            <span class="ps-detail">${esc(m.plan_file || '')}</span></div>
          <div class="pr-actions">
            <button class="ho-btn primary" id="plImplement">Implement now</button>
            <button class="ho-btn" id="plLater">Later (menu → Implement)</button>
          </div>`;
        $('#plImplement').addEventListener('click', () => {
          if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_implement' }));
          hidePlanStrip();
          ensureStreamingBubble();
        });
        $('#plLater').addEventListener('click', hidePlanStrip);
      } else {
        hidePlanStrip();
        toast(m.message || 'Plan not saved', true);
      }
      break;
    case 'agent_settled':
      // steer / follow_up queued in the agent
      if (m.message || m.promptId) toast('Steer queued');
      break;
    case 'usage':
      {
        // per-session token/context meter update (sid-scoped broadcast)
        const target = sessions.find(x => x.id === m.sid) || cur();
        if (target) {
          target.usage = { tokens: m.tokens || {}, context: m.context || {},
                           compacting: !!m.compacting, speed: m.speed || 0 };
          if (target.id === activeId) renderTokMeter();
        }
      }
      break;
    case 'queue_update':
      // no such message is emitted today, but if one ever carries stats fields
      // it renders through the same path as get_session_stats
      if (m.totalMessages != null || m.toolCalls != null || (m.tokens || {}).total != null) {
        applySessionStats(m);
      }
      break;
    case 'rpc_response':
      handleRpcResponse(m);
      break;
    case 'notify':
      // A cost warning and a compaction notice are not the same kind of thing as
      // ordinary chatter. The governor's warnings are the ones worth noticing
      // while a turn is still running, so they get the emphatic toast.
      toast(m.message, m.level === 'warn');
      break;
    case 'log':
      // raw agent stderr — surface only when clearly an error
      if (/error|failed|refused|not found/i.test(m.line) && !streaming) toast(m.line, true);
      break;
    case 'proc_exit':
      if (busy) endStream();
      break;
    case 'fatal':
      endStream();
      toast('Fatal: ' + m.error, true);
      break;
    case 'delegation_activity':
      // Sub-agent and research activity: the panel's job, never the transcript's.
      if (window.TacitDelegation && window.TacitDelegation.handle) {
        window.TacitDelegation.handle(m);
      }
      break;
    default:
      // The side assistant owns its own events; it must never touch the main
      // conversation's streaming state.
      if (m && typeof m.type === 'string' && m.type.indexOf('assistant') === 0
          && window.TacitAssistant && window.TacitAssistant.handle) {
        window.TacitAssistant.handle(m);
      }
      break;
  }
}

// ── sending ──────────────────────────────────────────────────────────
function send(text) {
  text = (text || '').trim();
  if (!text && !attachments.length) return;
  if (busy && !planLive) {
    // a turn is running → steer it at its next step instead of dropping the message.
    if (ws && ws.readyState === 1) {
      const imgs = drainAttachments();
      ws.send(JSON.stringify({ type: 'steer', message: text, images: imgs }));
      const s = cur();
      if (s) { s.messages.push({ role: 'user', content: text + (imgs.length ? ` · ${imgs.length} image(s)` : '') }); persist(); renderSessionList(); }
      appendMsg('user', text);
      toast('Steering the running turn');
      return;
    }
  }
  if (busy && planLive) { toast('Plan mode is running — wait for it or press Stop'); return; }
  const s = cur();
  if (!s) { toast('Press New session to start one'); return; }
  if (wsDead || !ws || ws.readyState !== 1) { connect(); toast('Reconnecting — your message will send once the link is up'); }
  if (sessionStarting) toast('Agent is booting — message queued, it will fire the moment it is ready');

  emptyState.style.display = 'none';
  const imgs = drainAttachments();
  s.messages.push({ role: 'user', content: text + (imgs.length ? ` · ${imgs.length} image(s)` : '') });
  s.messages.push({ role: 'assistant', content: '', reason: '', tools: [] });
  if (s.title === 'New session' && !s.namedByUser) s.title = autoTitle(text);
  renderSessionList();
  renderModelChip();
  persist();

  appendMsg('user', text);
  const el = appendMsg('assistant', '');
  streaming = { msg: s.messages[s.messages.length - 1], el };
  setBusy(true);

  let payload;
  if (planModeOn) {
    planLive = true;
    payload = JSON.stringify({ type: 'prompt', message: text, plan: true });
    renderPlanStatus({ phase: 'decomposing', detail: 'starting plan mode…', explorers: [] });
    setPlanMode(false);
  } else {
    payload = JSON.stringify({ type: 'prompt', message: text, images: imgs.length ? imgs : undefined });
  }
  if (ws && ws.readyState === 1) ws.send(payload);
  else { pendingSend = payload; toast('Waiting for connection…'); }
}

// base64 image payloads, shaped {type:'image', data, mimeType}
function drainAttachments() {
  const out = attachments.map(a => ({ type: 'image', data: a.data, mimeType: a.mimeType }));
  attachments = [];
  renderAttachRow();
  return out;
}

function setBusy(b) {
  busy = b;
  // the send button stays available while the agent runs — sending then
  // steers/interrupts the running turn instead of starting a new one.
  stopBtn.hidden = !b;
  sendBtn.classList.toggle('steer', b);
  sendBtn.title = b ? 'Steer the running turn (Enter)' : 'Send (Enter)';
  document.body.classList.toggle('busy', b);
  refreshPlaceholder();
  updateSteerHint();
}

function refreshPlaceholder() {
  inputEl.placeholder = planModeOn
    ? 'Describe what to plan (research → questions → plan)…'
    : busy ? 'Steer the running turn… (■ stops it)'
           : 'Message Tacit…';
}

function updateSteerHint() {
  const h = $('#steerHint');
  if (h) h.hidden = !(busy && !planLive);
}

// make sure an assistant bubble exists even if reason/text arrives before send()
function ensureStreamingBubble() {
  if (streaming) return;
  const s = cur();
  if (!s) return;
  let last = s.messages[s.messages.length - 1];
  if (!last || last.role !== 'assistant') {
    last = { role: 'assistant', content: '', reason: '', tools: [] };
    s.messages.push(last);
  }
  emptyState.style.display = 'none';
  const el = appendMsg('assistant', '');
  streaming = { msg: last, el };
  setBusy(true);
}

function endStream() {
  if (streaming) {
    // flush any unthrottled tail content (rAF may still be pending)
    const cc = streaming.el.querySelector('.msg-content');
    if (cc && cc._dirty) { cc._dirty = false; cc.innerHTML = md(cc._raw || ''); }
    const el = streaming.el.closest('.msg');
    if (el) el.classList.remove('streaming');
    const blk = streaming.el.querySelector('.reason-block');
    if (blk) {
      if (streaming.msg.reason) {
        blk.querySelector('.reason-toggle').textContent = 'thought process';
      } else {
        blk.remove();   // nothing was ever thought — drop the empty pill
      }
    }
    if (!streaming.msg.content && !streaming.msg.tools.length) {
      streaming.msg.content = '(empty response)';
      const c = streaming.el.querySelector('.msg-content');
      if (c) c.innerHTML = md('(empty response)');
    }
  }
  streaming = null;
  setBusy(false);
  persist();
}

function stop() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'abort' }));
  toast('Abort requested');
}

// ── rendering ────────────────────────────────────────────────────────
function appendMsg(role, text) {
  if (role === 'user') stickToBottom = true;
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  wrap.innerHTML = `
    <div class="avatar">${role === 'user' ? 'YOU' : 'TC'}</div>
    <div class="msg-body">
      <div class="msg-role">${role === 'user' ? 'You' : 'Tacit'}</div>
      <div class="msg-body-inner"></div>
    </div>`;
  const body = wrap.querySelector('.msg-body-inner');
  if (role === 'assistant') {
    body.innerHTML = `<div class="reason-block"><button class="reason-toggle">thinking…</button><div class="reason-body"></div></div><div class="tools"></div><div class="msg-content"></div>`;
    body.querySelector('.reason-toggle').addEventListener('click', e =>
      e.target.closest('.reason-block').classList.toggle('open'));
  } else {
    body.innerHTML = `<div class="msg-content"></div>`;
    body.querySelector('.msg-content').textContent = text;
    const edit = document.createElement('button');
    edit.className = 'msg-edit';
    edit.title = 'Edit — rewinds the session to this message and continues from it';
    edit.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17.05V20z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
    edit.addEventListener('click', ev => { ev.stopPropagation(); editUserMessage(wrap); });
    body.parentElement.appendChild(edit);
  }
  const inner = ensureInner();
  inner.appendChild(wrap);
  scrollBottom();
  return body;
}

function ensureInner() {
  let inner = messagesEl.querySelector('.messages-inner');
  if (!inner) {
    inner = document.createElement('div');
    inner.className = 'messages-inner';
    messagesEl.appendChild(inner);
  }
  return inner;
}

// Deltas can arrive many times per frame; re-render the markdown at most
// once per animation frame and batch the scroll with it.
let streamRaf = 0;
function scheduleStreamPaint() {
  if (streamRaf) return;
  streamRaf = requestAnimationFrame(() => {
    streamRaf = 0;
    if (streaming) {
      const c = streaming.el.querySelector('.msg-content');
      if (c && c._dirty) { c._dirty = false; c.innerHTML = md(c._raw || ''); }
    }
    if (stickToBottom) { messagesEl.scrollTop = messagesEl.scrollHeight; if (jumpBtn) jumpBtn.hidden = true; }
    else if (jumpBtn) jumpBtn.hidden = false;
  });
}

function appendText(delta) {
  const c = streaming.el.querySelector('.msg-content');
  c._raw = (c._raw || '') + delta;
  c._dirty = true;
  scheduleStreamPaint();
}

function appendReason(delta) {
  const r = streaming.el.querySelector('.reason-body');
  r.textContent += delta;
  const blk = r.closest('.reason-block');
  blk.querySelector('.reason-toggle').textContent = 'thinking…';
  scheduleStreamPaint();
}

function addToolCard(m) {
  const tools = streaming.el.querySelector('.tools');
  const card = document.createElement('div');
  card.className = 'tool-card';
  card.dataset.id = m.id || '';
  const argStr = m.name === 'bash' ? (m.args.command || '') :
    (m.args.path || m.args.file_path || m.args.pattern || JSON.stringify(m.args)).slice(0, 120);
  card.innerHTML = `
    <div class="tool-head">
      <span class="tool-badge"></span>
      <span class="tool-args"></span>
      <span class="tool-spin"></span>
    </div>
    <div class="tool-body"><pre></pre></div>`;
  card.querySelector('.tool-badge').textContent = m.name || 'tool';
  card.querySelector('.tool-args').textContent = argStr;
  card.querySelector('.tool-head').addEventListener('click', () => card.classList.toggle('open'));
  tools.appendChild(card);
  scrollBottom();
}

function finishToolCard(m) {
  const card = messagesEl.querySelector(`.tool-card[data-id="${CSS.escape(m.id || '')}"]`);
  if (!card) return;
  card.classList.toggle('err', !!m.is_error);
  const spin = card.querySelector('.tool-spin');
  if (spin) spin.remove();
  card.querySelector('.tool-body pre').textContent = m.result || (m.is_error ? 'error' : 'done');
}

function renderTranscript(s) {
  const inner = ensureInner();
  inner.innerHTML = '';
  if (!s) { emptyState.style.display = ''; return; }
  emptyState.style.display = s.messages.length ? 'none' : '';
  // The transcript stores one assistant row per step — every model round that ran
  // tools gets its own row, often with no text at all. The live view streams a
  // whole turn into ONE bubble (all events land in `streaming`), so drawing a
  // bubble per stored row made a fork or reload of a tool-heavy turn read as the
  // model having sent a message for every step. Group consecutive assistant rows
  // into the turn they belong to; a user or summary row starts a new group.
  const groups = [];
  for (const msg of s.messages) {
    const last = groups[groups.length - 1];
    if (msg.role === 'assistant' && last && last.role === 'assistant') last.parts.push(msg);
    else groups.push({ role: msg.role, parts: [msg] });
  }
  for (const g of groups) {
    const head = g.parts[0];
    const text = g.parts.map(p => p.content || '').join('\n\n');
    const body = appendMsg(g.role, g.role === 'assistant' ? '' : (head.content || ''));
    if (g.role !== 'assistant') continue;
    if (text) {
      const c = body.querySelector('.msg-content');
      c._raw = text;
      c.innerHTML = md(text);
    }
    const reason = g.parts.map(p => p.reason || '').filter(Boolean).join('\n\n');
    const blk = body.querySelector('.reason-block');
    if (reason) {
      blk.querySelector('.reason-body').textContent = reason;
      blk.querySelector('.reason-toggle').textContent = 'thought process';
    } else {
      blk.remove();
    }
    for (const msg of g.parts) {
      for (const t of (msg.tools || [])) {
        const tools = body.querySelector('.tools');
        const card = document.createElement('div');
        card.className = 'tool-card' + (t.is_error ? ' err' : '');
        const argStr = t.name === 'bash' ? ((t.args || {}).command || '') :
          JSON.stringify(t.args || {}).slice(0, 120);
        card.innerHTML = `
          <div class="tool-head"><span class="tool-badge"></span><span class="tool-args"></span></div>
          <div class="tool-body"><pre></pre></div>`;
        card.querySelector('.tool-badge').textContent = t.name;
        card.querySelector('.tool-args').textContent = argStr;
        card.querySelector('.tool-body pre').textContent = t.result || '';
        card.querySelector('.tool-head').addEventListener('click', () => card.classList.toggle('open'));
        tools.appendChild(card);
      }
    }
    // A step that ran tools and said nothing is real work, but a group with
    // nothing at all in it is noise left behind by an interrupted turn.
    if (!text && !reason && !g.parts.some(p => (p.tools || []).length)) body.closest('.msg').remove();
  }
  stickToBottom = true;
  scrollBottom();
}

// auto-scroll sticks to the bottom until the user scrolls up; then a
// floating jump button offers the way back down.
function scrollBottom() {
  if (!stickToBottom) { if (jumpBtn) jumpBtn.hidden = false; return; }
  messagesEl.scrollTop = messagesEl.scrollHeight;
  if (jumpBtn) jumpBtn.hidden = true;
}
messagesEl.addEventListener('scroll', () => {
  const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  stickToBottom = nearBottom;
  if (jumpBtn) jumpBtn.hidden = nearBottom;
});
if (jumpBtn) jumpBtn.addEventListener('click', () => {
  stickToBottom = true;
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: 'smooth' });
  jumpBtn.hidden = true;
  inputEl.focus();
});

function renderSessionList() {
  sessionListEl.innerHTML = '';
  for (const s of sessions.slice(0, 30)) {
    const el = document.createElement('div');
    el.className = 'session-item' + (s.id === activeId ? ' active' : '');
    el.innerHTML = `
      <svg class="s-icon" viewBox="0 0 24 24" fill="none"><path d="M5 4h14v11H8l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
      <span class="s-title"></span>
      <button class="s-del" title="Delete"><svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>`;
    const titleEl = el.querySelector('.s-title');
    titleEl.textContent = s.title || 'untitled';
    titleEl.title = 'Double-click to rename';
    titleEl.addEventListener('dblclick', e => {
      e.stopPropagation();
      inlineRename(titleEl, s.title, v => renameSession(s.id, v));
    });
    el.addEventListener('click', e => {
      if (e.target.closest('.s-del')) return;
      switchSession(s.id);
    });
    el.querySelector('.s-del').addEventListener('click', e => {
      e.stopPropagation();
      deleteSession(s.id);
    });
    sessionListEl.appendChild(el);
  }
}

async function adoptSession(sid, label) {
  try {
    const rec = await (await fetch(`/api/sessions/${sid}`)).json();
    if (!rec || !rec.id) { toast('could not load the new session', true); return; }
    if (!sessions.find(s => s.id === rec.id)) {
      sessions.unshift({
        id: rec.id, title: rec.title || 'Session', model: rec.model, mode: rec.mode,
        thinking: rec.thinking, workdir: rec.project || '', created: rec.created,
        // The whole stored shape, not a {role, content} projection. A fork copies
        // the transcript server-side, where a turn is one assistant row per step
        // with its tool calls attached and often no text at all; stripping tools
        // here rendered every one of those steps as an empty "Tacit" bubble, so a
        // fork of a tool-heavy turn looked like the model had sent dozens of
        // blank messages. renderTranscript already draws reason and tools.
        messages: rec.messages || [],
      });
    }
    activeId = rec.id;
    persist();
    renderSessionList();
    endStreamQuiet();
    renderTranscript(cur());
    renderModelChip();
    renderTokMeter();
    attachCurrent();
    if (label) toast(label);
  } catch (e) {
    toast('could not load the new session', true);
  }
}

function switchSession(id) {
  if (id === activeId) return;
  activeId = id;
  persist();
  renderSessionList();
  endStreamQuiet();
  renderTranscript(cur());
  renderModelChip();
  renderTokMeter();
  attachCurrent();
}

function endStreamQuiet() {
  if (streaming) {
    const el = streaming.el.closest('.msg');
    if (el) el.classList.remove('streaming');
    streaming = null;
  }
  setBusy(false);
}

function deleteSession(id) {
  sessions = sessions.filter(s => s.id !== id);
  if (activeId === id) activeId = sessions.length ? sessions[0].id : null;
  persist();
  fetch(`/api/sessions/${id}`, { method: 'DELETE' }).catch(() => {});
  renderSessionList();
  endStreamQuiet();
  renderTranscript(cur());
  renderModelChip();
  renderTokMeter();
  attachCurrent();
}

// ── edit a past user message: rewind the session to it (ChatGPT-style) ──
// Server drops that turn and everything after, respawns the agent with only
// the prefix as restored context, and the edited text is re-sent as the next
// prompt — the session continues from the edit point.
let pendingEdit = null;
let editAckTimer = null;

function userTurnIndexOfBubble(wrap) {
  const inner = messagesEl.querySelector('.messages-inner');
  return [...inner.querySelectorAll('.msg.user')].indexOf(wrap);
}

function editUserMessage(wrap) {
  if (busy) { toast('Stop the running turn first, then edit', true); return; }
  if (!ws || ws.readyState !== 1) { toast('Not connected', true); return; }
  const s = cur();
  if (!s) return;
  const userTurnIndex = userTurnIndexOfBubble(wrap);
  if (userTurnIndex < 0) return;
  const contentEl = wrap.querySelector('.msg-content');
  if (!contentEl || contentEl.querySelector('.edit-area')) return;
  const old = contentEl.textContent;
  const ta = document.createElement('textarea');
  ta.className = 'edit-area';
  ta.value = old;
  ta.rows = Math.max(3, Math.min(12, old.split('\n').length + 1));
  const row = document.createElement('div');
  row.className = 'edit-actions';
  const save = document.createElement('button');
  save.className = 'ho-btn primary';
  save.textContent = 'Save & continue';
  const cancel = document.createElement('button');
  cancel.className = 'ho-btn';
  cancel.textContent = 'Cancel';
  row.append(save, cancel);
  contentEl.replaceChildren(ta, row);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const cleanup = () => renderTranscript(s);
  cancel.addEventListener('click', cleanup);
  ta.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Escape') { e.preventDefault(); cleanup(); }
    else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save.click(); }
  });
  save.addEventListener('click', () => {
    const text = ta.value.trim();
    if (!text) return;
    pendingEdit = { sid: s.id, userTurnIndex, text };
    // the client view is the authority for the rewind — send the full copy so
    // the server can adopt it even if its own transcript is shorter/absent
    ws.send(JSON.stringify({ type: 'truncate_from', userTurnIndex,
                             messages: s.messages, created: s.created }));
    clearTimeout(editAckTimer);
    editAckTimer = setTimeout(() => {
      if (!pendingEdit) return;
      pendingEdit = null;
      toast('Edit timed out — nothing changed', true);
      cleanup();
    }, 6000);
  });
}

// ── markdown ─────────────────────────────────────────────────────────
function esc(t) { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

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

messagesEl.addEventListener('click', e => {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  const code = btn.closest('.code-block').querySelector('code').textContent;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1500);
  });
});

// ── model / mode / status ────────────────────────────────────────────
function renderModelChip() {
  const s = cur();
  if (!s) {
    $('#topTitle').textContent = 'Tacit';
    $('#topMeta').textContent = 'no session — press New session';
    renderWdChip();
    return;
  }
  $('#topTitle').textContent = s.title;
  const sid = splitModelId(s.model);
  const name = (info.models.find(m => m.id === s.model) || {}).name || sid.model;
  $('#topMeta').textContent =
    `${s.mode === 'agent' ? 'Agent' : 'Chat'} · ${sid.provider ? sid.provider + ' / ' : ''}${name}`;
  renderWdChip();
}

// ── working project picker ────────────────────────────────
// The agent operates inside a real project you pick — there is no bundled
// sandbox directory. The choice is per-session, and it is the only thing that
// decides where tools read and write.
function wdBase(p) {
  const clean = String(p || '').replace(/[\\/]+$/, '');
  return clean.split(/[\\/]/).pop() || clean;
}

function renderWdChip() {
  const s = cur();
  // The chip shows this session's workspace and nothing else. The old
  // localStorage fallback made switching to a workspace-less session display
  // another session's folder as if it were bound here.
  const wd = (s && s.workdir) || '';
  const el = $('#wdLabel');
  if (el) el.textContent = wd ? wdBase(wd) : 'no workspace';
  const chip = $('#wdChip');
  if (chip) {
    chip.title = wd
      ? `Workspace: ${wd}`
      : 'No workspace — the agent chooses its own paths';
  }
}

function reconnect() {
  if (ws) { try { ws.onclose = null; ws.close(); } catch (e) {} ws = null; }
  connect();
}

async function openWdMenu() {
  const menu = $('#wdMenu');
  const active = (cur() && cur().workdir) || '';
  let data = { recent: [] };
  try {
    const r = await fetch('/api/projects');
    data = await r.json();
  } catch (e) { /* recent + browse only */ }
  const item = (p, showDir) => {
    const isCur = p.path === active;
    return `<button class="wd-item${isCur ? ' active' : ''}" data-path="${esc(String(p.path))}">` +
      `<span class="wd-name">${esc(String(p.name || wdBase(p.path)))}</span>` +
      (showDir ? `<span class="wd-dir">${esc(String(p.path))}</span>` : '') +
      `</button>`;
  };
  const recent = (data.recent || []).map(p => item(p, true)).join('');
  // A workspace is any folder the user points at. There is deliberately no
  // list of "found" folders, because nothing assumes where projects live.
  const none = `<button class="wd-item${active ? '' : ' active'}" data-path="">` +
    `<span class="wd-name">No workspace</span>` +
    `<span class="wd-dir">the agent chooses its own paths</span></button>`;
  const browse = `<button class="wd-item" id="wdBrowse">` +
    `<span class="wd-name">Browse…</span>` +
    `<span class="wd-dir">pick any folder on this machine</span></button>`;
  menu.innerHTML =
    `<div class="wd-list">${none}</div>` +
    (recent ? `<div class="wd-sec">Recent</div><div class="wd-list">${recent}</div>` : '') +
    `<div class="wd-sec">Workspaces</div>` +
    `<div class="wd-list">${browse}</div>`;
  menu.hidden = false;
  const pick = (p) => { menu.hidden = true; chooseWorkdir(p); };
  menu.querySelectorAll('.wd-item[data-path]').forEach(b => b.addEventListener('click', () => pick(b.dataset.path)));
  $('#wdBrowse').addEventListener('click', () => {
    menu.hidden = true;
    openFsPicker('workspace', { start: active, title: 'Choose workspace', onPick: (p) => chooseWorkdir(p) });
  });
}

/** Adopt `p` as the working project, or clear it with ''. An untouched session
 *  is retargeted in place; a session that already has turns gets a fresh one,
 *  because the working directory binds when the agent is created. */
function chooseWorkdir(p) {
  const clearing = !p;
  // Per-session, not global: the choice lands on the active session only. The
  // localStorage write is gone — a global default is what made a new session
  // inherit the previous one's folder before anyone chose anything.
  const s = cur();
  if (s && (!s.messages || !s.messages.length)) {
    s.workdir = p || '';
    persist();
    renderWdChip();
    reconnect();
    toast(clearing ? 'No workspace — the agent picks its own paths' : `Workspace → ${wdBase(p)}`);
    return;
  }
  const fresh = newSession(true);
  fresh.workdir = p || '';
  persist();
  renderTranscript(cur());
  renderModelChip();
  attachCurrent();
  toast(clearing ? 'New session with no workspace' : `New session in ${wdBase(p)}`);
}

$('#wdChip').addEventListener('click', (e) => {
  e.stopPropagation();
  const m = $('#wdMenu');
  if (m.hidden) openWdMenu(); else m.hidden = true;
});
document.addEventListener('click', (e) => {
  const m = $('#wdMenu');
  const t = e.target;
  if (m && !m.hidden && !(t && t.closest && t.closest('.wd-wrap'))) m.hidden = true;
});

// ── folder browser ────────────────────────────────────────
// We browse the real filesystem ourselves instead of raising a native dialog,
// which would look and behave differently on Windows, macOS and Linux.
//
// Each *purpose* gets its own independent instance — its own element, its own
// ids, its own cursor and selection. The workspace picker and the "clone into"
// picker answer different questions, so they must not share one modal.
const fsPickers = new Map();

function fsInstance(key) {
  if (fsPickers.has(key)) return fsPickers.get(key);
  const el = document.createElement('div');
  el.className = 'fs-overlay';
  el.id = 'fsOverlay-' + key;
  el.hidden = true;
  el.innerHTML = `
    <div class="fs-shell">
      <header class="fs-head">
        <span class="fs-title" data-fs="title">Choose folder</span>
        <button class="icon-btn ho-close" data-fs="close" title="Close">
          <svg viewBox="0 0 24 24" width="16" height="16"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        </button>
      </header>
      <div class="fs-bar">
        <button class="ho-btn small" data-fs="up" title="Up one level">↑</button>
        <input class="fs-path" data-fs="path" spellcheck="false" autocomplete="off" placeholder="path">
        <button class="ho-btn small" data-fs="gopath">Go</button>
      </div>
      <div class="fs-quick" data-fs="quick"></div>
      <div class="fs-list" data-fs="list"></div>
      <footer class="fs-foot">
        <span class="fs-note" data-fs="note"></span>
        <div class="fs-foot-actions">
          <button class="ho-btn small" data-fs="mkdir">New folder</button>
          <button class="ho-btn primary" data-fs="pick">Use this folder</button>
        </div>
      </footer>
    </div>`;
  document.body.appendChild(el);
  const inst = { key, el, path: '', parent: '', onPick: null };
  inst.q = (name) => el.querySelector(`[data-fs="${name}"]`);
  fsPickers.set(key, inst);
  wireFsInstance(inst);
  return inst;
}

function fsShowStale(inst) {
  inst.q('list').innerHTML = '<div class="fs-empty">This backend has no /api/fs routes — it is an older build. Restart Tacit (stop and start the server), then reload this page.</div>';
  inst.q('note').textContent = '';
}

async function openFsPicker(key, { start = '', title = 'Choose folder', onPick = null } = {}) {
  const inst = fsInstance(key);
  inst.onPick = onPick;
  inst.q('title').textContent = title;
  inst.el.hidden = false;
  let data = { roots: [], home: '' };
  let status = 0;
  try {
    const r = await fetch('/api/fs/roots');
    status = r.status;
    data = await r.json();
  } catch (e) { /* fall through to the message below */ }
  if (status === 404) { fsShowStale(inst); return; }
  const quick = inst.q('quick');
  quick.innerHTML = (data.roots || []).map(r =>
    `<button class="fs-quick-btn" data-path="${esc(String(r.path))}">${esc(String(r.name))}</button>`).join('');
  quick.querySelectorAll('.fs-quick-btn').forEach(b =>
    b.addEventListener('click', () => fsNavigate(inst, b.dataset.path)));
  const first = start || data.home || (data.roots && data.roots[0] && data.roots[0].path) || '';
  await fsNavigate(inst, first);
}

async function fsNavigate(inst, path) {
  const list = inst.q('list');
  list.innerHTML = '<div class="fs-empty">loading…</div>';
  let data, status = 0;
  try {
    const r = await fetch('/api/fs/list?path=' + encodeURIComponent(path || ''));
    status = r.status;
    data = await r.json();
  } catch (e) {
    list.innerHTML = '<div class="fs-empty">cannot reach the server</div>';
    return;
  }
  if (status === 404) { fsShowStale(inst); return; }
  if (!data || !data.ok) {
    const why = (data && (data.error || data.detail)) || ('the server returned HTTP ' + status);
    list.innerHTML = `<div class="fs-empty">${esc(String(why))}</div>`;
    return;
  }
  inst.path = data.path;
  inst.parent = data.parent || '';
  inst.q('path').value = data.path;
  inst.q('up').disabled = !data.parent;
  const rows = [];
  if (data.parent) {
    rows.push(`<button class="fs-row" data-path="${esc(data.parent)}"><span class="fs-ic">↰</span><span class="fs-nm">..</span></button>`);
  }
  for (const d of (data.dirs || [])) {
    rows.push(`<button class="fs-row" data-path="${esc(String(d.path))}"><span class="fs-ic">▸</span><span class="fs-nm">${esc(String(d.name))}</span></button>`);
  }
  list.innerHTML = rows.join('') || '<div class="fs-empty">no sub-folders here</div>';
  list.querySelectorAll('.fs-row').forEach(b =>
    b.addEventListener('click', () => fsNavigate(inst, b.dataset.path)));
  inst.q('note').textContent = `${(data.dirs || []).length} folder(s)`;
  list.scrollTop = 0;
}

function closeFsPicker(inst) { inst.el.hidden = true; }

function wireFsInstance(inst) {
  inst.q('close').addEventListener('click', () => closeFsPicker(inst));
  inst.el.addEventListener('click', (e) => { if (e.target === inst.el) closeFsPicker(inst); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !inst.el.hidden) closeFsPicker(inst);
  });
  inst.q('up').addEventListener('click', () => { if (inst.parent) fsNavigate(inst, inst.parent); });
  const goPath = () => { const v = (inst.q('path').value || '').trim(); if (v) fsNavigate(inst, v); };
  inst.q('gopath').addEventListener('click', goPath);
  inst.q('path').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); goPath(); } });
  inst.q('mkdir').addEventListener('click', async () => {
    const name = (prompt('New folder name') || '').trim();
    if (!name) return;
    const r = await fetch('/api/fs/mkdir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: inst.path, name }),
    });
    const j = await r.json().catch(() => ({}));
    if (!j.ok) { toast(j.error || 'could not create the folder', true); return; }
    await fsNavigate(inst, inst.path);
  });
  inst.q('pick').addEventListener('click', () => {
    const chosen = inst.path;
    const cb = inst.onPick;
    closeFsPicker(inst);
    if (chosen && cb) cb(chosen);
  });
}

// ── token meter (topbar) ─────────────────────────────────────────────
// Cumulative session tokens + a context
// fill bar. Adapts to compaction: the fill turns green in the zone where
// auto-compaction would fire (instead of red), pulses while a compaction
// is running, and shows —% afterwards until the next response.
const tokMeter = $('#tokMeter'), tokCount = $('#tokCount'),
      tokFill = $('#tokFill'), tokPct = $('#tokPct'), tokAuto = $('#tokAuto');

const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M'
                 : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k'
                 : String(n || 0);

// ── task strip: the session's task list, above the composer ──────────
// The Task List plugin keeps the turn's remaining work as session state, so
// it survives compaction; this strip is its visible face. Read-only: the
// model owns the list, the person watches it. Hidden entirely when the
// plugin is off or the list is empty, so an unused list costs nothing.
let taskStripTimer = 0;
async function refreshTaskStrip() {
  const strip = $('#taskStrip');
  if (!strip) return;
  const s = cur();
  const sid = s && s.id;
  if (!sid) { strip.hidden = true; return; }
  try {
    const r = await fetch('/api/tasks/' + encodeURIComponent(sid))
      .then(r => r.json());
    if (!r.ok) { strip.hidden = true; return; }
    const tasks = r.tasks || [];
    if (!tasks.length) { strip.hidden = true; strip._dismissed = false; return; }
    // A ✕ press hides it until the list itself changes: dismissed is cleared
    // when the count moves, so the next real update is visible again.
    if (strip._dismissed && strip._lastCount === r.count) return;
    strip._dismissed = false;
    strip.hidden = false;
    $('#taskStripCount').textContent = `${r.open}/${r.count}`;
    const list = $('#taskStripList');
    if (list.hidden && strip._lastCount !== undefined && strip._lastCount !== r.count) {
      list.hidden = false;   // something changed — show it
    }
    strip._lastCount = r.count;
    list.innerHTML = tasks.map((t, i) => `
      <div class="task-strip-item ${t.done ? 'done' : ''}">
        <span class="tick">${t.done ? '✓' : '○'}</span>
        <span class="task-text">${esc(t.text || '')}</span>
      </div>`).join('');
  } catch (e) { strip.hidden = true; }
}
function scheduleTaskStrip() {
  if (taskStripTimer) return;
  taskStripTimer = setTimeout(() => { taskStripTimer = 0; refreshTaskStrip(); }, 400);
}

function renderTokMeter() {
  const s = cur();
  const u = s && s.usage;
  if (!u) { tokMeter.hidden = true; return; }
  tokMeter.hidden = false;
  const tok = u.tokens || {};
  const ctx = u.context || {};
  tokCount.textContent = `${fmtTok(tok.total)} tok`;
  tokAuto.hidden = !autoCompactionOn;
  tokMeter.classList.toggle('compacting', !!u.compacting);
  const win = ctx.window || 0;
  const pct = ctx.percent;
  let fillPct = 0, label = 'ctx —';
  if (u.compacting) {
    label = 'compacting…';
    fillPct = 100;
  } else if (pct != null && win) {
    fillPct = Math.max(0, Math.min(100, pct));
    label = `${pct.toFixed(0)}%`;
  }
  tokPct.textContent = label;
  tokFill.style.width = fillPct + '%';
  // compaction fires when context > window − reserve; that boundary is the
  // alert zone. With auto-compaction on it is a calm "will compact" zone.
  const compactAt = win && ctx.reserve ? (1 - ctx.reserve / win) * 100 : 90;
  tokFill.className = 'tok-fill' +
    (fillPct >= compactAt ? (autoCompactionOn ? ' auto-zone' : ' alert')
     : fillPct >= compactAt - 20 ? ' warn' : '');
  const cache = (tok.cacheRead || 0) + (tok.cacheWrite || 0);
  tokMeter.title = [
    `session tokens — in ${fmtTok(tok.input)} · out ${fmtTok(tok.output)}` +
      (cache ? ` · cache ${fmtTok(cache)}` : ''),
    win ? `context — ${ctx.tokens != null ? fmtTok(ctx.tokens) + ' / ' + fmtTok(win) : 'unknown (post-compaction)'}`
        : 'context window unknown for this model',
    u.speed != null ? `speed — ${fmtTok(u.speed)} tok/s` : '',
    u.compacting ? 'compaction in progress…' :
    autoCompactionOn ? `auto-compaction on — triggers near ${compactAt.toFixed(0)}%`
                     : `auto-compaction off — compacts manually from the ⋮ menu near ${compactAt.toFixed(0)}%`,
  ].join('\n');
}

// model ids are "provider/model:tag" — the sidebar picks in two levels
function splitModelId(id) {
  const i = (id || '').indexOf('/');
  return i === -1 ? { provider: '', model: id || '' }
                  : { provider: id.slice(0, i), model: id.slice(i + 1) };
}
function modelsByProvider() {
  const map = new Map();
  // providers exist even before any model is discovered — registry order first
  for (const p of (info.providers || [])) if (!map.has(p)) map.set(p, []);
  for (const m of info.models) {
    const p = splitModelId(m.id).provider || 'local';
    if (!map.has(p)) map.set(p, []);
    map.get(p).push(m);
  }
  return map;
}
const fmtCtx = m => m.contextWindow ? ` · ${Math.round(m.contextWindow / 1024)}k` : '';

function renderProviderSelect() {
  const byProv = modelsByProvider();
  providerSelect.innerHTML = '';
  if (!byProv.size) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'no providers — open Harness';
    providerSelect.appendChild(opt);
    return;
  }
  for (const [prov, models] of byProv) {
    const opt = document.createElement('option');
    opt.value = prov;
    opt.textContent = `${prov} · ${models.length} model${models.length > 1 ? 's' : ''}`;
    providerSelect.appendChild(opt);
  }
  const s = cur();
  if (s) providerSelect.value = splitModelId(s.model).provider || 'local';
}

function renderModelSelect() {
  const models = modelsByProvider().get(providerSelect.value) || [];
  modelSelect.innerHTML = '';
  if (!models.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'no models — Re-scan in Settings';
    modelSelect.appendChild(opt);
    return;
  }
  for (const m of models) {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.name + fmtCtx(m) + (m.id === info.default ? ' (default)' : '');
    modelSelect.appendChild(opt);
  }
  const s = cur();
  if (s) {
    modelSelect.value = s.model;
    if (modelSelect.value !== s.model && models.length) modelSelect.value = models[0].id;
  }
}
function renderModelPickers() { renderProviderSelect(); renderModelSelect(); }

// Levels are per model, learned once, cached here so switching back does not ask
// again. There is deliberately no preset list to fall back to: a guessed rung is
// what this replaced.
const thinkCache = {};
async function renderThinkSelect() {
  const s = cur();
  const ref = (s && s.model) || info.default || '';
  let levels = thinkCache[ref];
  if (!levels) {
    try {
      const r = await fetch('/api/harness/thinking?ensure=1&ref=' + encodeURIComponent(ref));
      const d = await r.json();
      levels = (d.thinking_levels && d.thinking_levels.length) ? d.thinking_levels : ['default'];
    } catch (e) {
      levels = ['default'];
    }
    thinkCache[ref] = levels;
  }
  if (thinkSelect.dataset.ref !== ref || thinkSelect.options.length !== levels.length) {
    thinkSelect.dataset.ref = ref;
    thinkSelect.innerHTML = '';
    for (const l of levels) {
      const opt = document.createElement('option');
      opt.value = l;
      opt.textContent = l === 'default' ? 'model default' : l;
      thinkSelect.appendChild(opt);
    }
  }
  const want = (s && s.thinking && levels.indexOf(s.thinking) >= 0) ? s.thinking : levels[0];
  thinkSelect.value = want;
  // The record is what gets sent, so a record naming a rung this model does not have
  // would run at a level the interface is not showing. Adopt what is displayed.
  if (s && s.thinking !== want) {
    s.thinking = want;
    localStorage.setItem('tacit.thinking', want);
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'set_thinking_level', level: want }));
    }
  }
}

async function loadInfo() {
  try {
    const r = await fetch('/api/info');
    info = await r.json();
  } catch (e) { /* keep defaults */ }
  renderModelPickers();
  renderThinkSelect();
  // no permanent status widget — surface problems only, once per outage
  if (!info.llm_online && !llmWarned) {
    llmWarned = true;
    toast(`LLM unreachable · ${(info.llm_url || '').replace(/^https?:\/\//, '')}`, true);
  } else if (info.llm_online) llmWarned = false;
  if (info.default && !localStorage.getItem('tacit.seen')) {
    // first-ever load (or old registry): migrate legacy/default model picks
    localStorage.setItem('tacit.seen', '1');
    const known = new Set((info.models || []).map(m => m.id));
    sessions.forEach(s => {
      if (!s.model || !known.has(s.model) || s.model === info.default) {
        s.model = info.default;
      }
    });
    persist();
    renderModelPickers();
  }
  renderModelChip();
}

// ── wiring ───────────────────────────────────────────────────────────
$('#newChatBtn').addEventListener('click', () => {
  const s = newSession(true);
  endStreamQuiet();
  renderTranscript(s);
  renderModelChip();
  renderTokMeter();
  attachCurrent();
  pickTagline();
  inputEl.focus();
});

function applyModelSelection(id) {
  const s = cur();
  if (!s || !id || s.model === id) { renderModelChip(); return; }
  s.model = id;
  localStorage.setItem('tacit.model', s.model);
  persist(); renderModelChip();
  syncSessionMetadata(s);
  if (ws && ws.readyState === 1) {
    // Live model switch: do NOT respawn, keep the existing conversation context.
    ws.send(JSON.stringify({ type: 'set_model', model: s.model }));
  }
  toast('Model switched');
}

providerSelect.addEventListener('change', () => {
  const models = modelsByProvider().get(providerSelect.value) || [];
  if (!models.length) return;
  const pick = (models.find(m => m.id === info.default) || models[0]).id;
  renderModelSelect();          // refill the model list for this provider
  modelSelect.value = pick;     // prefer the registry default inside the provider
  applyModelSelection(pick);
});

modelSelect.addEventListener('change', () => applyModelSelection(modelSelect.value));

thinkSelect.addEventListener('change', () => {
  const s = cur();
  s.thinking = thinkSelect.value;
  localStorage.setItem('tacit.thinking', s.thinking);
  persist();
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'set_thinking_level', level: s.thinking }));
  toast(`Thinking level: ${s.thinking} (applies live)`);
});

// bridge for harness.js — it edits the registry, then asks us to refresh
window.Tacit = {
  refreshInfo: async () => { await loadInfo(); renderModelChip(); },
  toast: (t, e) => toast(t, e),
};

$('#modeSeg').addEventListener('click', e => {
  const btn = e.target.closest('button[data-mode]');
  if (!btn) return;
  const s = cur();
  s.mode = btn.dataset.mode;
  localStorage.setItem('tacit.mode', s.mode);
  document.querySelectorAll('#modeSeg button').forEach(b =>
    b.classList.toggle('active', b === btn));
  persist(); renderModelChip(); syncSessionMetadata(s);
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'client_new_session', model: s.model, mode: s.mode }));
  toast(s.mode === 'agent' ? 'Agent mode: tools on — transcript carried over'
                           : 'Chat mode: pure conversation — transcript carried over');
});

$('#sidebarToggle').addEventListener('click', () => {
  const sb = $('#sidebar');
  if (innerWidth <= 760) sb.classList.toggle('open');
  else sb.classList.toggle('hidden');
});

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
});
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(inputEl.value); inputEl.value = ''; inputEl.style.height = 'auto'; }
});
sendBtn.addEventListener('click', () => { send(inputEl.value); inputEl.value = ''; inputEl.style.height = 'auto'; });
stopBtn.addEventListener('click', stop);

// task strip: click the count line to expand/collapse; ✕ hides it for this
// visit of the session (it returns when the list changes again)
const taskStrip = $('#taskStrip');
if (taskStrip) {
  $('#taskStripCount').addEventListener('click', () => {
    const list = $('#taskStripList');
    if (list) list.hidden = !list.hidden;
  });
  $('#taskStripClose').addEventListener('click', () => {
    taskStrip.hidden = true;
    taskStrip._dismissed = true;
  });
}

// ── plan mode UI ─────────────────────────────────────────────────────
const planToggle = $('#planToggle');
const planStrip = $('#planStrip');

function setPlanMode(on) {
  planModeOn = on;
  planToggle.classList.toggle('active', on);
  refreshPlaceholder();
}
planToggle.addEventListener('click', () => setPlanMode(!planModeOn));

function renderPlanStatus(m) {
  planStrip.hidden = false;
  const phaseLabel = {
    decomposing: '1 · Decomposing request',
    exploring: '2 · Exploring the codebase',
    questions: '3 · Preparing questions',
    awaiting_answers: '4 · Your answers',
    synthesizing: '5 · Writing the plan',
    cancelled: 'Cancelled',
    failed: 'Failed',
  }[m.phase] || m.phase;
  const steps = ['decomposing', 'exploring', 'questions', 'awaiting_answers', 'synthesizing'];
  const idx = steps.indexOf(m.phase);
  let html = `<div class="ps-head"><span class="ps-icon"></span><span class="ps-phase">${esc(phaseLabel)}</span>`;
  if (m.detail) html += `<span class="ps-detail">${esc(m.detail)}</span>`;
  html += `<button class="ps-cancel" id="psCancel">Cancel</button></div>`;
  if ((m.explorers || []).length) {
    html += '<div class="ps-explorers">' + m.explorers.map(e => `
      <div class="ps-exp ${e.state}">
        <span class="ps-exp-dot"></span>
        <span class="ps-exp-label">${esc(e.label || '')}</span>
        <span class="ps-exp-act">${esc(e.activity || '')}</span>
      </div>`).join('') + '</div>';
  }
  if (idx >= 0) {
    html += `<div class="ps-progress"><div class="ps-progress-bar" style="width:${(idx + 1) / steps.length * 100}%"></div></div>`;
  }
  planStrip.innerHTML = html;
  const cancel = $('#psCancel');
  if (cancel) cancel.addEventListener('click', () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_abort' }));
  });
  scrollBottom();
}

function renderPlanQuestions(questions) {
  planStrip.hidden = false;
  let html = `<div class="ps-head"><span class="ps-icon"></span><span class="ps-phase">4 · Clarifying questions</span>
    <span class="ps-detail">answer to shape the plan — or skip</span></div><div class="pq-list">`;
  questions.forEach((q, i) => {
    html += `<div class="pq-item" data-q="${i}">
      <div class="pq-q">${esc(q.q)}</div>
      <div class="pq-opts">` +
      (q.options || []).map(o =>
        `<button class="pq-opt" data-opt="${esc(o)}">${esc(o)}</button>`).join('') +
      `<button class="pq-opt other">Other…</button>
      </div>
      <input class="pq-typed" placeholder="type your own answer…" hidden>
    </div>`;
  });
  html += `</div><div class="pq-actions">
    <button class="ho-btn primary" id="pqSubmit">Continue with these answers</button>
    <button class="ho-btn" id="pqSkip">Skip questions</button>
  </div>`;
  planStrip.innerHTML = html;

  const answers = {};
  planStrip.querySelectorAll('.pq-opt').forEach(btn => {
    btn.addEventListener('click', () => {
      const item = btn.closest('.pq-item');
      const q = item.dataset.q;
      if (btn.classList.contains('other')) {
        item.querySelector('.pq-typed').hidden = false;
        item.querySelector('.pq-typed').focus();
        return;
      }
      item.querySelectorAll('.pq-opt').forEach(b => b.classList.remove('picked'));
      btn.classList.add('picked');
      answers[q] = btn.dataset.opt;
    });
  });
  planStrip.querySelectorAll('.pq-typed').forEach(inp => {
    inp.addEventListener('input', () => { answers[inp.closest('.pq-item').dataset.q] = inp.value; });
  });

  const formatAnswers = (skipped) => questions.map((q, i) => {
    const a = skipped ? '(skipped)' : (answers[i] || '(no answer)');
    return `Q: ${q.q}\nA: ${a}`;
  }).join('\n\n');

  $('#pqSubmit').addEventListener('click', () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_answers', answers: formatAnswers(false) }));
    renderPlanStatus({ phase: 'synthesizing', detail: 'answers received — writing the plan…', explorers: [] });
  });
  $('#pqSkip').addEventListener('click', () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_answers', answers: formatAnswers(true) }));
    renderPlanStatus({ phase: 'synthesizing', detail: 'questions skipped — writing the plan…', explorers: [] });
  });
}

function renderPlanReady(plan) {
  planStrip.hidden = false;
  planStrip.innerHTML = `
    <div class="ps-head"><span class="ps-icon"></span><span class="ps-phase">Plan draft ready</span>
      <span class="ps-detail">review it, then approve to save it under ~/.tacit/plans/</span></div>
    <div class="pr-actions">
      <button class="ho-btn primary" id="prApprove">Approve plan</button>
      <button class="ho-btn" id="prReject">Reject</button>
      <button class="ho-btn" id="prHide">Dismiss</button>
    </div>`;
  $('#prApprove').addEventListener('click', () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_approve', approved: true }));
  });
  $('#prReject').addEventListener('click', () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_approve', approved: false }));
  });
  $('#prHide').addEventListener('click', hidePlanStrip);
}

function hidePlanStrip() { planStrip.hidden = true; planStrip.innerHTML = ''; }

// ── image attachments ────────────────────────────────────────────────
const attachRow = $('#attachRow');
$('#attachBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', e => {
  for (const f of e.target.files) {
    if (!f.type.startsWith('image/')) continue;
    const reader = new FileReader();
    reader.onload = () => {
      const data = String(reader.result).split(',')[1];   // strip data: prefix
      attachments.push({ data, mimeType: f.type, name: f.name });
      renderAttachRow();
    };
    reader.readAsDataURL(f);
  }
  e.target.value = '';
});

function renderAttachRow() {
  if (!attachments.length) { attachRow.hidden = true; attachRow.innerHTML = ''; return; }
  attachRow.hidden = false;
  attachRow.innerHTML = attachments.map((a, i) => `
    <span class="att-chip">
      <img src="data:${a.mimeType};base64,${a.data}" alt="">
      <span>${esc(a.name)}</span>
      <button data-i="${i}" title="Remove">×</button>
    </span>`).join('');
  attachRow.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    attachments.splice(+b.dataset.i, 1);
    renderAttachRow();
  }));
}

// ── session actions menu ─────────────────────────────────────────────
const sessMenu = $('#sessMenu');
let sessMenuOpen = false;
function closeSessMenu() { sessMenuOpen = false; sessMenu.classList.remove('open'); }
function openSessMenu() { sessMenuOpen = true; sessMenu.classList.add('open'); }
$('#sessMenuBtn').addEventListener('click', e => {
  e.stopPropagation();
  sessMenuOpen ? closeSessMenu() : openSessMenu();
});
document.addEventListener('click', () => { if (sessMenuOpen) closeSessMenu(); });
sessMenu.addEventListener('click', e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  e.stopPropagation();
  closeSessMenu();
  if (btn.dataset.act === 'rename') { startRenameTop(); return; }   // local-only, no ws needed
  if (!ws || ws.readyState !== 1) return toast('Not connected', true);
  const act = btn.dataset.act;
  if (act === 'stats') ws.send(JSON.stringify({ type: 'get_session_stats' }));
  else if (act === 'compact') { ws.send(JSON.stringify({ type: 'compact' })); toast('Compacting context…'); }
  else if (act === 'fork') { ws.send(JSON.stringify({ type: 'fork' })); toast('Forking session…'); }
  else if (act === 'clone') { ws.send(JSON.stringify({ type: 'clone_session' })); toast('Cloning session…'); }
});

// topbar title: double-click to rename inline
$('#topTitle').addEventListener('dblclick', () => startRenameTop());


// one renderer for session stats — used by the stats RPC and by any
// queue_update carrying the same fields, so the two cannot drift apart
function applySessionStats(d) {
  if (!d) return;
  const tok = d.tokens || {};
  toast(`msgs ${d.totalMessages ?? '?'} · tools ${d.toolCalls ?? '?'} · tokens ${tok.total ?? '?'} · ctx ${d.contextUsage?.percent != null ? d.contextUsage.percent.toFixed(0) + '%' : '?'}`, false);
  // reconcile the topbar meter with the agent's own counters
  const s = cur();
  if (s && d.tokens) {
    s.usage = {
      tokens: { input: tok.input || 0, output: tok.output || 0,
                cacheRead: tok.cacheRead || 0, cacheWrite: tok.cacheWrite || 0,
                total: tok.total || 0 },
      context: { tokens: d.contextUsage?.tokens ?? null,
                 window: d.contextUsage?.contextWindow || (s.usage && s.usage.context?.window) || 0,
                 percent: d.contextUsage?.percent ?? null, reserve: 16384 },
      compacting: false,
    };
    renderTokMeter();
  }
}

function handleRpcResponse(m) {
  if (m.command === 'get_session_stats' && m.ok && m.data) {
    applySessionStats(m.data);
  } else if (m.command === 'steer' || m.command === 'follow_up') {
    if (!m.ok) toast('Steer failed: ' + (m.error || ''), true);
  } else if (m.command === 'compact') {
    toast(m.ok ? 'Context compacted' : ('Compact failed: ' + (m.error || '')), !m.ok);
  } else if (m.command === 'export_html') {
    if (m.ok && m.data && m.data.path) toast('Exported → ' + m.data.path);
    else toast('Export failed: ' + (m.error || ''), true);
  } else if (m.command === 'get_state') {
    if (m.ok && m.data) renderTokMeter();
  } else if (m.command === 'bash') {
    if (m.ok && m.data) toast(`command finished (exit ${m.data.code ?? 0})`);
    else toast('command failed: ' + (m.error || ''), true);
  } else if (m.command === 'fork' || m.command === 'clone') {
    if (m.ok && m.data && m.data.sid) adoptSession(m.data.sid, m.command === 'fork' ? 'Forked — continuing from the copy' : 'Cloned');
    else toast((m.command === 'fork' ? 'Fork' : 'Clone') + ' failed: ' + (m.error || ''), true);
  } else if (m.command === 'new_session') {
    toast(m.ok ? 'Fresh session started' : ('New session failed: ' + (m.error || '')), !m.ok);
  }
}

$('#suggestions').addEventListener('click', e => {
  const chip = e.target.closest('.chip');
  if (chip) send(chip.dataset.s);
});

let toastEl = null;
function toast(text, isError) {
  if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'toast'; document.body.appendChild(toastEl); }
  toastEl.textContent = text;
  toastEl.style.borderColor = isError ? 'var(--err)' : 'var(--line-2)';
  toastEl.classList.add('show');
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => toastEl.classList.remove('show'), isError ? 5000 : 2200);
}

const termOverlay = $('#termOverlay'), termOut = $('#termOut'), termIn = $('#termIn');
const termCwd = $('#termCwd'), termShellName = $('#termShellName'), termPrompt = $('#termPrompt');
let termHistory = [], termHistIdx = -1, termBusy = false;

function termPrint(text, cls) {
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = text;
  termOut.appendChild(line);
  termOut.scrollTop = termOut.scrollHeight;
}

function openTerminal() {
  termOverlay.hidden = false;
  if (!termOut.childElementCount) {
    termPrint('Tacit terminal — commands run in the workspace', 'term-note');
  }
  const s = cur();
  termCwd.textContent = (s && s.workdir) || '';
  termIn.focus();
}

function closeTerminal() { termOverlay.hidden = true; }

async function termRun(cmd) {
  if (!cmd.trim() || termBusy) return;
  termBusy = true;
  termHistory.push(cmd);
  termHistIdx = termHistory.length;
  termPrint(`> ${cmd}`, 'term-echo');
  termIn.value = '';
  const s = cur();
  try {
    const r = await fetch('/api/terminal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd, project: (s && s.workdir) || '' }),
    });
    const d = await r.json();
    if (d.cwd) termCwd.textContent = d.cwd;
    if (d.shell) termShellName.textContent = d.shell;
    if (d.stdout) termPrint(d.stdout.replace(/\n$/, ''), 'term-stdout');
    if (d.stderr) termPrint(d.stderr.replace(/\n$/, ''), 'term-stderr');
    if (!d.ok) termPrint(d.error || 'command failed', 'term-stderr');
    else if (d.code) termPrint(`[exit ${d.code}]`, 'term-note');
  } catch (e) {
    termPrint(`failed: ${e.message || e}`, 'term-stderr');
  }
  termBusy = false;
  termIn.focus();
}

$('#termBtn').addEventListener('click', openTerminal);
$('#termClose').addEventListener('click', closeTerminal);
$('#termClear').addEventListener('click', () => { termOut.innerHTML = ''; termIn.focus(); });
termOverlay.addEventListener('click', e => { if (e.target === termOverlay) closeTerminal(); });
termIn.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); termRun(termIn.value); }
  else if (e.key === 'Escape') closeTerminal();
  else if (e.key === 'ArrowUp') {
    if (termHistIdx > 0) { termHistIdx -= 1; termIn.value = termHistory[termHistIdx] || ''; }
    e.preventDefault();
  } else if (e.key === 'ArrowDown') {
    if (termHistIdx < termHistory.length - 1) {
      termHistIdx += 1;
      termIn.value = termHistory[termHistIdx] || '';
    } else {
      termHistIdx = termHistory.length;
      termIn.value = '';
    }
    e.preventDefault();
  }
});
document.addEventListener('keydown', e => {
  if (termOverlay.hidden) return;
  if (e.key === 'Escape') closeTerminal();
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'l') {
    e.preventDefault();
    termOut.innerHTML = '';
  }
});

// No session is created here. A session exists because the user asked for one,
// never because a page was loaded. Everything below tolerates an empty list.
if (!activeId || !sessions.find(s => s.id === activeId)) {
  activeId = sessions.length ? sessions[0].id : null;
}

// init mode seg from session
(function initSeg() {
  const s = cur();
  // Guard: a missing session used to throw here and abort the ENTIRE module,
  // which killed boot()/connect() and left the UI on "connecting...".
  if (!s) return;
  document.querySelectorAll('#modeSeg button').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === s.mode));
})();
updateSteerHint();

// Boot: load server-side session registry (cross-device), then connect.
(async function boot() {
  await initSessions();
  connect();
  await loadInfo();
  renderModelPickers();
  renderThinkSelect();
  renderModelChip();
  inputEl.focus();
})();

// Public surface for sibling panels (harness.js, git.js). Both load after this file.
pickTagline();

window.Tacit = Object.assign(window.Tacit || {}, {
  getSid: () => activeId,
  getWorkdir: () => { const s = cur(); return (s && s.workdir) || ''; },
  refreshInfo: () => loadInfo(),
  pickDir: ({ key = 'workspace', ...opts } = {}) => openFsPicker(key, opts),
  // the assistant panel shares this one socket rather than opening its own
  sendMessage: (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); },
  toast,
  // and this renderer: the assistant's answers are markdown from the same source
  md: (src) => md(src),
});
})();
