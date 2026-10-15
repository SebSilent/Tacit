/* Tacit's wire contract. Both frontends load this file, and neither one re-implements it.
   index.html: transport.js, then app.js.  /mobile: transport.js, then mobile.js.

   This exists because of PLAN item 8. A second frontend that re-derives the socket URL,
   the query parameters and the event names drifts from the first within a week, and the
   drift is invisible until a turn half-works on one device. So everything that *names the
   wire* lives here: the URL builder, the event types the server publishes, the command
   types the client sends, the reconnect backoff, and the REST helper. What stays per-page
   is chrome - how an event is drawn.

   tests/test_mobile_protocol.py reads the backend and fails if the lists below and the
   server's own literals disagree. That is the pin neither frontend can break by accident.
*/
(function () {
  'use strict';

  // ---- what the server publishes, measured against the backend ----
  // Two names the desktop client branches on are deliberately absent. `trace` is the
  // turn worker's own event (`{"type":"trace","steps":...}` written to the worker's
  // stdout); it is folded into `reason`/`text` before anything reaches a socket, so a
  // page that listened for it would listen forever. `proc_exit` is a dead branch in
  // app.js: nothing in backend/ emits that type at all.
  const EVENTS = [
    'hello', 'replay', 'session_ready', 'user_message', 'text', 'reason', 'done',
    'tool_start', 'tool_end', 'tool_progress', 'usage', 'state_delta',
    'rpc_response', 'error', 'notify', 'truncated', 'truncate_failed',
    'plan_approved', 'image_url', 'autonomy_cycle', 'delegation_activity'
  ];

  // ---- what a client may send on the socket (chat.py's `kind` dispatch) ----
  // `image` is not here on purpose: it is the shape of an *attachment* inside a
  // prompt, not a command. A page that sent {type:'image'} would hit the dispatch's
  // silent tail and get nothing back.
  const COMMANDS = [
    'prompt', 'steer', 'abort', 'compact',
    'set_model', 'set_thinking_level', 'set_session_name',
    'get_state', 'get_session_stats', 'truncate_from',
    'fork', 'clone_session',
    'plan_answers', 'plan_approve', 'plan_abort', 'plan_implement'
  ];

  // ---- the socket URL, moved verbatim out of app.js so there is exactly one of it ----
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

  // ---- a session id, on every origin the app is served from ----
  // crypto.randomUUID is undefined on plain-HTTP LAN origins (insecure context),
  // and /mobile is exactly such an origin. The fallback is the same shape the
  // browser produces, and it is only a client-side id: the server owns the store.
  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID()
      : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
          const r = Math.random() * 16 | 0;
          return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
  }

  // ---- one reconnect loop, not two ----
  // Backoff is capped and finite: a server that is down should cost a handful of
  // attempts, not a hammering loop, and a socket the page closed deliberately must
  // never come back (that is how a background tab re-materialises sessions).
  function openSocket(s, handlers) {
    let ws = null, tries = 0, timer = null, gone = false;

    function schedule() {
      if (gone) return;
      tries += 1;
      const wait = Math.min(800 * Math.pow(2, Math.min(tries - 1, 5)), 15000);
      timer = setTimeout(connect, wait);
      if (handlers.onreconnecting) handlers.onreconnecting(tries, wait);
    }

    function connect() {
      if (gone) return;
      clearTimeout(timer);
      ws = new WebSocket(wsUrl(s));
      ws.onopen = () => { tries = 0; if (handlers.onopen) handlers.onopen(); };
      ws.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }   // a non-JSON frame is not an event
        if (handlers.onmessage) handlers.onmessage(m);
      };
      ws.onclose = (ev) => {
        if (gone) return;
        if (handlers.onclose) handlers.onclose(ev);
        // 4404 is "this server has never heard of this id". Reconnecting the same URL
        // hits the same refusal forever, so the page decides (a New-session id is
        // legitimately unknown until the socket itself creates it).
        if (ev && ev.code === 4404) { if (handlers.onunknown) handlers.onunknown(); return; }
        schedule();
      };
      ws.onerror = () => { /* onclose follows, and that is where the retry lives */ };
    }

    connect();

    return {
      // A send that cannot send says so. Callers must not assume the wire took it.
      send(obj) {
        if (ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); return true; }
        return false;
      },
      // Deliberate teardown: no reconnect, no ghost socket left drawing a transcript
      // this view no longer shows.
      close() { gone = true; clearTimeout(timer); if (ws) { ws.onclose = null; try { ws.close(); } catch (e) {} } },
      socket() { return ws; },
      open() { return !!(ws && ws.readyState === 1); }
    };
  }

  // ---- REST, same-origin on both pages ----
  async function getJSON(path) {
    try {
      const r = await fetch(path, { headers: { 'Cache-Control': 'no-cache' } });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  async function postJSON(path, body) {
    try {
      const r = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
      });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  window.TacitTransport = {
    EVENTS, COMMANDS, wsUrl, uuid, openSocket, getJSON, postJSON,
    // The one place that knows a command name, so a page cannot invent one.
    isCommand: (kind) => COMMANDS.indexOf(kind) !== -1
  };
})();
