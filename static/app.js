/* Tacit Web — the chat client, spoken to over a WebSocket */
(() => {
'use strict';

// The id generator lives in transport.js. The reason is not tidiness: a page served
// over a LAN origin (http://192.168.x.x:8550) is an insecure context, where
// crypto.randomUUID does not exist at all. Two copies of that fallback is how one
// context gets ids another context cannot make.
const uuid = () => window.TacitTransport.uuid();

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
// ONE SOCKET PER SESSION (stage 1b). The old single transport carried every
// session through an in-band `switch`, which is how a turn running in session
// A could be steered, stopped and saved into session B. Now a socket is born
// attached to one session and stays attached: switching sessions opens a
// different socket, and a busy session keeps streaming into its own socket
// while you sit in another one. `ws` below is always the ACTIVE session's
// socket — every send path reads it, so the rest of this file keeps working.
let ws = null;
let sockets = new Map();   // sid -> WebSocket, one per session
let streaming = null;   // {msg, el} current assistant message being streamed
// Which card the turn in flight owns: {sid, msg, el} where `el` is the `.msg`
// wrap. `streaming` is the live writer and it is cleared by a session switch,
// by a reconnect, and by any idle report the server makes, so it cannot answer
// "which bubble is this turn's" once it is null — and the answer was being taken
// from "the last assistant bubble on screen", which at the moment the server
// marks a session busy is the bubble the PREVIOUS turn finished in. That is the
// whole card-flow corruption: the new turn's deltas were written into the old
// card, and only a reload drew the transcript the way it is stored.
let openCard = null;
let busy = false;
// True while this session's transcript is being folded. It is not `busy`: a hand
// compaction runs with no turn in flight, and the composer has to shut for it
// anyway, because a prompt started against a record mid-fold races the write that
// is about to land.
let compacting = false;
let reconnectTimer = null;
let wsDead = false;
let sessionStarting = false;
let pendingSend = null;
let planModeOn = false;          // next send runs plan orchestration
let planLive = false;            // an orchestration is currently running
let planText = '';               // the draft the strip is showing, kept so the
                                 // approval card can still display what was approved
let attachments = [];            // {data(base64), mimeType, name}
let autoCompactionOn = true;
let stickToBottom = true;
let llmWarned = false;
let serverSyncTimer = null;
let serverRegistryLoaded = false;

// ── themes (apply before first paint) ────────────────────────────────
// Eight composed worlds, not one shape re-hued eight times. Each hint names the
// ground and the accent, because that is what you are actually choosing between;
// a hint that names a mood ('neon cyber') tells you nothing you can pick on.
// Ordered darks, lights, then the two AAA instruments.
const THEMES = [
  { id: 'graphite', name: 'Graphite', tone: 'dark', hint: 'warm graphite, petrol accent' },
  { id: 'verdigris', name: 'Verdigris', tone: 'dark', hint: 'cool grey-green, copper' },
  { id: 'mono', name: 'Mono', tone: 'dark', hint: 'greyscale; colour only for status' },
  { id: 'amethyst', name: 'Amethyst', tone: 'dark', hint: 'plum ground, iris accent' },
  { id: 'nocturne', name: 'Nocturne', tone: 'dark', hint: 'indigo-slate, one brass lamp' },
  { id: 'lagoon', name: 'Lagoon', tone: 'dark', hint: 'deep teal-slate, water cyan' },
  { id: 'rosewood', name: 'Rosewood', tone: 'dark', hint: 'plum-rose ground, dusty mauve' },
  { id: 'absinthe', name: 'Absinthe', tone: 'dark', hint: 'green-slate, chartreuse' },
  { id: 'iris', name: 'Iris', tone: 'dark', hint: 'cool neutral, deep violet-blue' },
  { id: 'slate', name: 'Slate', tone: 'dark', hint: 'mid-tone grey, amber indicator' },
  { id: 'oxide', name: 'Oxide', tone: 'dark', hint: 'iron ground, chalk accent' },
  { id: 'kinpaku-dark', name: 'Kinpaku dark', tone: 'dark', hint: 'black lacquer, gold leaf' },
  { id: 'contrast', name: 'Contrast', tone: 'dark', hint: 'AAA, white on black' },
  { id: 'vellum', name: 'Vellum', tone: 'light', hint: 'warm paper, ultramarine' },
  { id: 'signal', name: 'Signal', tone: 'light', hint: 'cool white, chart-series colour' },
  { id: 'celadon', name: 'Celadon', tone: 'light', hint: 'jade-glaze ground, deep teal' },
  { id: 'frost', name: 'Frost', tone: 'light', hint: 'blue-white ground, deep cyan' },
  { id: 'monotype', name: 'Monotype', tone: 'light', hint: 'neutral light; the accent is ink' },
  { id: 'foxglove', name: 'Foxglove', tone: 'light', hint: 'warm light, muted plum' },
  { id: 'sorrel', name: 'Sorrel', tone: 'light', hint: 'pale clay ground, indigo ink' },
  { id: 'kinpaku', name: 'Kinpaku', tone: 'light', hint: 'gold-leaf paper, patina teal' },
  { id: 'contrast-light', name: 'Contrast light', tone: 'light', hint: 'AAA, black on white' },
];
const DEFAULT_THEME = 'graphite';
// The appearance revamp replaced every palette the old world shipped and renamed
// most of them. A
// stored id from before that is not an invalid preference, it is a preference for
// a world that no longer exists -- so it is carried to the nearest survivor by
// temperature and lightness instead of falling through to the default, which would
// quietly move someone who had picked a light theme onto a dark one.
const THEME_CARRIED = {
  midnight: 'graphite', ocean: 'graphite', daybreak: 'signal',
  vapor: 'verdigris', ember: 'oxide',
};
function resolveTheme(stored) {
  if (stored && THEMES.some(t => t.id === stored)) return stored;
  return THEME_CARRIED[stored] || DEFAULT_THEME;
}
let theme = resolveTheme(localStorage.getItem('tacit.theme'));
document.documentElement.dataset.theme = theme;

// Density is a view preference, not a property of a session: it changes no stored
// content, so it belongs in localStorage beside the theme rather than on the server.
// `default` deletes the attribute instead of writing a third value -- the common case
// then costs no override block at all.
let density = localStorage.getItem('tacit.density') || 'default';
function applyDensity(id) {
  density = (id === 'compact' || id === 'roomy') ? id : 'default';
  if (density === 'default') delete document.documentElement.dataset.density;
  else document.documentElement.dataset.density = density;
  try { localStorage.setItem('tacit.density', density); } catch (e) { /* private mode */ }
  paintDensity(density);
  return density;
}
function paintDensity(id) {
  const seg = document.getElementById('densitySeg');
  if (seg) seg.querySelectorAll('button').forEach(b =>
    b.classList.toggle('active', b.dataset.dens === id));
}
applyDensity(density);

function applyTheme(id) {
  if (!THEMES.some(t => t.id === id)) return;
  theme = id;
  document.documentElement.dataset.theme = id;
  localStorage.setItem('tacit.theme', id);
}
// Hover-preview: the Appearance panel can show a theme before you choose it.
// Deliberately NOT `applyTheme` — a browse through eight palettes is not a
// decision, and `localStorage` is the decision. `theme` stays the committed one,
// so `current()` keeps answering "which theme do I actually have" while a preview
// is on screen (the panel's "Active" label must not follow the cursor), and
// `endPreview()` puts the committed one back on the document.
function previewTheme(id) {
  if (!THEMES.some(t => t.id === id)) return;
  document.documentElement.dataset.theme = id;
}
function endPreview() { document.documentElement.dataset.theme = theme; }

// consumed by the harness "Appearance" panel
window.TacitTheme = {
  themes: THEMES, current: () => theme, apply: applyTheme,
  preview: previewTheme, endPreview: endPreview,
};

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

// ── deleted session ids ──────────────────────────────────────────────
// A session this browser has seen deleted, in both directions: the ids the
// server lists in its registry, and the ones this window deleted itself (kept
// locally, because a delete performed while the server was unreachable still has
// to hold on the next load). `localStorage` holds a full copy of the session
// list, so without this a removed session is re-added by `loadServerRegistry()`
// as a "local-only session worth rescuing" and the socket then asks the server to
// create it back under the same id.
const DELETED_KEY = 'tacit.deleted';
const deletedIds = new Set((() => {
  try {
    const parsed = JSON.parse(localStorage.getItem(DELETED_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.filter(x => typeof x === 'string') : [];
  } catch (e) { return []; }
})());

function noteDeleted(id) {
  if (!id) return;
  deletedIds.add(id);
  try {
    localStorage.setItem(DELETED_KEY, JSON.stringify(Array.from(deletedIds).slice(-200)));
  } catch (e) { /* private mode: the server's own list still covers it */ }
}

// A row that the store says is gone, taken out of the list and the page.
function dropGhost(id) {
  const before = sessions.length;
  sessions = sessions.filter(s => s.id !== id);
  if (sessions.length === before) return;
  const sock = sockets.get(id);
  if (sock) { try { sock.onclose = null; sock.close(); } catch (e) {} sockets.delete(id); }
  if (openCard && openCard.sid === id) openCard = null;
  if (activeId === id) activeId = sessions.length ? sessions[0].id : null;
  persist({ localOnly: true });
  renderSessionList();
  endStreamQuiet();
  renderTranscript(cur());
  renderModelChip();
  renderTokMeter();
  attachCurrent();
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
    // Drop what the store has deleted, and what this browser deleted while the
    // store was not listening, before the loop below can mistake either for a
    // local-only session worth rescuing.
    for (const id of (reg.deleted || [])) deletedIds.add(id);
    // Filtered here rather than through `dropGhost()`: this runs during boot,
    // before the list is assembled, and a row that is not on it needs no redraw.
    sessions = sessions.filter(s => byId.has(s.id) || !deletedIds.has(s.id));
    let changed = false;
    for (const ls of sessions) {
      const ss = byId.get(ls.id);
      if (!ss) {
        if (ls.messages && ls.messages.length) { byId.set(ls.id, ls); changed = true; }
      } else if ((ls.messages || []).length > (ss.messageCount ?? (ss.messages || []).length)) {
        // local holds a longer transcript (offline edits, or the server was
        // restarted before the first persist) — keep the richer copy.
        // The comparison counts the server's HISTORY, not its rows: a registry
        // row is a preview tail, so a 300-message session arrives with 24 rows
        // and `messageCount: 300`. Comparing raw lengths made every trimmed row
        // look poorer than a local copy and pushed a truncated transcript over the
        // full one. When the server row is the richer one we simply leave it in
        // `byId` — and ensureFull() pulls the rest when the session opens.
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

// The registry sends a preview tail per session (backend/store.py
// REGISTRY_PREVIEW): boot used to download every transcript in the home — 1.2 MB
// measured, and it grows with the *longest* conversation rather than the number
// of them. Drawing the drawer only needs the tail. Opening a session needs the
// whole thing, so the tail is upgraded here, on the one path that has to have it:
// undo, rollback and "edit this turn" all index into `messages` by position, and
// cutting a tail at the row a full transcript would number 274 silently deletes
// turns nobody mentioned.
const fullInFlight = new Set();
async function ensureFull(s) {
  if (!s || !s.trimmed || fullInFlight.has(s.id)) return s;
  // A running turn appends to this array from its own socket while the stored
  // record lags behind it, so replacing the transcript mid-stream would erase the
  // bubble that is still arriving. The turn's `done` persists anyway; the next
  // open upgrades. The tail keeps drawing in the meantime.
  const st = sessionStates[s.id] || {};
  if (st.busy || st.starting || s._busy) return s;
  fullInFlight.add(s.id);
  try {
    const rec = await (await fetch(`/api/sessions/${s.id}`)).json();
    if (rec && rec.id === s.id) {
      s.messages = rec.messages || [];
      s.messageCount = s.messages.length;
      delete s.trimmed;
      persist({ localOnly: true });      // we just read the server; no reason to push back
      if (s.id === activeId) { renderTranscript(s); renderTokMeter(); }
    }
  } catch (e) {
    // stay a tail: the drawer works, and the next open retries
  } finally {
    fullInFlight.delete(s.id);
  }
  return s;
}

async function initSessions() {
  await loadServerRegistry();
  if (!activeId || !sessions.find(s => s.id === activeId)) {
    activeId = sessions.length ? sessions[0].id : null;
  }
  renderSessionList();
  // The socket is already open by now (boot connects before this runs), so the
  // transcript upgrade cannot delay it — this await only delays the first paint
  // of history, which would otherwise paint 24 rows and then jump.
  await ensureFull(cur());
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
  // The wire contract lives in transport.js, so there is exactly one copy of this URL.
  // Every call site here is unchanged.
  return window.TacitTransport.wsUrl(s);
}

function connect() {
  clearTimeout(reconnectTimer);
  const s = cur();
  if (!s) { setConn(''); return; }        // nothing to attach to yet
  connectSession(s);
}

// Open (or reuse) the socket for one session. The active session's socket is
// also stored in `ws`, so every existing send path keeps reading `ws`.
function connectSession(s) {
  if (!s) return;
  const existing = sockets.get(s.id);
  if (existing && existing.readyState === 1) {
    if (s.id === activeId) {
      ws = existing;
      // The state verifier, ask side: a socket that stayed open while you
      // were in another session never re-sends hello, so the only way to
      // learn whether a turn is running here is to ask. The server answers
      // from the session registry — the same truth the indicator dots read.
      try { existing.send(JSON.stringify({ type: 'get_state' })); } catch (e) {}
    }
    return;                                        // transport already live
  }
  if (existing) { try { existing.onclose = null; existing.close(); } catch (e) {} sockets.delete(s.id); }
  if (s.id === activeId) {
    // The previous session's socket is NOT closed here: it stays in the map
    // and keeps streaming if a turn is running there. Idle sockets are
    // closed by the reaper. Only `ws` — the pointer the send paths read —
    // moves to the new session's socket below.
    setConn('connecting…');
  }
  const sock = new WebSocket(wsUrl(s));
  sockets.set(s.id, sock);
  if (s.id === activeId) ws = sock;
  sock.onopen = () => {
    if (s.id === activeId) { wsDead = false; setConn(''); }
    delete s._new; delete s._adopt;
  };
  sock.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    // Every event on this socket belongs to the session it was opened for.
    // The old cross-session `bgIngest` merge is gone: a session's transcript
    // is written only by its own socket, which is the isolation guarantee in
    // the interface.
    if (m.sid && m.sid !== s.id) return;
    if (s.id === activeId) handle(m);
    else handleBackground(m, s);
  };
  sock.onclose = ev => {
    sockets.delete(s.id);
    if (s.id !== activeId) return;   // a background socket closing is not this tab's problem
    wsDead = true;
    // 4404 is a different failure from a dead network: the server answered and
    // said it does not know this session. That happens when the store is
    // rebuilt or relocated underneath a browser that still holds its ids, and
    // retrying the identical request forever just prints "retrying" on the
    // status line while nothing can ever change. Ask once for the server to
    // adopt the id this tab already has; if it still refuses, stop.
    if (ev && ev.code === 4404 && s) {
      // The server says it has never seen this id. When this window deleted the
      // session, that answer is correct, and asking for the id back is the bug:
      // `?create=1` re-materialises a session the person threw away, under the id
      // and the title they removed. Drop the row instead of adopting it.
      if (deletedIds.has(s.id)) { dropGhost(s.id); return; }
      if (!s._adopted) {
        s._adopted = true;
        s._adopt = true;
        setConn('session not on server. Re-creating');
        scheduleReconnect();
        return;
      }
      setConn('session could not be restored. Start a new one');
      return;
    }
    // Everything else stays on the old behaviour: the server is restarting or
    // the link dropped, and the next attempt may genuinely succeed.
    setConn('disconnected. Retrying');
    scheduleReconnect();
  };
  sock.onerror = () => {};
}

// attach the current session to its own socket (stage 1b: no in-band switch)
function attachCurrent() {
  if (window.TacitAssistant && window.TacitAssistant.sessionChanged) {
    window.TacitAssistant.sessionChanged(activeId);
  }
  if (window.TacitDelegation && window.TacitDelegation.sessionChanged) {
    window.TacitDelegation.sessionChanged(activeId);
  }
  refreshTaskStrip();
  const s = cur();
  if (!s) return;
  // A brand-new session must ride a socket that is allowed to create it
  // (?create=1), so drop any socket it already had and open a fresh one.
  // Switching never created anything, which is why pressing New session did
  // nothing.
  if (s._new || s._adopt) {
    const old = sockets.get(s.id);
    if (old) { try { old.onclose = null; old.close(); } catch (e) {} sockets.delete(s.id); }
    ws = null;
  }
  connectSession(s);
  // The session you left keeps its socket: if a turn is running there, it
  // keeps streaming into its own transcript in the background. Idle sockets
  // are closed by the reaper, not on every switch.
}

// background stream events for a non-active session, arriving on that
// session's own socket: keep its transcript alive without touching the
// active session's view. The socket is a subscriber of the session's stream
// for as long as it is open, so a turn started anywhere — this tab, another
// tab, another device — streams here too.
function handleBackground(m, s) {
  if (m.type === 'state_delta') {
    // The busy mark is the server's truth about this session; keeping it
    // here is what stops the idle-socket reaper from closing the transport
    // of a session that is working somewhere else.
    if (m.busy != null) { s._busy = !!m.busy; renderSessionList(); }
    return;
  }
  if (m.type === 'user_message') {
    // The same row, for the same reason, in a session that is not on screen:
    // without it mk() finds an assistant row last and appends the new turn to
    // the old bubble. mk() starts a fresh one once the user row is there.
    s.messages.push({ role: 'user', content: m.text || '', origin: m.origin, ts: m.ts });
    persist();
    return;
  }
  const mk = () => {
    let last = s.messages[s.messages.length - 1];
    if (!last || last.role !== 'assistant') {
      last = { role: 'assistant', content: '', reason: '', tools: [], ts: nowTs() };
      s.messages.push(last);
    }
    return last;
  };
  if (m.type === 'text') {
    mk().content += m.delta;
    persist();
  } else if (m.type === 'reason') {
    const a = mk(); a.reason = (a.reason || '') + (m.delta || '');
  } else if (m.type === 'tool_start') {
    mk().tools.push({ id: m.id, name: m.name, args: m.args });
  } else if (m.type === 'tool_end') {
    const a = mk();
    const t = (a.tools || []).find(t => t.id === m.id);
    if (t) { t.result = m.result; t.is_error = m.is_error; }
  } else if (m.type === 'done' || m.type === 'proc_exit') {
    s._busy = false;
    persist();
    renderSessionList();       // its indicator leaves the working state
  }
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => { if (cur()) connect(); }, 1200);
}

// Idle-socket reaper. Sockets are kept for sessions that are busy or were
// recently active; everything else is closed after a grace period, so a long
// session list cannot accumulate one live transport per row. The active
// session's socket is never reaped.
const SOCKET_IDLE_MS = 30000;
setInterval(() => {
  for (const [sid, sock] of sockets) {
    if (sid === activeId || sock.readyState !== 1) continue;
    const s = sessions.find(x => x.id === sid);
    if (s && s._busy) continue;          // a working session keeps its transport
    if (Date.now() - (s && s._lastActive || 0) < SOCKET_IDLE_MS) continue;
    try { sock.onclose = null; sock.close(); } catch (e) {}
    sockets.delete(sid);
  }
}, 10000).unref?.();

function setConn(txt) { $('#connHint').textContent = txt; }
function showBootDot(on) { $('#connHint').classList.toggle('booting', on); }
function flushPending() {
  if (pendingSend && ws && ws.readyState === 1) {
    ws.send(pendingSend);
    pendingSend = null;
  }
}

// The one card this view may write into for the turn in flight. Held by
// send(), by renderTranscript() for a session that is mid-turn, and by nothing
// else: a bubble that merely happens to be the last assistant row on screen
// belongs to a turn that already ended.
function setOpenCard(s, msg, bodyEl) {
  const wrap = bodyEl && bodyEl.closest('.msg');
  if (wrap) openCard = { sid: s.id, msg, el: wrap };
  return bodyEl;
}

// The card the turn in flight owns, if this view still holds it: same session,
// still the last row of the transcript, still in the document.
function heldCard(s) {
  if (!openCard || openCard.sid !== s.id) return null;
  if (s.messages[s.messages.length - 1] !== openCard.msg) return null;
  if (!openCard.el || !openCard.el.isConnected) return null;
  return openCard;
}

// re-attach to an assistant response still streaming in this session
function resumeLiveStream() {
  const s = cur();
  if (!s || streaming) return;
  emptyState.style.display = 'none';
  const held = heldCard(s);
  if (held) {
    // renderTranscript (or send) already drew this turn's card, partial and
    // all; re-arming it is what keeps a reconnect from splitting one answer
    // across two bubbles.
    streaming = { msg: held.msg, el: held.el.querySelector('.msg-body-inner') };
    setBusy(true);
    return;
  }
  // Nothing of ours is open, so this turn starts a fresh card. Reusing the
  // previous turn's row is the bug this function used to have.
  const row = { role: 'assistant', content: '', reason: '', tools: [], ts: nowTs() };
  s.messages.push(row);
  const body = setOpenCard(s, row,
    appendMsg('assistant', '', null, row.ts, s.messages.indexOf(row)));
  streaming = { msg: row, el: body };
  setBusy(true);
}

// A user row ends the previous turn's claim on the view. `armed` is the card
// that was open when the row arrived: if it sits before this row in the
// transcript it belongs to the turn this prompt follows, so it stops receiving
// text, and if it never got any content it is a stray this view opened on a
// busy signal that arrived too early and it comes out of both the list and the
// page.
function rebaseOnUserRow(s, armed) {
  if (!armed) return;
  const at = s.messages.indexOf(armed.msg);
  // Always released, whatever position it holds: this window has already
  // returned for a prompt it typed itself, so a user row reaching here came
  // from somewhere else and starts a turn whose card does not exist yet.
  streaming = null;
  if (openCard && openCard.msg === armed.msg) openCard = null;
  const empty = !armed.msg.content && !armed.msg.reason
    && !(armed.msg.tools || []).length;
  if (!empty || at < 0) return;    // real text, or not in this transcript: leave it
  s.messages.splice(at, 1);
  const el = armed.el && armed.el.closest('.msg');
  if (el) el.remove();
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
      // The verifier's answer on a fresh socket: hello carries the session's
      // live state, and a turn that survived a reconnect (server restart, or
      // the socket being reaped and reopened) must re-arm the composer.
      if (m.busy && !streaming) resumeLiveStream();
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
      // Plan mode's live state arrives on the same hello that carries busy,
      // because it has the same problem: the strip is drawn from events, and a
      // socket that was not open missed them. The server holds the plan by
      // session id, so it can answer this question for any session, including
      // one nobody is watching.
      restorePlanStrip(m.plan || null);
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
      // The state verifier, client side: a busy delta is the session's live
      // state, not a suggestion. It re-arms the composer when a turn is
      // running (switch-back, reconnect), and an explicit not-busy delta
      // clears it — this is the "off" that used to be invisible.
      if (m.busy != null) {
        if (m.busy) {
          if (!streaming) resumeLiveStream();
        } else {
          endStreamQuiet();
        }
      }
      if (m.busy && !m.starting && !streaming) resumeLiveStream();
      if (m.assistantBusy != null && window.TacitAssistant && window.TacitAssistant.setExternalBusy) {
        window.TacitAssistant.setExternalBusy(m.assistantBusy);
      }
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
    case 'replay':
      // Stage 3.6: a hello that landed mid-turn carried the turn's events so
      // far. The model's last assistant message may already hold text that
      // handleBackground accumulated from the other socket, so the bubble is
      // rebuilt from the replay itself — the replay is authoritative, and the
      // result is idempotent no matter what the background path had added.
      {
        const s = cur();
        if (!s || !Array.isArray(m.events)) break;
        endStreamQuiet();
        if (s.messages.length && s.messages[s.messages.length - 1].role === 'assistant') {
          s.messages.pop();
        }
        const mk = () => {
          let last = s.messages[s.messages.length - 1];
          if (!last || last.role !== 'assistant') {
            last = { role: 'assistant', content: '', reason: '', tools: [], ts: nowTs() };
            s.messages.push(last);
          }
          return last;
        };
        for (const ev of m.events) {
          if (ev.type === 'text') mk().content += ev.delta || '';
          else if (ev.type === 'reason') { const a = mk(); a.reason = (a.reason || '') + (ev.delta || ''); }
          else if (ev.type === 'tool_start') mk().tools.push({ id: ev.id, name: ev.name, args: ev.args });
          else if (ev.type === 'tool_end') {
            const a = mk();
            const t = (a.tools || []).find(t => t.id === ev.id);
            if (t) { t.result = ev.result; t.is_error = ev.is_error; }
          }
        }
        persist();
        // A non-empty replay is the turn saying it is still running: paint it and
        // record the trailing card as the one this turn owns, whatever the state
        // poll has to say about `busy` three seconds from now.
        renderTranscript(s, m.events.length > 0);
        if (busy) resumeLiveStream();
      }
      break;
    case 'compacting':
      // A compaction started or finished in this session, from anywhere: the
      // turn's own fold, the one a turn start runs, or the ⋮ menu. The meter
      // pulses and the composer shuts while it is true.
      if (!m.sid || m.sid === activeId) setCompacting(m.on);
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
        // The state verifier, viewer side: a turn started anywhere — this
        // tab, another tab, another device — arms this view's streaming, and
        // its end quiets it. The stream was always the session's; the view
        // just follows it now.
        if (m.busy != null) {
          if (m.busy) { if (!streaming) resumeLiveStream(); }
          else endStreamQuiet();
        }
      }
      break;
    case 'user_message': {
      // A row the server appended to the transcript. This window draws the
      // person's own prompt the instant they press Enter, so that one is
      // matched against the local echo and dropped rather than drawn twice —
      // and every other origin (the Send button, the autonomous loop) gets a
      // real row here, which is what stops the turn's deltas being absorbed by
      // the previous assistant bubble.
      const s = cur();
      if (!s) break;
      const echoed = m.origin === 'you' && localEchoes.get(s.id) === (m.text || '');
      localEchoes.delete(s.id);
      if (echoed) break;
      // `ts` comes from the server's row (session_runtime publishes it), not from
      // this window's clock, so a prompt typed in another tab shows when it was
      // actually sent.
      // A user row always starts a new agent card. Whatever was open belongs to
      // the turn this prompt follows — the server marks the session busy before
      // the prompt reaches the transcript, so a view that armed itself on that
      // signal is holding the previous turn's card, not this one's.
      rebaseOnUserRow(s, streaming);
      s.messages.push({ role: 'user', content: m.text || '', origin: m.origin, ts: m.ts });
      persist();
      appendMsg('user', m.text || '', m.origin, m.ts);
      // A fresh assistant bubble for the turn this prompt started.
      ensureStreamingBubble();
      break;
    }
    case 'text':
      // Self-heal, the same guard 'reason' already had: a delta that arrives
      // with no card open used to be dropped on the floor, and the turn looked
      // like it had said nothing.
      if (!streaming) ensureStreamingBubble();
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
      if (!streaming) ensureStreamingBubble();
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
    case 'tool_progress':
      // Live tail of a command that is still running. Deliberately NOT pushed
      // into streaming.msg.tools: that array is the transcript's tool list, and
      // a progress chunk is not a result the model ever saw. Nothing here is
      // saved — reload the session and the card shows the finished result.
      progressToolCard(m);
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
      if (m.saved) renderPlanApproved(m);
      else { hidePlanStrip(); toast(m.message || 'Plan not saved', true); }
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
          if (target.id === activeId) {
            renderTokMeter();
            // A call just reported, so nothing is folding: the flag is cleared
            // from the provider's own event rather than a second signal.
            setCompacting(m.compacting);
          }
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
      // ...and, under its own switch, a chime that does not require the toast to
      // be read. Every warn mark rings (50%, 75%, 90%): whether three chimes in
      // one turn is too many is a listening judgement, recorded as one.
      if (m.level === 'warn') chime('warn');
      break;

    case 'autonomy_cycle':
      // A launched autonomous cycle. Deliberately no toast: the loop can run for
      // hours, and a banner every cycle is noise the tick does not need — the
      // point of the sound is that you hear it without looking at the tab.
      chime('tick');
      break;
    case 'log':
      // raw agent stderr — surface only when clearly an error
      if (/error|failed|refused|not found/i.test(m.line) && !streaming) toast(m.line, true);
      break;
    case 'error':
      // The turn's engine failed. Until now this event had no case: it fell
      // through to the assistant-prefix default and the browser showed
      // nothing while the agent said the turn could not go on.
      turnFailed = true;
      toast('Turn error: ' + (m.message || 'unknown'), true);
      break;
    case 'proc_exit':
      if (busy) endStream();
      break;
    case 'fatal':
      turnFailed = true;
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
// The prompt this window has already echoed into its own transcript. The
// server publishes every row it appends — including the ones this window
// typed — so the echo is matched and skipped instead of drawn twice. A Map
// rather than a field on the session, because sessions go to localStorage.
const localEchoes = new Map();   // sid -> text

function send(text) {
  if (compacting) {
    // Not a queue, not a steer: the fold is rewriting the transcript this prompt
    // would be appended to. Wait for it, then send.
    toast('Compacting this session\'s context. Try again in a moment', true);
    return;
  }
  text = (text || '').trim();
  if (!text && !attachments.length) return;
  if (busy && !planLive) {
    // a turn is running → steer it at its next step instead of dropping the message.
    if (ws && ws.readyState === 1) {
      const imgs = drainAttachments();
      ws.send(JSON.stringify({ type: 'steer', message: text, images: imgs }));
      const s = cur();
      const sent = nowTs();
      if (s) { s.messages.push({ role: 'user', content: text + (imgs.length ? ` · ${imgs.length} image(s)` : ''), ts: sent }); persist(); renderSessionList(); }
      appendMsg('user', text, null, sent);
      toast('Steering the running turn');
      return;
    }
  }
  if (busy && planLive) { toast('Plan mode is running. Wait for it or press Stop'); return; }
  const s = cur();
  if (!s) { toast('Press New session to start one'); return; }
  if (wsDead || !ws || ws.readyState !== 1) { connect(); toast('Reconnecting. Your message will send once the link is up'); }
  if (sessionStarting) toast('Agent is booting. Message queued, it will fire the moment it is ready');

  emptyState.style.display = 'none';
  const imgs = drainAttachments();
  localEchoes.set(s.id, text);   // the server will publish this row too
  const sent = nowTs();
  s.messages.push({ role: 'user', content: text + (imgs.length ? ` · ${imgs.length} image(s)` : ''), ts: sent });
  s.messages.push({ role: 'assistant', content: '', reason: '', tools: [], ts: sent });
  // Index this turn's first assistant row while it is being created. The backend
  // stamps a snapshot with the number of stored rows at the moment it takes one
  // (agent._turn_msg_index), which is this same row, so the two agree without
  // either side asking the other.
  const at = s.messages.length - 1;
  if (s.title === 'New session' && !s.namedByUser) s.title = autoTitle(text);
  renderSessionList();
  renderModelChip();
  persist();

  appendMsg('user', text, null, sent, at - 1);
  const el = appendMsg('assistant', '', null, sent, at);
  streaming = { msg: s.messages[s.messages.length - 1], el };
  // This bubble is this turn's card. Recorded here so a busy signal that
  // arrives after a stale idle report re-arms it rather than opening a second
  // one, and so the turn that ends can let go of it.
  setOpenCard(s, s.messages[at], el);
  setBusy(true);
  markActiveSessionState('busy');

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
  if (b) turnFailed = false;   // a fresh turn, a clean slate for the chime
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
  inputEl.placeholder = compacting
    ? 'Compacting this session\'s context…'
    : planModeOn
      ? 'Describe what to plan (research → questions → plan)…'
      : busy ? 'Steer the running turn… (■ stops it)'
             : 'Message Tacit…';
}

function setCompacting(on) {
  const next = !!on;
  if (next === compacting) return;
  compacting = next;
  // The composer goes shut, not merely quiet: the server claims the session for
  // the fold too, so a second window cannot start a turn on top of it either.
  inputEl.disabled = compacting;
  document.body.classList.toggle('compacting', compacting);
  renderTokMeter();
  refreshPlaceholder();
}

function updateSteerHint() {
  const h = $('#steerHint');
  if (h) h.hidden = !(busy && !planLive);
}

// Make sure an assistant bubble exists even if reason/text arrives before send().
// One implementation with resumeLiveStream(), on purpose: this used to fall back
// on "the last row, if it is an assistant row", which after a turn has ended is
// the previous turn's row, and on "otherwise append a bubble", which never
// recorded which bubble the turn owned. Both of those are the question
// resumeLiveStream() now answers in one place.
function ensureStreamingBubble() {
  resumeLiveStream();
}

// ── turn sounds ────────────────────────────────────────────────────
// Synthesised rather than an asset file: nothing to ship, nothing to
// download, no codec, and it is quiet by construction. Two short notes say
// "the work finished" without demanding to be looked at — the case the sound
// exists for is the tab in the background. Gated on a server preference, so
// every device pointed at this install agrees about whether it makes a noise.
//
// Two switches, because the two kinds of sound make different promises.
// `turnSound` is the end of work someone is waiting for, so it can be on by
// default. `alertSound` covers things that fire *while* work runs — a budget
// warning, one tick per cycle of a loop that can run for hours unattended —
// and starts silent: a sound that repeats by itself is the kind a person
// reaches for the mute on.
let turnSoundOn = true;
let alertSoundOn = false;
let audioCtx = null;
let turnFailed = false;   // set by an error/fatal event, read by the chime

// The tunable part, in one table, and the only place to change any of it. The four
// are separated by construction, not by luck: two rising/falling contours against
// each other, a single note for the tick, and each pair landing in a different
// register, so a chime is identifiable without seeing the tab.
//
// They were written blind — this environment has no ears — and they stay unverified
// until someone listens. Settings > Tools now has an audition button that plays all
// four past both switches for exactly that reason, so the person who can hear them
// is the one who decides, not the person who read the source.
const CHIME_NOTES = {
  done: [523, 784],     // C5 -> G5, rising: the turn finished
  error: [392, 262],    // G4 -> C4, falling: it ended on an error
  warn: [659, 523],     // E5 -> C5, a drop: still running, and not going well
  tick: [880],          // one soft A5: a new autonomous cycle started
};

// The kinds the mid-turn switch governs. 'done'/'error' answer to the master.
const ALERT_KINDS = new Set(['warn', 'tick']);

async function loadSoundPref() {
  try {
    const r = await fetch('/api/harness/sound');
    const d = await r.json();
    turnSoundOn = d.turn_sound !== false;
    alertSoundOn = d.alert_sound === true;   // silent unless the server says on
  } catch (e) { /* offline: keep the default */ }
}

// The synthesis alone, with no opinion about the switches. `chime` gates and then
// calls this; the Settings audition calls it directly, because you clicked to hear
// a sound and a mute preference must not swallow the thing that answers the click.
function tone(notes, peak) {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return;
  audioCtx = audioCtx || new AC();
  // A context created before the first gesture starts suspended; a turn can
  // end while nobody has clicked in this tab yet, so resume and let it fail
  // quietly rather than throw inside a stream handler.
  if (audioCtx.state === 'suspended') audioCtx.resume();
  const t0 = audioCtx.currentTime + 0.02;
  notes.forEach((freq, i) => {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    const at = t0 + i * 0.11;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(peak, at + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.22);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(at);
    osc.stop(at + 0.25);
  });
}

function chime(kind) {
  if (!turnSoundOn) return;                              // master switch
  if (ALERT_KINDS.has(kind) && !alertSoundOn) return;     // mid-turn sounds are opted into
  try {
    const notes = CHIME_NOTES[kind];
    if (!notes) return;            // an unknown kind is silence, not a thrown error
    // The tick is one soft mark that says "a cycle started" and then gets out of
    // the way; the rest are calls to look at the screen.
    tone(notes, kind === 'tick' ? 0.02 : 0.05);
  } catch (e) { /* audio is never worth an error banner */ }
}

// Play all four, in order, spaced so four chimes do not arrive as one smear.
// Deliberately ignores both switches — this is the listening pass the table above
// has been waiting for. Returns the count so the panel can say what it played.
function previewChimes() {
  ['done', 'error', 'warn', 'tick'].forEach((kind, i) => {
    setTimeout(() => {
      try { tone(CHIME_NOTES[kind], kind === 'tick' ? 0.02 : 0.05); } catch (e) {}
    }, i * 750);
  });
  return 4;
}

function endStream() {
  const ranHere = !!streaming;
  if (streaming) {
    // flush any unthrottled tail content (rAF may still be pending)
    const cc = streaming.el.querySelector('.msg-content');
    if (cc && cc._dirty) { cc._dirty = false; cc.innerHTML = md(cc._raw || ''); }
    const el = streaming.el.closest('.msg');
    if (el) el.classList.remove('streaming');
    const blk = streaming.el.querySelector('.reason-block');
    if (blk) {
      if (streaming.msg.reason) {
        blk.classList.remove('open');   // collapsed again: the trace must not bury the answer
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
  // The turn is over, so nothing owns a card any more. This is the line that
  // keeps the next turn off the previous answer: until the card is released
  // here, `resumeLiveStream()` treats it as still open.
  openCard = null;
  setBusy(false);
  markActiveSessionState('idle');
  // The chime belongs to a turn that really ended in this view (ranHere), not
  // to a quiet re-arm after a session switch. A turn that ended on an error
  // gets the lower two-note; a finished one the rising pair.
  if (ranHere) chime(turnFailed ? 'error' : 'done');
  turnFailed = false;
  persist();
  // The undo point this turn took appears without a reload: the snapshot was
  // written mid-turn, so the directory only now has a row to match this bubble's
  // index against.
  const ended = cur();
  if (ended) decorateSnapshots(ended.id);
}

function stop() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'abort' }));
  toast('Abort requested');
}

// ── rendering ────────────────────────────────────────────────────────
// Who was actually at the keyboard. The agent sees a plain user row either
// way — these tags exist so the person can tell their own words from one the
// Assistant sent, and never mistake an autonomous prompt for something they
// typed and forgot.
const MSG_TAGS = { assistant: 'Assistant', autonomous: 'Autonomous-Mode' };

// ── the transcript's clock ───────────────────────────────────────────
// `ts` is epoch seconds, written by store.append (store.py:154) and carried on
// every row registry() serves. It is formatted here, at display, and never
// stored localised: a string with one machine's timezone in it would end up in
// the record, in the markdown export, and in front of the model. HH:MM only --
// a date on every row is noise, and the full stamp is on title=.
function timeOf(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// No ts, no element. A time is never invented for a row that has none: that
// covers everything written before the field existed and anything that arrived
// through merge(), where the client's own rows replace the server's.
function timeHtml(ts) {
  const t = timeOf(ts);
  if (!t) return '';
  const d = new Date(ts * 1000);
  return `<span class="msg-time" title="${esc(d.toLocaleString())}">${t}</span>`;
}

// A row created before the server's copy lands is stamped with this window's
// clock, so the time appears the moment you send rather than on reload. It is a
// fallback, not the truth: the stored row carries the server's `ts`, and a
// reload replaces it.
function nowTs() { return Date.now() / 1000; }

// ── snapshots beside the message that caused them ─────────────────────────
// A snapshot knows which row it belongs to because the backend wrote that index
// beside the copy (extras.py's `.message` marker, set by agent._auto_snapshot
// through _turn_msg_index). It is metadata *about* the message, never content in
// it: the transcript sent to the model and the saved markdown export are
// unchanged. That is the constraint this feature is specified under — a snapshot
// marker must never reach what the model reads or what the person exports.
let snapRows = [];   // this session's snapshots, as /api/snapshots returns them
let snapSeq = 0;     // a slow response must not decorate a newer session

function decorateSnapshots(sid) {
  snapSeq++;
  const mine = snapSeq;
  snapRows = [];
  if (!sid) { paintSnapshotLinks(); return; }
  fetch(`/api/snapshots?session=${encodeURIComponent(sid)}`)
    .then((r) => (r && r.ok ? r.json() : { snapshots: [] }))
    .then((d) => {
      if (mine !== snapSeq) return;              // a newer render already won
      snapRows = (d && d.snapshots) || [];
      paintSnapshotLinks();
    })
    .catch(() => { /* no control beats one that opens nothing */ });
}

// One control per snapshot, on the row that caused it. A snapshot with no marker
// -- the agent's own `snapshot` tool, or anything taken before this existed -- is
// skipped rather than guessed at, and a session with no snapshots paints nothing.
function paintSnapshotLinks() {
  const byMsg = {};
  for (const s of snapRows) {
    if (typeof s.message !== 'number') continue;
    (byMsg[s.message] = byMsg[s.message] || []).push(s);
  }
  messagesEl.querySelectorAll('.msg.assistant[data-msgi]').forEach((el) => {
    // Idempotent: this runs on render *and* at the end of a turn, so a control
    // already on the row would otherwise double on every reload-free turn.
    el.querySelectorAll('.msg-snap').forEach((b) => b.remove());
    const rows = byMsg[el.dataset.msgi];
    if (!rows || !rows.length) return;
    const role = el.querySelector('.msg-role');
    if (!role) return;
    const at = role.querySelector('.msg-time');   // the clock keeps the right edge
    for (const s of rows) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'msg-snap';
      b.dataset.snap = s.name;
      b.textContent = 'snapshot';
      b.title = `${s.name} · ${s.file_count} file(s) · taken before this turn's first edit · opens compare`;
      role.insertBefore(b, at);
    }
  });
}

function appendMsg(role, text, origin, ts, msgi) {
  if (role === 'user') stickToBottom = true;
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  // The position of the row this bubble draws, on the element itself. Snapshots
  // are linked to a message by exactly that index, so this is where the two
  // coordinate systems meet; nothing else in the renderer needs it.
  if (msgi != null) wrap.dataset.msgi = String(msgi);
  const tag = (role === 'user' && MSG_TAGS[origin])
    ? ` <span class="msg-tag ${origin}">${MSG_TAGS[origin]}</span>` : '';
  wrap.innerHTML = `
    <div class="avatar">${role === 'user' ? 'YOU' : 'TC'}</div>
    <div class="msg-body">
      <div class="msg-role"><span class="msg-role-name">${role === 'user' ? 'You' : 'Tacit'}</span>${tag}${timeHtml(ts)}</div>
      <div class="msg-body-inner"></div>
    </div>`;
  const body = wrap.querySelector('.msg-body-inner');
  if (role === 'assistant') {
    body.innerHTML = `<div class="reason-block"><button class="reason-toggle">thinking…</button><div class="reason-body"></div></div><div class="tools"></div><div class="msg-content"></div>`;
    // No listener on .reason-toggle: one delegated click handler on messagesEl
    // routes the whole transcript (see `// delegated` below). A listener per row
    // is a listener per message, forever, on the one surface that grows without
    // bound.
  } else {
    body.innerHTML = `<div class="msg-content"></div>`;
    body.querySelector('.msg-content').textContent = text;
    const edit = document.createElement('button');
    edit.className = 'msg-edit';
    edit.title = 'Edit. Rewinds the session to this message and continues from it';
    edit.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17.05V20z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
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
  blk.classList.add('open');   // arriving trace is visible, not parked in a hidden box
  scheduleStreamPaint();
}

function addToolCard(m) {
  const tools = streaming.el.querySelector('.tools');
  const card = document.createElement('div');
  card.className = 'tool-card run';
  card.dataset.id = m.id || '';
  const argStr = m.name === 'bash' ? (m.args.command || '') :
    (m.args.path || m.args.file_path || m.args.pattern || JSON.stringify(m.args)).slice(0, 120);
  card.innerHTML = `
    <div class="tool-head">
      <span class="tool-badge"></span>
      <span class="tool-args"></span>
      <span class="tool-spin"></span>
    </div>
    <div class="tool-rail"></div>
    <div class="tool-body"><pre></pre></div>`;
  card.querySelector('.tool-badge').textContent = m.name || 'tool';
  card.querySelector('.tool-args').textContent = argStr;
  // Follow the block's end only if the reader was already there. Measured BEFORE the
  // append: after it the block has already grown, so atEnd would read false for every
  // long result and the newest call would land below the fold.
  const atEnd = tools.scrollHeight - tools.scrollTop - tools.clientHeight < 40;
  tools.appendChild(card);
  if (atEnd) tools.scrollTop = tools.scrollHeight;
  scrollBottom();
}

const TOOL_TAIL_CHARS = 32768;  // newest end wins: the card is a window, not a file

function progressToolCard(m) {
  const card = messagesEl.querySelector(`.tool-card[data-id="${CSS.escape(m.id || '')}"]`);
  if (!card) return;
  // No card means the command already finished, or a replay overtook its own
  // tool_start. Dropping the chunk is the right call either way: the tail is a
  // live view only, and `tool_end` replaces the body with the real result.
  const pre = card.querySelector('.tool-body pre');
  if (!pre) return;
  if (m.chunk) {
    pre.textContent += m.chunk;
    if (pre.textContent.length > TOOL_TAIL_CHARS) {
      pre.textContent = pre.textContent.slice(-TOOL_TAIL_CHARS);
    }
    // Follow the output only while the body is open, so a collapsed card can
    // never move anything on screen.
    if (card.classList.contains('open')) {
      const body = card.querySelector('.tool-body');
      body.scrollTop = body.scrollHeight;
    }
  }
  let meta = card.querySelector('.tool-meta');
  if (!meta) {
    meta = document.createElement('span');
    meta.className = 'tool-meta';
    const head = card.querySelector('.tool-head');
    head.insertBefore(meta, head.querySelector('.tool-spin'));
  }
  // A heartbeat arrives with an empty chunk and only this number moving, which
  // is the whole point: a build that prints nothing for two minutes must not
  // look like the hang it is not.
  const chars = m.chars || 0;
  const size = chars >= 1024
    ? (chars / 1024).toFixed(chars >= 10240 ? 0 : 1) + ' KB'
    : chars + ' B';
  meta.textContent = `${Math.round(m.elapsed_s || 0)}s · ${size}`;
  // The bar runs up while the call is live. This is the one place in the transcript
  // where motion carries information rather than decoration: a long command's bar
  // lengthening is the progress, and it stops being true the moment the call ends.
  setRail(card, (m.elapsed_s || 0) * 1000);
}

// One place decides how a duration reads, so the live card, the finished card and
// a card rebuilt from the stored transcript all say the same thing about the same
// call. Sub-second work is the common case and "0.4s" hides that a call took 40ms.
function fmtMs(ms) {
  if (ms === null || ms === undefined) return '';
  const n = Number(ms);
  return n < 1000 ? `${Math.round(n)}ms` : `${(n / 1000).toFixed(1)}s`;
}

function setToolMeta(card, text) {
  let meta = card.querySelector('.tool-meta');
  if (!meta) {
    meta = document.createElement('span');
    meta.className = 'tool-meta';
    const head = card.querySelector('.tool-head');
    head.insertBefore(meta, head.querySelector('.tool-spin'));
  }
  meta.textContent = text;
}

function finishToolCard(m) {
  const card = messagesEl.querySelector(`.tool-card[data-id="${CSS.escape(m.id || '')}"]`);
  if (!card) return;
  card.classList.toggle('err', !!m.is_error);
  card.classList.remove('run');
  const spin = card.querySelector('.tool-spin');
  if (spin) spin.remove();
  card.querySelector('.tool-body pre').textContent = m.result || (m.is_error ? 'error' : 'done');
  // The live tail showed "8s · 2.1 KB" while a command ran; on the finished card
  // the running byte count is meaningless, so the slot keeps its place and holds
  // what still matters: how long it took, which sandbox ran it, how many files it
  // changed. A missing elapsed_ms (an older session, or the Assistant's own loop)
  // leaves the slot empty rather than inventing a zero.
  const meta = toolCardMeta(m);
  if (meta) setToolMeta(card, meta);
  // The finished duration is the bar's real value; the live one was a running guess.
  card.dataset.ms = m.elapsed_ms || 0;
  setRail(card, m.elapsed_ms);
  railSum(card.parentElement);
}

// One builder for the finished card's meta slot. The live card and the rebuilt
// transcript card used to assemble it twice, which is the same duplication the
// plan calls out for the card markup itself. Nothing is invented here: a field the
// backend did not send simply does not appear, so an old session renders as it
// always did.
// ── tool-call rail ───────────────────────────────────────────────────────
// A fixed log scale in ms, so bars mean the same thing in every turn. Measured, not
// guessed: the first version floored at 300ms, which mapped every duration from 20ms to
// 300ms onto an identical EMPTY bar -- and fmtMs's own note says sub-second work is the
// common case, so the majority of calls would have read as taking no time. 30ms spreads
// each decade about equally (0.09-0.13 per doubling) and the display floor guarantees a
// fast call still shows a stub. An absent duration returns 0 rather than the floor:
// "not recorded" must not look like "instantly", which is the same reason the meta slot
// stays empty instead of inventing a zero.
const RAIL_MIN = 30, RAIL_MAX = 30000, RAIL_LOG = Math.log(RAIL_MAX / RAIL_MIN);
const RAIL_FLOOR = 0.05;

function railDur(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return 0;
  const clamped = Math.max(RAIL_MIN, Math.min(RAIL_MAX, n));
  return Math.max(RAIL_FLOOR, Math.log(clamped / RAIL_MIN) / RAIL_LOG);
}

// One unitless custom property drives the fill: no length is written into a style
// attribute, and the bar composites instead of laying out.
function setRail(card, ms) {
  const rail = card.querySelector('.tool-rail');
  if (rail) rail.style.setProperty('--dur', railDur(ms).toFixed(3));
}

// The turn's aggregate readout. Recomputed from the cards instead of tracked in a
// counter, because a counter that survives a reload has to be stored somewhere and
// that is a second source of truth about work the transcript already describes.
function railSum(tools) {
  if (!tools) return;
  let n = 0, total = 0, slow = 0;
  tools.querySelectorAll('.tool-card').forEach(c => {
    const ms = Number(c.dataset.ms || 0);
    if (ms > 0) { n++; total += ms; slow = Math.max(slow, ms); }
  });
  let sum = tools.querySelector('.rail-sum');
  if (!n) { if (sum) sum.remove(); return; }
  if (!sum) {
    sum = document.createElement('div');
    sum.className = 'rail-sum';
  }
  const bits = [`${n} call${n === 1 ? '' : 's'}`, fmtMs(total) + ' total'];
  if (n > 1) bits.push('slowest ' + fmtMs(slow));
  sum.textContent = bits.join(' · ');
  tools.appendChild(sum);  // also keeps it below the newest card
}

function toolCardMeta(m) {
  if (!m) return '';
  const bits = [];
  const dur = fmtMs(m.elapsed_ms);
  if (dur) bits.push(dur);
  if (m.backend) bits.push('sandbox: ' + m.backend);
  if (m.changed) bits.push(m.changed + (m.changed === 1 ? ' file changed' : ' files changed'));
  return bits.join(' · ');
}

function renderTranscript(s, midTurn) {
  const inner = ensureInner();
  inner.innerHTML = '';
  if (!s) { emptyState.style.display = ''; return; }
  emptyState.style.display = s.messages.length ? 'none' : '';
  // A registry row is a tail: what is on screen is the END of the conversation.
  // ensureFull normally replaces it within a moment, but if that fetch fails, or
  // the session was deleted mid-flight, the person must not read 24 rows as the
  // whole transcript — and the edit guard below depends on `trimmed` being a
  // state the interface admits to rather than hides.
  if (s.trimmed && s.messages.length) {
    const gap = document.createElement('div');
    gap.className = 'transcript-gap';
    gap.textContent = `${s.messageCount - s.messages.length} earlier message(s) not loaded`;
    inner.appendChild(gap);
  }
  // The transcript stores one assistant row per step — every model round that ran
  // tools gets its own row, often with no text at all. The live view streams a
  // whole turn into ONE bubble (all events land in `streaming`), so drawing a
  // bubble per stored row made a fork or reload of a tool-heavy turn read as the
  // model having sent a message for every step. Group consecutive assistant rows
  // into the turn they belong to; a user or summary row starts a new group.
  // `at` is the row's index in the stored transcript, which is the coordinate the
  // snapshot marker uses (agent._turn_msg_index). A group carries the index of its
  // first row, so an undo point lands on the reply that caused it, not on the
  // step that happened to be written last.
  const groups = [];
  s.messages.forEach((msg, i) => {
    const last = groups[groups.length - 1];
    if (msg.role === 'assistant' && last && last.role === 'assistant') last.parts.push(msg);
    else groups.push({ role: msg.role, parts: [msg], at: i });
  });
  let lastAsst = null;   // the trailing assistant card, if one survived
  for (const g of groups) {
    const head = g.parts[0];
    const text = g.parts.map(p => p.content || '').join('\n\n');
    // head.origin: the sender tag survives a reload, because it rides on the
    // stored row (and agent.transcript_messages strips it before the model).
    // head.ts: the stored row's own clock. A group's time is the time its first
    // row was written, which is when the turn started, not when it finished.
    const body = appendMsg(g.role, g.role === 'assistant' ? '' : (head.content || ''), head.origin, head.ts, g.at);
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
          <div class="tool-rail"></div>
          <div class="tool-body"><pre></pre></div>`;
        card.querySelector('.tool-badge').textContent = t.name;
        card.querySelector('.tool-args').textContent = argStr;
        card.querySelector('.tool-body pre').textContent = t.result || '';
        // Rebuilt cards get the same slot the live one showed, from the numbers
        // stored with the call - reload a session and the work still says how long
        // it took, what ran it, and what it touched.
        const meta = toolCardMeta(t);
        if (meta) setToolMeta(card, meta);
        card.dataset.ms = t.elapsed_ms || 0;
        setRail(card, t.elapsed_ms);
        tools.appendChild(card);
        railSum(tools);
      }
    }
    // A step that ran tools and said nothing is real work, but a group with
    // nothing at all in it is noise left behind by an interrupted turn.
    if (!text && !reason && !g.parts.some(p => (p.tools || []).length)) body.closest('.msg').remove();
    else lastAsst = body;
  }
  // A session that is mid-turn has exactly one card still open: the trailing
  // assistant group, the row the turn in flight is writing into. Recording it
  // here is what lets a reload, a switch-back or a reconnect re-arm that bubble
  // instead of opening an empty one beside it — and a card that is not the last
  // group can never be adopted, which is the other half of the fix.
  // `midTurn` is the caller's own knowledge, and the replay path needs it: a
  // viewer that arrives mid-turn is handed the turn's buffered events, which is
  // proof the turn is running, while the state poll that feeds `sessionStates`
  // is up to three seconds stale and `endStreamQuiet()` has just cleared `busy`.
  // Without it the rebuilt card is not recorded, and the turn's first delta
  // opens a second empty bubble beside the text the replay already painted.
  const live = !!midTurn || !!(sessionStates[s.id] && sessionStates[s.id].busy)
    || s._busy || (s.id === activeId && busy);
  if (live && lastAsst && groups.length && groups[groups.length - 1].role === 'assistant') {
    setOpenCard(s, s.messages[s.messages.length - 1], lastAsst);
  } else if (openCard && openCard.sid === s.id) {
    openCard = null;
  }
  // Async on purpose: the transcript paints the instant the rows are known, and
  // the undo points arrive when the snapshot directory has been read.
  decorateSnapshots(s.id);
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

// ── left-bar activity indicators (plan item 2) ───────────────────────
// Which sessions are working, waiting or failed, per session, like Claude
// Code's sidebar. Two sources, one truth: the server's registry
// (GET /api/sessions/state) is authoritative, and this tab's own socket
// events refine it between polls.
let sessionStates = {};        // sid -> {busy, starting, kind, since, turns}
let statesTimer = null;

async function pollSessionStates() {
  try {
    const r = await fetch('/api/sessions/state');
    if (!r.ok) return;
    const d = await r.json();
    const next = d.states || {};
    const changed = JSON.stringify(next) !== JSON.stringify(sessionStates);
    sessionStates = next;
    // Mirror busy into the session rows the reaper and the dots read.
    for (const s of sessions) {
      const st = next[s.id];
      s._busy = !!(st && st.busy);
      if (s._busy) s._lastActive = Date.now();
    }
    if (changed) renderSessionList();
  } catch (e) { /* offline: the dots keep their last known state */ }
}

function startStatesPoll() {
  if (statesTimer) return;
  pollSessionStates();
  statesTimer = setInterval(pollSessionStates, 3000);
}

// The active session's own events refine its dot immediately, without
// waiting for the next poll.
function markActiveSessionState(state) {
  const s = cur();
  if (!s) return;
  if (state === 'busy') { s._busy = true; s._lastActive = Date.now(); }
  else if (state === 'idle') s._busy = false;
  renderSessionList();
}

function renderSessionList() {
  sessionListEl.innerHTML = '';
  for (const s of sessions.slice(0, 30)) {
    const st = sessionStates[s.id] || {};
    const working = !!(st.busy || s._busy);
    const el = document.createElement('div');
    el.className = 'session-item' + (s.id === activeId ? ' active' : '')
      + (working ? ' working' : '');
    el.innerHTML = `
      <span class="s-dot" title="${working ? 'working' : 'idle'}"></span>
      <svg class="s-icon" viewBox="0 0 24 24" fill="none"><path d="M5 4h14v11H8l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
      <span class="s-title"></span>
      ${working ? '<button class="s-stop" title="Stop this session\'s turn"><svg viewBox="0 0 24 24" fill="none"><rect x="7" y="7" width="10" height="10" rx="1.5" stroke="currentColor" stroke-width="1.8"/></svg></button>' : ''}
      <button class="s-del" title="Delete"><svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>`;
    const titleEl = el.querySelector('.s-title');
    titleEl.textContent = s.title || 'untitled';
    titleEl.title = working ? 'Working…' : 'Double-click to rename';
    titleEl.addEventListener('dblclick', e => {
      e.stopPropagation();
      inlineRename(titleEl, s.title, v => renameSession(s.id, v));
    });
    el.addEventListener('click', e => {
      if (e.target.closest('.s-del') || e.target.closest('.s-stop')) return;
      switchSession(s.id);
    });
    const stopBtnRow = el.querySelector('.s-stop');
    if (stopBtnRow) stopBtnRow.addEventListener('click', e => {
      e.stopPropagation();
      stopSession(s.id);
    });
    el.querySelector('.s-del').addEventListener('click', e => {
      e.stopPropagation();
      deleteSession(s.id);
    });
    sessionListEl.appendChild(el);
  }
}

// Stop a session's turn from its row — possibly a session you are not
// looking at. Its own socket carries the abort; nothing else is touched.
function stopSession(id) {
  const sock = sockets.get(id);
  if (sock && sock.readyState === 1) {
    sock.send(JSON.stringify({ type: 'abort' }));
    toast('Stopping…');
  } else {
    // No live socket: the session cannot be mid-turn in this tab. The
    // registry may still say busy (a turn running from another tab), and
    // that turn is not this tab's to stop — say so rather than pretending.
    toast(sessionStates[id] && sessionStates[id].busy
      ? 'That turn is running in another window'
      : 'That session is not running anything', true);
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
  closeDrawer();   // before the early return: re-picking the session you are on must still clear it
  if (id === activeId) return;
  activeId = id;
  persist();
  renderSessionList();
  endStreamQuiet();
  renderTranscript(cur());
  renderModelChip();
  renderTokMeter();
  attachCurrent();
  // A registry row is a preview tail. Draw it now so the click feels instant,
  // then redraw once the whole transcript lands. Order matters: attachCurrent
  // runs first because ensureFull refuses to replace a transcript whose session
  // is mid-turn, and it is the socket that knows the turn is running.
  ensureFull(cur());
}

function endStreamQuiet() {
  if (streaming) {
    const el = streaming.el.closest('.msg');
    if (el) el.classList.remove('streaming');
    // A card that received anything is a finished turn. Some turns never
    // publish `done` — a worker that dies, an error path that ends on `error` —
    // and an idle report is the only end they get, so holding that card is
    // exactly how the next turn lands inside the previous answer.
    // An empty card is a send still in flight: a reconnect or a stale idle
    // report must not split one answer across two bubbles, so it stays held and
    // is re-armed when the busy signal arrives.
    const m = openCard && openCard.msg;
    if (m && (m.content || m.reason || (m.tools || []).length)) openCard = null;
    streaming = null;
  }
  setBusy(false);
}

async function deleteSession(id) {
  const gone = sessions.find(s => s.id === id);
  // Recorded before the request, not after it. The gap between the two is where
  // a reconnect, a registry sync or another tab used to put the session back:
  // the delete was fire-and-forget, its result never read, and the tombstone
  // only existed on the disk the browser was not consulting.
  noteDeleted(id);
  sessions = sessions.filter(s => s.id !== id);
  if (activeId === id) activeId = sessions.length ? sessions[0].id : null;
  // Its socket dies with it: a deleted session must not keep a live
  // transport (or a running turn's stream) pointed at a record that is gone.
  const sock = sockets.get(id);
  if (sock) { try { sock.onclose = null; sock.close(); } catch (e) {} sockets.delete(id); }
  if (openCard && openCard.sid === id) openCard = null;
  persist();
  renderSessionList();
  endStreamQuiet();
  renderTranscript(cur());
  renderModelChip();
  renderTokMeter();
  attachCurrent();
  let failed = '';
  try {
    const r = await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) failed = (d && d.error) || ('the server answered ' + r.status);
  } catch (e) {
    failed = 'the server did not answer';
  }
  if (!failed) return;
  // The store kept it, so the list must keep it too. A row that is only gone in
  // one window comes back on the next load, which reads as the delete failing.
  deletedIds.delete(id);
  try {
    localStorage.setItem(DELETED_KEY, JSON.stringify(Array.from(deletedIds).slice(-200)));
  } catch (e) { /* private mode */ }
  if (gone && !sessions.find(s => s.id === id)) sessions.unshift(gone);
  persist();
  renderSessionList();
  toast('Could not delete that session: ' + failed, true);
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
  if (s.trimmed) {
    // The index counted below comes from the bubbles on screen. On a preview tail
    // that count begins partway through the conversation, while the server reads
    // it as an offset into the FULL transcript — so the rewind would cut at the
    // wrong turn. Upgrade the copy first; the next click edits. (The server
    // refuses a fragment too, via _adoptable in routers/chat.py — but refusing is
    // not editing, so the client must not offer it in the first place.)
    ensureFull(s);
    toast('Loading the full history of this session: try the edit again in a moment', true);
    return;
  }
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
      toast('Edit timed out. Nothing changed', true);
      cleanup();
    }, 6000);
  });
}

// ── markdown ─────────────────────────────────────────────────────────
// `"` is load-bearing here, not decoration: the renderers interpolate esc()
// inside double-quoted attributes (data-path, title, data-opt), so a value
// carrying a bare quote escapes its attribute and lands new attributes on a
// live element — no `<` needed, so escaping `<`/`>` does not cover it. The
// other five modules' esc all replace it; this one was the odd one out.
// The leading String() is the other five's other difference: eight call sites
// in the workspace and file-browser renderers already write
// esc(String(p.path)) by hand, because a bare esc(0) used to throw inside
// md()/a renderer and the surrounding try/catch swallows it — the pane just
// silently loses its content. Latent rather than live (nothing passes a
// non-string statically), which is why those eight coercions exist at all.
// The renderer itself lives in static/markdown.js, because a second copy of a renderer
// is a second set of escaping bugs.
// These wrappers exist so every call site below is unchanged; they delegate, always.
function esc(t) { return window.TacitMarkdown.esc(t); }
function inlineMd(t) { return window.TacitMarkdown.inlineMd(t); }
function md(src) { return window.TacitMarkdown.md(src); }

// delegated: the ONE click handler for the transcript surface. Rows built by
// appendMsg() carry no listeners of their own — anything clickable inside a
// message is routed here by class, so a 2,000-message transcript costs one
// listener instead of thousands. Add a branch, not an addEventListener.
messagesEl.addEventListener('click', e => {
  const btn = e.target.closest('.copy-btn');
  if (btn) {
    const code = btn.closest('.code-block').querySelector('code').textContent;
    navigator.clipboard.writeText(code).then(() => {
      btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1500);
    });
    return;
  }
  const toggle = e.target.closest('.reason-toggle');
  if (toggle) { toggle.closest('.reason-block').classList.toggle('open'); return; }
  // Tool cards are the most numerous node in the transcript — one turn can make
  // dozens, and every one of them used to carry its own click listener.
  const head = e.target.closest('.tool-head');
  if (head) { head.closest('.tool-card').classList.toggle('open'); return; }
  // The edit button used to stopPropagation() so its own listener ran alone.
  // There is now only this listener, so there is nothing left to stop.
  // The undo point this turn left behind. Opens the session's snapshot overlay on
  // that one card and runs its Compare -- the overlay owns the restore control, so
  // the transcript never grows one of its own.
  const snap = e.target.closest('.msg-snap');
  if (snap) {
    if (window.TacitPlatform && window.TacitPlatform.openSnapshot) {
      window.TacitPlatform.openSnapshot(snap.dataset.snap);
    }
    return;
  }
  const edit = e.target.closest('.msg-edit');
  if (edit) {
    // Kept from the per-node listener, for a reason I can name: the file has two
    // document-level click handlers (1787, 2521) that close menus on an outside
    // click. Stopping here still works the same way — this branch runs at
    // messagesEl, which is below document, so the click never reaches them.
    e.stopPropagation();
    editUserMessage(edit.closest('.msg'));
    return;
  }
});

// ── model / mode / status ────────────────────────────────────────────
function renderModelChip() {
  const s = cur();
  if (!s) {
    $('#topTitle').textContent = 'Tacit';
    $('#topMeta').textContent = 'no session. Press New session';
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
      : 'No workspace. The agent chooses its own paths';
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
    toast(clearing ? 'No workspace. The agent picks its own paths' : `Workspace: ${wdBase(p)}`);
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
  inst.q('list').innerHTML = '<div class="fs-empty">This backend has no /api/fs routes. It is an older build. Restart Tacit (stop and start the server), then reload this page.</div>';
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
  let fillPct = 0, label = 'ctx';
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
    `session tokens: in ${fmtTok(tok.input)} · out ${fmtTok(tok.output)}` +
      (cache ? ` · cache ${fmtTok(cache)}` : ''),
    win ? `context: ${ctx.tokens != null ? fmtTok(ctx.tokens) + ' / ' + fmtTok(win) : 'unknown (post-compaction)'}`
        : 'context window unknown for this model',
    u.speed != null ? `speed: ${fmtTok(u.speed)} tok/s` : '',
    u.compacting ? 'compaction in progress…' :
    autoCompactionOn ? `auto-compaction on. Triggers near ${compactAt.toFixed(0)}%`
                     : `auto-compaction off. Compacts manually from the ⋮ menu near ${compactAt.toFixed(0)}%`,
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
    opt.textContent = 'no providers. Open Harness';
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
    opt.textContent = 'no models. Re-scan in Settings';
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
// what this replaced. Entries carry the time they were filled, because a bare
// fallback cached while the server-side probe was still running must not outlive
// it: that staleness is what left this picker at "model default" for a model whose
// ladder had since been measured. The TTL only guards the fallback — a real ladder
// is a measured fact and does not expire.
const thinkCache = {};
const THINK_FALLBACK_TTL = 10000;
const thinkRetries = {};
async function renderThinkSelect() {
  const s = cur();
  const ref = (s && s.model) || info.default || '';
  const hit = thinkCache[ref];
  let levels = (hit && (hit.levels.length > 1 || Date.now() - hit.at < THINK_FALLBACK_TTL))
    ? hit.levels : null;
  if (!levels) {
    // Render at once with the honest fallback; probing runs server-side in a
    // background thread, so a re-fetch below picks the real ladder up when it
    // lands. Waiting here froze the picker for as long as the probe ran.
    levels = ['default'];
    let retry = false;
    try {
      const r = await fetch('/api/harness/thinking?ensure=1&ref=' + encodeURIComponent(ref));
      const d = await r.json();
      if (d.thinking_levels && d.thinking_levels.length) levels = d.thinking_levels;
      retry = !!d.detected;
    } catch (e) {
      // A failed fetch used to cache the fallback with no retry scheduled, and
      // the picker stayed at "model default" until a full reload. Ask again.
      retry = true;
    }
    thinkCache[ref] = { levels, at: Date.now() };
    const tries = thinkRetries[ref] || 0;
    if (retry && tries < 8) {
      // the probe was still running (or the fetch failed); ask again shortly.
      // Bounded so a model that never answers stops polling instead of spinning
      // forever; a later render re-fetches anyway once the fallback expires.
      thinkRetries[ref] = tries + 1;
      setTimeout(() => { delete thinkCache[ref]; if (cur() && ((cur().model) || info.default || '') === ref) renderThinkSelect(); }, 4000);
    } else if (!retry) {
      delete thinkRetries[ref];
    }
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
  // The brand slot states which checkout this is. It used to hold a hardcoded
  // "Default Harness" — a claim about the active profile that no code ever
  // wrote, and that stopped being true the moment a second profile existed.
  const brand = document.getElementById('brandVersion');
  if (brand) {
    // `hidden` is the pre-fetch state, not the resting one. style.css pins
    // `[hidden] { display: none !important }`, so writing text into the span is
    // not enough — the attribute has to be cleared here or the version sits in
    // the DOM invisible forever, while every source-level assertion about this
    // element keeps passing.
    brand.textContent = info.version || 'version unknown';
    brand.hidden = false;
  }
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
  if (!s || s.mode === btn.dataset.mode) return;
  s.mode = btn.dataset.mode;
  localStorage.setItem('tacit.mode', s.mode);
  document.querySelectorAll('#modeSeg button').forEach(b =>
    b.classList.toggle('active', b === btn));
  persist(); renderModelChip(); syncSessionMetadata(s);
  // A mode change changes the session you are looking at. It does not ask for a new
  // one. Stage 1b made this click also create a session, on the stated theory that
  // the transcript could not cross a mode boundary; carrying it is what the app did
  // before Stage 1b and it worked, so the old behaviour is the specification here.
  // The mode is already set in place above and landed in the store by
  // syncSessionMetadata(); the next turn builds its prompt from the record.
  toast(s.mode === 'agent' ? 'Agent mode: tools on'
                           : 'Chat mode: pure conversation');
});

// At or under the 760px breakpoint the sidebar is an off-canvas drawer laid over the
// transcript (style.css: `.sidebar.open`, plus the scrim at `.app:has(.sidebar.open)::after`).
// The scrim has been painted for ages and nothing ever listened for a tap on it, which is
// how a panel that only its own button can close ships green. Three ways out, all of them
// the ones a hand on a phone actually reaches for: tap away, Escape, and picking a session.
function drawerOpen() {
  return innerWidth <= 760 && $('#sidebar').classList.contains('open');
}

function closeDrawer() {
  if (drawerOpen()) $('#sidebar').classList.remove('open');
}

$('#sidebarToggle').addEventListener('click', () => {
  const sb = $('#sidebar');
  if (innerWidth <= 760) sb.classList.toggle('open');
  else sb.classList.toggle('hidden');
});

// Capture phase on purpose. The scrim is a `::after` on `.app`, so a tap on it arrives as a
// click on the shell; and a row that stops propagation still has to dismiss the drawer that
// is covering the transcript the person just chose.
document.addEventListener('click', (e) => {
  if (!drawerOpen()) return;
  if (e.target.closest('#sidebar') || e.target.closest('#sidebarToggle')) return;
  closeDrawer();
}, true);

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDrawer();
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
  // A new plan starts at decomposing and nothing else does, so this is where the
  // previous draft is dropped. Left in place, the approval card of a later plan
  // would show the text of the one before it.
  if (m.phase === 'decomposing') planText = '';
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
    <span class="ps-detail">answer to shape the plan, or skip</span></div><div class="pq-list">`;
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
    renderPlanStatus({ phase: 'synthesizing', detail: 'answers received. Writing the plan…', explorers: [] });
  });
  $('#pqSkip').addEventListener('click', () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_answers', answers: formatAnswers(true) }));
    renderPlanStatus({ phase: 'synthesizing', detail: 'questions skipped. Writing the plan…', explorers: [] });
  });
}

function planBody(text) {
  // The draft is the thing being approved, so it is the thing the card has to
  // show: plan_ready is the only event that carries the text, and a card with
  // three buttons and no plan asks for approval of nothing. It comes back as
  // markdown from the synthesise pass, so it goes through the same renderer the
  // transcript uses. The strip is short and a real plan is not, so the body
  // scrolls instead of pushing the composer off the bottom of the screen.
  if (!text) return '';
  // .msg-content is not decoration: every markdown rule in the stylesheet is
  // scoped to it, so a plan drawn without that class gets browser defaults —
  // full-size headings and unstyled code. The plan is the agent's own prose
  // about the codebase, so it is rendered the way the agent's prose is rendered.
  return `<div class="msg-content pr-body">${md(text)}</div>`;
}

function renderPlanReady(text) {
  planStrip.hidden = false;
  planText = text || '';
  planStrip.innerHTML = `
    <div class="ps-head"><span class="ps-icon"></span><span class="ps-phase">Plan draft ready</span>
      <span class="ps-detail">approve to save it under ~/.tacit/plans/ and write it into the project</span></div>
    ${planBody(planText)}
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

function renderPlanApproved(info) {
  // Drawn from two sources that must look the same: the plan_approved event and
  // the snapshot a reload gets from hello. Both carry plan_file, the file under
  // ~/.tacit/plans. There is one copy now, not two: the approval used to also
  // write the project's PLAN.md and to refuse to overwrite a PLAN.md the person
  // had written themselves, which is why this line had to name two files and say
  // which one did not happen. It names the one file that always lands, and the
  // server's own sentence — what Implement will do with it — rides in the tooltip.
  planStrip.hidden = false;
  planStrip.innerHTML = `
    <div class="ps-head"><span class="ps-icon ok"></span>
      <span class="ps-phase">Plan approved & saved</span>
      <span class="ps-detail" title="${esc(info.message || '')}">${esc(info.plan_file || '')}</span></div>
    ${planBody(planText || info.text || '')}
    <div class="pr-actions">
      <button class="ho-btn primary" id="plImplement">Implement now</button>
      <button class="ho-btn" id="plLater">Later (menu → Implement)</button>
    </div>`;
  $('#plImplement').addEventListener('click', () => {
    // Stage 1b: the server creates the fresh session and returns its
    // id; the rpc_response handler adopts it and opens its socket.
    // No streaming bubble here — this session's turn is over.
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'plan_implement' }));
    hidePlanStrip();
  });
  $('#plLater').addEventListener('click', hidePlanStrip);
}

function restorePlanStrip(p) {
  // hello carries the plan the session is actually on. The strip is drawn from
  // events, and events that streamed past are gone for good, so without this a
  // reload or a switch-back showed an empty strip over a plan that was still
  // running — or still waiting for answers — on the server. The snapshot is the
  // same state those events drew, so each phase maps back onto its renderer.
  // Null means no plan, which must clear a strip left from the session this
  // socket replaced.
  if (!p || p.cancelled) { planLive = false; hidePlanStrip(); return; }
  // The phase is not the whole story, so the data decides. A finished plan is
  // still phase 'synthesizing' — plan_ready carries the text and never moves the
  // phase — and an approved one is indistinguishable from it by phase alone;
  // saved and text say what actually exists. `running` is the server's answer to
  // a question the client cannot ask: a snapshot whose thread died with the last
  // server must not come back as a live strip holding the composer shut.
  planLive = !!p.running;
  planText = p.text || '';
  if (p.saved) return renderPlanApproved(p);
  if (p.text) return renderPlanReady(p.text);
  if (p.phase === 'awaiting_answers' && (p.questions || []).length) {
    return renderPlanQuestions(p.questions);
  }
  if (planLive) {
    renderPlanStatus({ phase: p.phase || '', detail: p.detail || '',
                       explorers: p.explorers || [] });
    return;
  }
  hidePlanStrip();
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
    const d = m.data || {};
    // Say what the fold did, and which model wrote the note: TACIT_COMPACT_MODEL
    // can nominate another one and has no Settings surface, so this is the only
    // place a person can see it.
    toast(m.ok
      ? `Context compacted: ${d.compacted} messages, ~${fmtTok(d.saved_tokens || 0)} tokens freed, note by ${d.model || 'the session model'}`
      : ('Compact failed: ' + (m.error || '')), !m.ok);
  } else if (m.command === 'export_html') {
    if (m.ok && m.data && m.data.path) toast('Exported → ' + m.data.path);
    else toast('Export failed: ' + (m.error || ''), true);
  } else if (m.command === 'get_state') {
    if (m.ok && m.data) renderTokMeter();
  } else if (m.command === 'bash') {
    if (m.ok && m.data) toast(`command finished (exit ${m.data.code ?? 0})`);
    else toast('command failed: ' + (m.error || ''), true);
  } else if (m.command === 'fork' || m.command === 'clone') {
    if (m.ok && m.data && m.data.sid) adoptSession(m.data.sid, m.command === 'fork' ? 'Forked. Continuing from the copy' : 'Cloned');
    else toast((m.command === 'fork' ? 'Fork' : 'Clone') + ' failed: ' + (m.error || ''), true);
  } else if (m.command === 'plan_implement') {
    if (m.ok && m.data && m.data.sid) {
      adoptSession(m.data.sid, 'Implementing in a fresh session');
    } else {
      toast('Implement failed: ' + (m.error || ''), true);
    }
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
    termPrint('Tacit terminal. Commands run in the workspace', 'term-note');
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
      body: JSON.stringify({ command: cmd, project: (s && s.workdir) || '',
                             session: (s && s.id) || '' }),
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

// Boot: the socket goes up FIRST, then the data arrives.
// `sessions` and `activeId` are restored from localStorage synchronously at the
// top of this module, so there is already a valid session to attach to before a
// single byte has been fetched. Booting the other way round - await the registry,
// then connect - made "connecting..." last exactly as long as that request, and
// the registry is a read of every session file on disk. Ordering it the other way
// costs nothing: `connectSession` reuses a socket that is already open, and a
// session the server does not know answers 4404 and is adopted, which is the
// path that already exists for a relocated store.
(async function boot() {
  const bootId = activeId;
  if (bootId) connect();
  await initSessions();
  // The registry may name a different active session, or supply the first one on
  // a browser with no stored state. Only then is a second connect needed; calling
  // it unconditionally would close and reopen a socket still mid-handshake.
  if (activeId !== bootId) connect();
  await loadInfo();
  await loadSoundPref();
  renderModelPickers();
  renderThinkSelect();
  renderModelChip();
  startStatesPoll();          // the left-bar activity indicators
  inputEl.focus();
})();

// Public surface for sibling panels (harness.js, git.js). Both load after this file.
pickTagline();

window.Tacit = Object.assign(window.Tacit || {}, {
  getSid: () => activeId,
  getWorkdir: () => { const s = cur(); return (s && s.workdir) || ''; },
  refreshInfo: () => loadInfo(),
  refreshSoundPref: () => loadSoundPref(),
  // Settings > Tools auditions the four chimes through this, past both switches
  previewChimes: () => previewChimes(),
  // Settings > Appearance sets transcript density through this
  setDensity: (id) => applyDensity(id),
  pickDir: ({ key = 'workspace', ...opts } = {}) => openFsPicker(key, opts),
  // the assistant panel shares this one socket rather than opening its own
  sendMessage: (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); },
  toast,
  // and this renderer: the assistant's answers are markdown from the same source
  md: (src) => md(src),
});
})();
