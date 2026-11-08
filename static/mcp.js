/*
 * The MCP panel: which servers are installed, and one switch each.
 *
 * Servers only, on purpose. A server can carry forty-three tools, and a 300-pixel
 * strip that listed them would be a second Settings > MCP with worse typography;
 * what a person comes here for is "is this one on, and what is it costing me
 * right now", which is one boolean and one number per server. The tools, their
 * pins and their allow-lists stay in Settings > MCP, which the footer opens.
 *
 * The switch is the thing the backend now actually obeys. Switching a server off
 * used to write a preference while its pinned schemas kept going into every
 * request, its tools stayed in the catalogue the model searches, and its
 * subprocess stayed up. See mcp_registry.update_server and active_tools.
 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  let data = { servers: [], settings: {}, injection: {} };
  let loading = false;

  const toast = (t, e) => window.Tacit && window.Tacit.toast && window.Tacit.toast(t, e);

  async function api(path, opts) {
    try {
      const r = await fetch(path, Object.assign(
        { headers: { 'Content-Type': 'application/json' } }, opts || {}));
      return await r.json();
    } catch (e) { return { ok: false, error: 'could not reach Tacit' }; }
  }

  // ~3.8k, not 3812: these are estimates of a token count, and four significant
  // digits on an estimate reads like a measurement.
  function tok(n) {
    n = Math.max(0, Math.round(n || 0));
    return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n);
  }

  function injectedFor(id) {
    const keys = (data.injection && data.injection.active) || [];
    return keys.filter((k) => k.slice(0, k.indexOf(':')) === id).length;
  }

  function row(s) {
    const injected = injectedFor(s.id);
    const pinned = (s.pinned || []).length;
    // A server that is off but still injecting is the bug this panel was written
    // over. It cannot happen now, and if it ever does the row says it rather than
    // showing an off switch and a quiet cost.
    const leak = !s.enabled && injected > 0;
    return `
    <div class="mcp-row${s.enabled ? '' : ' off'}" data-id="${esc(s.id)}">
      <span class="mcp-dot ${esc(s.state)}" title="${esc(s.state)}${s.error ? ': ' + esc(s.error) : ''}"></span>
      <span class="mcp-name">${esc(s.name || s.id)}</span>
      <span class="mcp-meta">${s.tool_count} tool${s.tool_count === 1 ? '' : 's'} · ~${tok(s.tools_tokens)}${
        s.enabled && injected ? ' · <b>' + injected + ' in the prompt</b>' : ''}${
        !s.enabled && s.state !== 'stopped' ? ' · ' + esc(s.state) : ''}${
        leak ? ' · <b>still injected</b>' : ''}</span>
      ${s.error ? `<span class="mcp-err" title="${esc(s.error)}">!</span>` : ''}
      <button class="tgl${s.enabled ? ' on' : ''}" data-act="toggle" role="switch"
        aria-checked="${!!s.enabled}" title="${s.enabled ? 'Switch this server off' : 'Switch it on'}"
        ><span class="tgl-knob"></span></button>
      ${pinned ? `<button class="ho-btn small" data-act="unpin" title="${pinned} pinned schema(s) are injected on every turn without the agent asking">unpin ${pinned}</button>` : ''}
    </div>`;
  }

  function render() {
    const box = $('#mcpList');
    if (!box) return;
    const servers = data.servers || [];
    const inj = data.injection || {};
    const on = servers.filter((s) => s.enabled).length;
    const sub = $('#mcpSub');
    if (sub) sub.textContent = servers.length
      ? `${on} of ${servers.length} on · ~${tok(inj.injected_tokens)} in the prompt`
      : 'no servers yet';

    if (!servers.length) {
      box.innerHTML = '<div class="as-empty">No MCP server is installed. Add one in '
        + '<b>Settings &#9656; MCP</b> and it appears here with a switch.<br><br>'
        + '<span class="as-note">A connected server\'s tools are not injected until the '
        + 'agent searches for one and activates it, or you pin it.</span></div>';
      return;
    }
    box.innerHTML = servers.map(row).join('')
      + (inj.direct_mode
        ? '<div class="as-note mcp-foot">Direct mode is on: every enabled server\'s '
          + 'tools go into every request. Turn it off in <b>Settings &#9656; MCP</b> to '
          + 'let the agent reach for them only when it needs one.</div>'
        : '<div class="as-note mcp-foot">Lazy loading is on: only the '
          + tok(inj.saved_tokens) + ' tokens the agent has not reached for stay out of '
          + 'the prompt. Pinned tools are the exception, and they are always in.</div>');
  }

  async function load() {
    if (loading) return;
    loading = true;
    const box = $('#mcpList');
    if (box && !data.servers.length) box.innerHTML = '<div class="as-empty">Loading…</div>';
    const res = await api('/api/mcp/servers');
    loading = false;
    if (res && res.ok) data = { servers: res.servers || [], settings: res.settings || {},
                                injection: res.injection || {} };
    else if (res && res.error && box) box.innerHTML = `<div class="as-empty">${esc(res.error)}</div>`;
    render();
  }

  async function toggle(id, enable) {
    const res = await api(`/api/mcp/servers/${encodeURIComponent(id)}`, {
      method: 'PATCH', body: JSON.stringify({ enabled: enable }),
    });
    if (!res || !res.ok) { toast('MCP: ' + ((res && res.error) || 'failed'), true); return; }
    // The server row comes back with the state the switch produced, so a start that
    // failed is reported here rather than discovered three turns later by the model.
    if (enable && res.server && res.server.state !== 'running' && res.server.error) {
      toast(`${id} is on but did not start: ${res.server.error}`, true);
    } else {
      toast(`${id} ${enable ? 'on' : 'off'}`);
    }
    await load();
  }

  async function unpin(id) {
    const res = await api(`/api/mcp/servers/${encodeURIComponent(id)}/unpin_all`,
      { method: 'POST', body: '{}' });
    if (!res || !res.ok) { toast('MCP: ' + ((res && res.error) || 'failed'), true); return; }
    toast(`${id}: ${res.unpinned} unpinned, ~${tok(res.tokens_freed)} off every prompt`);
    await load();
  }

  // One listener for the whole list, not one per row: the panel re-renders on every
  // toggle, and a listener built into a row would be rebuilt each time as well.
  function onClick(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const row = btn.closest('.mcp-row');
    if (!row) return;
    const id = row.dataset.id;
    // The row carries 'off' when the server is disabled, so the state the click
    // should produce is exactly that class's presence. Inverting it here made every
    // click re-assert the state the row was already in: a switch that never moved.
    if (btn.dataset.act === 'toggle') toggle(id, row.classList.contains('off'));
    else if (btn.dataset.act === 'unpin') unpin(id);
  }

  function setOpen(on) {
    const panel = $('#mcpPanel');
    const btn = $('#mcpToggle');
    if (!panel) return;
    panel.classList.toggle('open', on);
    if (btn) btn.classList.toggle('on', on);
    try { localStorage.setItem('tacit.mcpOpen', on ? '1' : '0'); } catch (e) { /* ignore */ }
    if (on) load();
  }

  function init() {
    const box = $('#mcpList');
    if (box) box.addEventListener('click', onClick);
    const t = $('#mcpToggle');
    if (t) t.addEventListener('click', () => setOpen(!$('#mcpPanel').classList.contains('open')));
    const c = $('#mcpClose');
    if (c) c.addEventListener('click', () => setOpen(false));
    const s = $('#mcpSettingsBtn');
    if (s) s.addEventListener('click', () => {
      if (window.TacitPlatform && window.TacitPlatform.openTab) window.TacitPlatform.openTab('mcp');
    });
    let want = false;
    try { want = localStorage.getItem('tacit.mcpOpen') === '1'; } catch (e) { /* ignore */ }
    if (want) setOpen(true);
    // The panel is global state, not this session's, and an agent can start a server
    // or let one die mid-turn. Refresh when the tab comes back rather than polling.
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && $('#mcpPanel') && $('#mcpPanel').classList.contains('open')) load();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else { init(); }

  window.TacitMcp = { load, open: () => setOpen(true) };
})();
