/* Tacit platform panels: MCP, Memory Vault, Plugins, Tokens.
 *
 * These extend the existing Settings overlay rather than adding another
 * framework: harness.js already toggles every `.ho-panel` by `data-tab`, so we
 * only contribute tabs, panels and renderers. No build step, no dependencies.
 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    try { return await r.json(); } catch (e) { return { ok: false, error: 'bad response' }; }
  }
  const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });
  const put = (p, body) => api(p, { method: 'PUT', body: JSON.stringify(body || {}) });
  const patch = (p, body) => api(p, { method: 'PATCH', body: JSON.stringify(body || {}) });

  function note(text, isErr) {
    const el = $('#hoNote');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('err', !!isErr);
  }

  // Every action handler in this file used to call toast() with no such
  // function in scope: the POST went through, then the handler threw, the
  // panel never re-rendered, and no feedback appeared — which read as
  // "the buttons do nothing". app.js's toast lives inside its closure; this
  // reaches the same one over the bridge, and falls back to the note line.
  function toast(text, isErr) {
    if (window.Tacit && window.Tacit.toast) window.Tacit.toast(text, isErr);
    else note(text, isErr);
  }

  // ── tabs + panels ──────────────────────────────────────────────────────
  // Core tabs come from index.html. These are the ones this file adds. Anything
  // that is not plain Tacit is marked advanced and stays hidden until asked for,
  // so the settings panel opens with six tabs rather than ten.
  const TABS = [
    { id: 'dashboard', label: 'Profile' },
    { id: 'capabilities', label: 'Capabilities', advanced: true },
    { id: 'learning', label: 'Learning', advanced: true },
    { id: 'mcp', label: 'MCP', advanced: true },
    { id: 'memory', label: 'Memory', advanced: true },
    { id: 'plugins', label: 'Plugins', advanced: true },
  ];

  const ADV_KEY = 'tacit.settings.advanced';

  function advancedOn() {
    try { return localStorage.getItem(ADV_KEY) === '1'; } catch (e) { return false; }
  }

  function applyAdvanced() {
    const on = advancedOn();
    document.querySelectorAll('.ho-tab').forEach((btn) => {
      const spec = TABS.find((t) => t.id === btn.dataset.tab);
      if (spec && spec.advanced) btn.hidden = !on;
    });
    const box = $('#advWrap');
    if (box) box.classList.toggle('on', on);
    // if the tab you were on just went away, land somewhere that still exists
    const active = document.querySelector('.ho-tab.active');
    if (active && active.hidden) {
      const first = [...document.querySelectorAll('.ho-tab')].find((b) => !b.hidden);
      if (first) first.click();
    }
  }

  function install() {
    const tabs = $('#hoTabs');
    // By id, not by class: the snapshots overlay also has a .ho-body, and it
    // sits earlier in the document, so a class query landed every dynamic
    // tab panel inside that hidden overlay — the tabs clicked, the renderers
    // ran, and nothing appeared from Profile to Plugins.
    const body = $('#hoBody');
    if (!tabs || !body) return;
    TABS.forEach((t) => {
      const btn = document.createElement('button');
      btn.className = 'ho-tab';
      btn.dataset.tab = t.id;
      btn.textContent = t.label;
      tabs.appendChild(btn);
      const panel = document.createElement('section');
      panel.className = 'ho-panel';
      panel.id = 'panel-' + t.id;
      panel.hidden = true;
      body.appendChild(panel);
    });
    tabs.addEventListener('click', (e) => {
      const btn = e.target.closest('.ho-tab');
      if (!btn || !TABS.some((t) => t.id === btn.dataset.tab)) return;
      render(btn.dataset.tab);
    });

    const adv = document.createElement('label');
    adv.className = 'chk adv-switch';
    adv.id = 'advWrap';
    adv.title = 'Show the optional systems: capabilities, MCP, memory and plugins';
    adv.innerHTML = '<input type="checkbox" id="advToggle"> advanced';
    tabs.appendChild(adv);
    const cb = $('#advToggle');
    cb.checked = advancedOn();
    cb.addEventListener('change', () => {
      try { localStorage.setItem(ADV_KEY, cb.checked ? '1' : '0'); } catch (e) { /* ignore */ }
      applyAdvanced();
    });
    applyAdvanced();

    // the per-session snapshots overlay, reached from the top bar
    const btn = $('#snapBtn');
    if (btn) btn.addEventListener('click', openSnapshots);
    const close = $('#snapClose');
    if (close) close.addEventListener('click', closeSnapshots);
    const ov = $('#snapOverlay');
    if (ov) ov.addEventListener('click', (e) => { if (e.target === ov) closeSnapshots(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && ov && !ov.hidden) closeSnapshots();
    });
  }

  function render(tab) {
    if (tab === 'dashboard') renderDashboard();
    else if (tab === 'capabilities') renderCapabilities();
    else if (tab === 'learning') renderLearning();
    else if (tab === 'mcp') renderMcp();
    else if (tab === 'memory') renderMemory();
    else if (tab === 'plugins') renderPlugins();
  }

  // ── Tokens dashboard ───────────────────────────────────────────────────
  // The Profile tab is the selector and nothing else. The token tables that
  // used to sit under it were removed at the operator's request: the numbers
  // live in the backend's own accounting, and the tab's job is to choose a
  // bundle, not to report on it.
  async function renderDashboard() {
    const panel = $('#panel-dashboard');
    panel.innerHTML = '<div class="ho-loading">Loading profiles…</div>';
    const pf = await api('/api/profiles');
    const profileRow = (p) => `
      <div class="prof-row ${p.active ? 'on' : ''}" data-name="${esc(p.name)}">
        <span class="prof-name">${esc(p.label)}</span>
        <span class="badge">${esc(p.cost_display)} tokens</span>
        ${p.active ? '<span class="badge on">active</span>' : `<button class="ho-btn small" data-act="apply">Apply</button>`}
        <span class="prof-desc">${esc(p.description)}</span>
        ${p.builtin ? '' : '<button class="ho-btn small danger" data-act="del">×</button>'}
      </div>`;
    const profiles = (pf.profiles || []).map(profileRow).join('');

    panel.innerHTML = `
      <div class="prof-list">${profiles}</div>
      <div class="mcp-add">
        <input class="mcp-in" id="profName" placeholder="save current setup as…" spellcheck="false">
        <button class="ho-btn small" id="profSave">Save profile</button>
        <span class="ho-sub sm">Switching a profile applies immediately.</span>
      </div>`;

    panel.querySelectorAll('.prof-row').forEach((row) => {
      const name = row.dataset.name;
      const apply = row.querySelector('[data-act="apply"]');
      if (apply) apply.addEventListener('click', async () => {
        note(`applying ${name}…`);
        const r = await post(`/api/profiles/${encodeURIComponent(name)}/apply`);
        note(r.ok ? `${name} applied (${esc(fmt((r.cost || {}).total))} tokens of extras)`
                  : (r.error || 'failed'), !r.ok);
        renderDashboard();
      });
      const del = row.querySelector('[data-act="del"]');
      if (del) del.addEventListener('click', async () => {
        if (!confirm(`Delete profile "${name}"?`)) return;
        await api('/api/profiles/' + encodeURIComponent(name), { method: 'DELETE' });
        renderDashboard();
      });
    });
    $('#profSave').addEventListener('click', async () => {
      const name = $('#profName').value.trim();
      if (!name) { note('give the profile a name', true); return; }
      const r = await post('/api/profiles', { name });
      note(r.ok ? `saved profile "${r.name}"` : (r.error || 'failed'), !r.ok);
      if (r.ok) renderDashboard();
    });
  }

  // ── Capabilities ───────────────────────────────────────────────────────
  // Sandbox, memory and learning in one place, each option stating whether it
  // is usable and why not when it is not. A heavy mode that is on is marked in
  // the header so it cannot be forgotten.
  async function renderCapabilities() {
    const panel = $('#panel-capabilities');
    panel.innerHTML = '<div class="ho-loading">Loading capabilities…</div>';
    const c = await api('/api/capabilities');
    const a = await api('/api/audit?limit=40');
    const gw = await api('/api/gateways');
    const lg = await api('/api/learning');
    const an = await api('/api/analyzer').catch(() => ({ enabled: false }));
    const gu = await api('/api/guidance').catch(() => ({ enabled: true, prompt_chars: 979 }));
    const dp = await api('/api/deps');
    const mg = await api('/api/migrate/hint');
    const sm = c.sandbox_mode || {};
    const cfg = (c.config || {});
    const rep = c.memory_report || {};

    const optionFor = (row, chosen) => {
      const usable = row.available && row.implemented;
      const why = !row.available ? (row.reason || 'unavailable')
        : (!row.implemented ? 'no adapter yet' : '');
      return `<option value="${esc(row.id)}"${row.id === chosen ? ' selected' : ''}` +
        `${usable ? '' : ' disabled'}>${esc(row.name)}${usable ? '' : ' — ' + esc(why)}</option>`;
    };
    const kind = (k) => (c.providers || []).filter((p) => p.kind === k);

    const sandboxOn = sm.backend && sm.backend !== 'none';
    const memoryOn = (c.memory || {}).id && c.memory.id !== 'off';
    const flags = [];
    if (sandboxOn) flags.push('sandbox ' + sm.backend);
    if (memoryOn) flags.push('memory ' + c.memory.id);
    if ((c.learning || {}).id && c.learning.id !== 'learn-off') flags.push('learning ' + c.learning.id);

    const en = sm.will_enforce || {};
    const yn = (v) => (v ? 'yes' : 'no');

    panel.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">Optional systems. Nothing here is on unless you turn it on.</span>
        ${flags.length ? `<span class="badge on">${esc(flags.join(' · '))}</span>` : '<span class="badge">all optional systems off</span>'}
      </div>

      <div class="ho-section">Sandbox <span class="ho-sub sm">how commands are isolated</span></div>
      <div class="mcp-add">
        <select class="git-select" id="capSandbox">${kind('sandbox').map((r) => optionFor(r, sm.backend)).join('')}</select>
        <label class="chk"><input type="checkbox" id="capNet" ${cfg.sandbox && cfg.sandbox.network ? 'checked' : ''}> allow network</label>
        <input class="mcp-in sm" id="capTimeout" type="number" min="1" max="600" value="${(cfg.sandbox && cfg.sandbox.timeout) || 180}">
        <span class="ho-sub sm">seconds</span>
        <button class="ho-btn small" id="capSandboxSave">Save</button>
      </div>
      <div class="tok-table">
        <div class="tok-row"><span class="tok-label">Will enforce</span><span class="tok-value">timeout ${yn(en.timeout)} · cpu ${yn(en.cpu_limit)} · memory ${yn(en.memory_limit)} · network ${yn(en.network)} · read-only ${yn(en.readonly_project)}</span></div>
        <div class="tok-row"><span class="tok-label">Platform</span><span class="tok-value">${esc(sm.platform || '')}</span><span class="tok-hint">what this OS can do at all</span></div>
      </div>

      <div class="ho-section">Memory <span class="ho-sub sm">what the agent may remember</span></div>
      <div class="mcp-add">
        <select class="git-select" id="capMemory">${kind('memory').map((r) => optionFor(r, (c.memory || {}).id)).join('')}</select>
        <input class="mcp-in sm" id="capBudget" type="number" min="0" max="2000" value="${rep.budget || 0}">
        <span class="ho-sub sm">token budget</span>
        <button class="ho-btn small" id="capMemorySave">Save</button>
      </div>
      <div class="tok-table">
        <div class="tok-row"><span class="tok-label">Injected now</span><span class="tok-value">${esc(rep.display || '0')}</span><span class="tok-hint">${rep.used || 0} of ${rep.budget || 0} tokens, ${rep.held_back || 0} held back</span></div>
      </div>
      ${(rep.injected || []).length ? `<div class="mem-list">${rep.injected.map((i) =>
        `<div class="mem-row"><span class="mem-type badge">${esc(i.type)}</span>` +
        `<span class="mem-text">${esc((i.content || '').slice(0, 160))}</span>` +
        `<span class="badge">${esc(i.why)}</span>` +
        `<span class="badge">${i.tokens} tok</span>` +
        `<span class="badge">${esc(i.source_session || i.source || '')}</span></div>`).join('')}</div>`
        : '<div class="ho-sub sm">Nothing is being injected.</div>'}

      <div class="mcp-add">
        <label class="ho-sub sm"><input type="checkbox" id="capGuidance"${gu.enabled ? ' checked' : ''}> Guide the agent mid-turn</label>
      </div>
      <div class="ho-sub sm">Adds a short note on the step that needs one: after a tool fails, when the same call comes again, as the step budget runs out, and when a step plans instead of acting. Standing prompt ${gu.prompt_chars || 979} chars, unchanged by this.</div>

      <div class="ho-section">Gateways <span class="ho-sub sm">hand a task to another program</span></div>
      <div class="mcp-add">
        <select class="git-select" id="capGateway">
          ${(gw.gateways || []).map((g) => `<option value="${esc(g.id)}"${g.id === gw.selected ? ' selected' : ''}${g.available ? '' : ' disabled'}>${esc(g.name)}${g.available ? '' : ' — unavailable'}</option>`).join('')}
        </select>
        <button class="ho-btn small primary" id="capGatewaySelect">Select</button>
      </div>
      <div class="tok-table">
        ${(gw.gateways || []).filter((g) => g.command).map((g) =>
          `<div class="tok-row"><span class="tok-label">${esc(g.name)}</span><span class="tok-value">${esc(g.command)}</span></div>`).join('')
          || '<div class="tok-row"><span class="tok-label">No command configured</span></div>'}
      </div>
      <div id="capGwList">${(gw.gateways || []).filter((g) => g.status === 'optional').map((g) =>
        `<div class="prof-row" data-gw="${esc(g.id)}"><span class="prof-name">${esc(g.name)}</span>` +
        `<span class="prof-desc">${esc(g.command)}</span>` +
        `<button class="ho-btn small danger" data-act="gwdel">×</button></div>`).join('')}</div>
      <div class="mcp-add">
        <input class="mcp-in" id="capGwId" placeholder="id" spellcheck="false">
        <input class="mcp-in" id="capGwCmd" placeholder="command" spellcheck="false">
        <input class="mcp-in" id="capGwArgs" placeholder="args, use {task}" spellcheck="false">
        <button class="ho-btn small" id="capGwAdd">Add gateway</button>
      </div>
      <div class="mcp-add"${gw.selected === 'none' ? ' hidden' : ''} id="capGwRunRow">
        <input class="mcp-in" id="capGwTask" placeholder="task to hand over" spellcheck="false">
        <button class="ho-btn small" id="capGwRun">Run</button>
      </div>
      <div id="capGwOut"></div>

      <div class="ho-section">Optional dependencies <span class="ho-sub sm">checked on this machine, now</span></div>
      <div class="tok-table">
        ${(dp.groups || []).map((g) => {
          // A group can fail its probe with nothing missing from the binary
          // list — node present, playwright never installed — so "needs" with
          // an empty list and a missing[0] that is undefined both have to go.
          const need = (g.missing || []).map((m) => m.binary).join(', ');
          const hint = g.satisfied ? (g.probe || g.note)
            : (((g.missing || [])[0] || {}).install || g.probe || '');
          return `<div class="tok-row">
          <span class="tok-label">${esc(g.label)}</span>
          <span class="tok-value">${g.satisfied ? 'ready' : (need ? 'needs ' + esc(need) : 'not ready')}</span>
          <span class="tok-hint">${esc(hint)}</span>
        </div>`;
        }).join('')}
      </div>

      <div class="ho-section">Import from another tool <span class="ho-sub sm">one time, only if you ask</span></div>
      <div class="mcp-add">
        <input class="mcp-in" id="capMigPath" placeholder="path to a file or folder you exported" spellcheck="false">
        <button class="ho-btn small" id="capMigPreview">Preview</button>
      </div>
      <div class="ho-sub sm">${esc(mg.hint || '')}</div>
      <div id="capMigOut"></div>

      <div class="ho-section">Audit <span class="ho-count">${(a.entries || []).length} recent</span></div>
      <div class="mcp-audit">${(a.entries || []).map((e) =>
        `<div class="audit-row"><span class="audit-event">${esc(e.event)}</span>` +
        `<span class="audit-detail">${esc(e.backend || '')} ${esc(e.tool || '')} ${esc(e.status || '')} ${e.changed ? '· ' + e.changed + ' changed' : ''}</span></div>`).join('')
        || '<div class="ho-empty sm">Nothing recorded yet.</div>'}</div>`;

    const setSandbox = async () => {
      const r = await post('/api/capabilities/sandbox', {
        backend: $('#capSandbox').value, network: $('#capNet').checked,
        timeout: parseInt($('#capTimeout').value, 10) || 180,
      });
      if (!r.ok) { toast(r.error || 'could not save', true); return; }
      toast('sandbox set to ' + r.sandbox.id);
      renderCapabilities();
    };
    $('#capSandboxSave').addEventListener('click', setSandbox);

    $('#capMemorySave').addEventListener('click', async () => {
      const r = await post('/api/capabilities/memory', {
        mode: $('#capMemory').value, budget: parseInt($('#capBudget').value, 10) || 0,
      });
      if (!r.ok) { toast(r.error || 'could not save', true); return; }
      toast('memory set to ' + r.memory.id);
      renderCapabilities();
    });

    // ── per-turn guidance ──
    const guBox = $('#capGuidance');
    if (guBox) guBox.addEventListener('change', async () => {
      const r = await post('/api/guidance', { enabled: guBox.checked });
      if (!r.ok) { toast(r.error || 'could not save', true); return; }
      toast('mid-turn guidance ' + (r.enabled ? 'on' : 'off'));
    });

    // ── gateways ──
    const gwOut = () => $('#capGwOut');
    $('#capGatewaySelect').addEventListener('click', async () => {
      const id = $('#capGateway').value;
      const r = await post(`/api/gateways/${encodeURIComponent(id)}/select`);
      toast(r.ok ? (id === 'none' ? 'gateways off' : id + ' selected') : (r.error || 'failed'), !r.ok);
      if (r.ok) renderCapabilities();
    });
    $('#capGwAdd').addEventListener('click', async () => {
      const id = ($('#capGwId').value || '').trim();
      const command = ($('#capGwCmd').value || '').trim();
      const args = ($('#capGwArgs').value || '').trim();
      if (!id || !command) { toast('an id and a command are required', true); return; }
      const r = await post('/api/gateways',
        { id, command, args: args ? args.split(/\s+/) : [], enabled: true });
      if (!r.ok) { toast(r.error || 'could not add it', true); return; }
      toast('added ' + id + '. Select it before it can run.');
      renderCapabilities();
    });
    panel.querySelectorAll('[data-gw]').forEach((row) => {
      row.querySelector('[data-act="gwdel"]').addEventListener('click', async () => {
        if (!confirm('Remove gateway ' + row.dataset.gw + '?')) return;
        const r = await api('/api/gateways/' + encodeURIComponent(row.dataset.gw),
                            { method: 'DELETE' });
        toast(r.ok ? 'removed' : (r.error || 'could not remove it'), !r.ok);
        if (r.ok) renderCapabilities();
      });
    });
    const runBtn = $('#capGwRun');
    if (runBtn) runBtn.addEventListener('click', async () => {
      const task = ($('#capGwTask').value || '').trim();
      if (!task) { toast('a task is required', true); return; }
      gwOut().innerHTML = '<div class="ho-sub sm">running…</div>';
      const r = await post(`/api/gateways/${encodeURIComponent(gw.selected)}/run`, { task });
      gwOut().innerHTML = r.ok
        ? `<pre class="mem-preview">${esc(r.output || '(no output)')}</pre>`
        : `<div class="ho-err">${esc(r.error || 'the gateway did not answer')}</div>`;
    });

    // ── migration: nothing happens until you point and confirm ──
    const migOut = () => $('#capMigOut');
    $('#capMigPreview').addEventListener('click', async () => {
      const path = ($('#capMigPath').value || '').trim();
      if (!path) { toast('point at a file or folder', true); return; }
      migOut().innerHTML = '<div class="ho-sub sm">reading…</div>';
      const r = await post('/api/migrate/preview', { path });
      if (!r.ok) { migOut().innerHTML = `<div class="ho-err">${esc(r.error || 'nothing found')}</div>`; return; }
      migOut().innerHTML = `
        <div class="tok-table">
          <div class="tok-row"><span class="tok-label">Found</span><span class="tok-value">${r.count} item(s)</span><span class="tok-hint">${esc(r.display)} tokens</span></div>
          <div class="tok-row"><span class="tok-label">Refused</span><span class="tok-value">${r.refused_count}</span><span class="tok-hint">looked like credentials</span></div>
        </div>
        <div class="mcp-add">
          <button class="ho-btn small primary" id="capMigDo">Import these</button>
        </div>`;
      $('#capMigDo').addEventListener('click', async () => {
        const done = await post('/api/migrate/import', { path });
        migOut().innerHTML = done.ok
          ? `<div class="ho-sub sm">imported ${done.added_count}, skipped ${done.skipped_count}. ${esc(done.note || '')}</div>`
          : `<div class="ho-err">${esc(done.error || 'import failed')}</div>`;
      });
    });
  }

  // ── MCP ────────────────────────────────────────────────────────────────
  async function renderMcp() {
    const panel = $('#panel-mcp');
    panel.innerHTML = '<div class="ho-loading">Loading MCP…</div>';
    const d = await api('/api/mcp/servers');
    const tools = await api('/api/mcp/tools');
    const audit = await api('/api/mcp/audit?limit=40');
    const servers = d.servers || [];
    const inj = d.injection || {};

    const serverCard = (s) => `
      <div class="mcp-card" data-id="${esc(s.id)}">
        <div class="mcp-head">
          <span class="mcp-name">${esc(s.name)}</span>
          <span class="badge ${s.enabled ? 'on' : ''}">${s.enabled ? 'enabled' : 'disabled'}</span>
          <span class="badge state-${esc(s.state)}">${esc(s.state)}</span>
          <span class="badge">${s.tool_count} tools · ${esc(fmt(s.tools_tokens))} tok</span>
        </div>
        <div class="mcp-meta"><code>${esc(s.command)} ${esc((s.args || []).join(' '))}</code></div>
        ${s.error ? `<div class="ho-err">${esc(s.error)}</div>` : ''}
        <div class="mcp-actions">
          <button class="ho-btn small" data-act="toggle">${s.enabled ? 'Disable' : 'Enable'}</button>
          <button class="ho-btn small" data-act="start">Start</button>
          <button class="ho-btn small" data-act="stop">Stop</button>
          <button class="ho-btn small" data-act="discover">Re-scan</button>
          <button class="ho-btn small danger" data-act="remove">Remove</button>
        </div>
      </div>`;

    const toolRow = (t) => `
      <div class="mcp-tool" data-key="${esc(t.key)}">
        <label class="chk"><input type="checkbox" class="pin" ${t.pinned ? 'checked' : ''}></label>
        <span class="mcp-tool-name">${esc(t.name)}</span>
        <span class="badge">${esc(t.server_id)}</span>
        <span class="badge danger-${esc(t.danger)}">${esc(t.danger)}</span>
        <span class="badge">${esc(fmt(t.tokens))} tok</span>
        <span class="mcp-tool-desc">${esc((t.description || '').slice(0, 140))}</span>
      </div>`;

    panel.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">Tools are discovered but stay out of the prompt until activated or pinned.</span>
        <label class="chk"><input type="checkbox" id="mcpDirect" ${inj.direct_mode ? 'checked' : ''}> Direct MCP mode</label>
      </div>
      <div class="tok-table">
        <div class="tok-row"><span class="tok-label">Discovered schemas</span><span class="tok-value">${esc(fmt(inj.discovered_tokens))}</span></div>
        <div class="tok-row"><span class="tok-label">Actually injected</span><span class="tok-value">${esc(fmt(inj.injected_tokens))}</span></div>
        <div class="tok-row"><span class="tok-label">Saved by lazy activation</span><span class="tok-value">${esc(fmt(inj.saved_tokens))}</span></div>
      </div>
      <div class="ho-section">Servers <span class="ho-count">${servers.length}</span></div>
      <div class="mcp-list">${servers.map(serverCard).join('') || '<div class="ho-empty sm">No MCP servers yet.</div>'}</div>
      <div class="ho-section">Add a server</div>
      <div class="mcp-add">
        <input class="mcp-in" id="mcpName" placeholder="name (e.g. filesystem)" spellcheck="false">
        <input class="mcp-in" id="mcpCmd" placeholder="command (e.g. npx)" spellcheck="false">
        <input class="mcp-in" id="mcpArgs" placeholder="args, space separated (e.g. -y @modelcontextprotocol/server-filesystem C:\\work)" spellcheck="false">
        <input class="mcp-in" id="mcpEnv" placeholder="env KEY=VALUE, space separated (optional)" spellcheck="false">
        <button class="ho-btn primary" id="mcpAdd">Add</button>
      </div>
      <div class="ho-sub sm">Nothing is installed or launched by adding a server — it starts disabled.</div>
      <div class="mcp-add">
        <input class="mcp-in" id="mcpHttpUrl" placeholder="…or an HTTP endpoint (https://host/mcp)" spellcheck="false">
        <button class="ho-btn" id="mcpAddHttp">Add HTTP server</button>
      </div>
      <div class="ho-section">Discovered tools <span class="ho-count">${(tools.tools || []).length}</span></div>
      <div class="mcp-tools">${(tools.tools || []).map(toolRow).join('') || '<div class="ho-empty sm">Nothing discovered yet.</div>'}</div>
      <div class="ho-section">Recent activity</div>
      <div class="mcp-audit">${(audit.events || []).slice().reverse().map((e) =>
        `<div class="audit-row"><span class="audit-event">${esc(e.event)}</span>` +
        `<span class="audit-detail">${esc(e.server || '')} ${esc(e.tool || '')} ${esc(e.error || e.reason || '')}</span></div>`
      ).join('') || '<div class="ho-empty sm">No activity yet.</div>'}</div>`;

    const call = async (act) => {
      const sid = panel.querySelector('.mcp-card') && null;
      return act();
    };
    panel.querySelectorAll('.mcp-card').forEach((card) => {
      const id = card.dataset.id;
      card.querySelector('[data-act="toggle"]').addEventListener('click', async () => {
        const on = card.querySelector('.badge').classList.contains('on');
        const r = await patch('/api/mcp/servers/' + encodeURIComponent(id), { enabled: !on });
        note(r.ok ? `${id} ${!on ? 'enabled' : 'disabled'}` : r.error, !r.ok);
        renderMcp();
      });
      card.querySelector('[data-act="start"]').addEventListener('click', async () => {
        note(`starting ${id}…`);
        const r = await post(`/api/mcp/servers/${encodeURIComponent(id)}/start`);
        note(r.ok ? `${id} running (${(r.tools || []).length || r.tools || 0} tools)` : (r.error || 'start failed'), !r.ok);
        renderMcp();
      });
      card.querySelector('[data-act="stop"]').addEventListener('click', async () => {
        await post(`/api/mcp/servers/${encodeURIComponent(id)}/stop`);
        note(`${id} stopped`);
        renderMcp();
      });
      card.querySelector('[data-act="discover"]').addEventListener('click', async () => {
        const r = await post(`/api/mcp/servers/${encodeURIComponent(id)}/discover`);
        note(r.ok ? `${id}: ${(r.tools || []).length} tool(s) discovered` : (r.error || 'scan failed'), !r.ok);
        renderMcp();
      });
      card.querySelector('[data-act="remove"]').addEventListener('click', async () => {
        if (!confirm(`Remove MCP server "${id}"?`)) return;
        await api('/api/mcp/servers/' + encodeURIComponent(id), { method: 'DELETE' });
        note(`${id} removed`);
        renderMcp();
      });
    });

    panel.querySelectorAll('.mcp-tool').forEach((row) => {
      row.querySelector('.pin').addEventListener('change', async (e) => {
        const key = row.dataset.key;
        const [sid, name] = key.split(':');
        const r = await post(`/api/mcp/tools/${encodeURIComponent(sid)}/${encodeURIComponent(name)}/pin`,
          { pinned: e.target.checked });
        note(r.ok ? `${name} ${e.target.checked ? 'pinned' : 'unpinned'} (${fmt(r.token_cost)} tokens)` : r.error, !r.ok);
        renderMcp();
      });
    });

    $('#mcpDirect').addEventListener('change', async (e) => {
      const r = await put('/api/mcp/settings', { direct_mode: e.target.checked });
      note(r.ok ? `Direct MCP mode ${e.target.checked ? 'on' : 'off'}` : (r.error || 'failed'), !r.ok);
      renderMcp();
    });

    $('#mcpAdd').addEventListener('click', async () => {
      const name = $('#mcpName').value.trim();
      const command = $('#mcpCmd').value.trim();
      if (!command) { note('a command is required', true); return; }
      const args = $('#mcpArgs').value.trim() ? $('#mcpArgs').value.trim().split(/\s+/) : [];
      const env = {};
      $('#mcpEnv').value.trim().split(/\s+/).filter(Boolean).forEach((pair) => {
        const i = pair.indexOf('=');
        if (i > 0) env[pair.slice(0, i)] = pair.slice(i + 1);
      });
      const r = await post('/api/mcp/servers', { name: name || command, command, args, env });
      note(r.ok ? `added "${r.server.id}" (disabled) — enable and Re-scan to discover tools` : (r.error || 'add failed'), !r.ok);
      if (r.ok) renderMcp();
    });

    $('#mcpAddHttp').addEventListener('click', async () => {
      const url = $('#mcpHttpUrl').value.trim();
      if (!url) { note('an endpoint URL is required', true); return; }
      const r = await post('/api/mcp/servers', { name: url, command: url, transport: 'http' });
      note(r.ok ? `added "${r.server.id}" over HTTP (disabled)` : (r.error || 'add failed'), !r.ok);
      if (r.ok) renderMcp();
    });
  }

  // ── Learning ──────────────────────────────────────────────────────────
  // Moved out of Capabilities: the mode selector, the analyzer, and the
  // proposals with their actions. The buttons were wired correctly all along
  // — the missing toast() above them is what made every action look dead.
  async function renderLearning() {
    const panel = $('#panel-learning');
    panel.innerHTML = '<div class="ho-loading">Loading learning…</div>';
    const c = await api('/api/capabilities');
    const lg = await api('/api/learning');
    const an = await api('/api/analyzer').catch(() => ({ enabled: false }));
    const kind = (k) => (c.providers || []).filter((p) => p.kind === k);
    const optionFor = (row, chosen) => {
      const usable = row.available && row.implemented;
      const why = !row.available ? (row.reason || 'unavailable')
        : (!row.implemented ? 'no adapter yet' : '');
      return `<option value="${esc(row.id)}"${row.id === chosen ? ' selected' : ''}` +
        `${usable ? '' : ' disabled'}>${esc(row.name)}${usable ? '' : ' — ' + esc(why)}</option>`;
    };

    panel.innerHTML = `
      <div class="ho-section">Learning <span class="ho-sub sm">whether Tacit may learn</span></div>
      <div class="mcp-add">
        <select class="git-select" id="capLearning">${kind('learning').map((r) => optionFor(r, (c.learning || {}).id)).join('')}</select>
        <button class="ho-btn small" id="capLearningSave">Save</button>
      </div>
      <div class="mcp-add">
        <label class="ho-sub sm"><input type="checkbox" id="capAnalyzer"${an.enabled ? ' checked' : ''}> Auto-analyze finished sessions</label>
        <button class="ho-btn small" id="capAnalyzerRun">Analyze now</button>
      </div>
      <div class="ho-sub sm">Runs on a timer over finished transcripts and leaves proposals behind. It adds no tool to the agent, makes no model call, and cannot apply anything.</div>
      <div class="tok-table">
        <div class="tok-row"><span class="tok-label">Sessions read</span><span class="tok-value">${an.sessions_read || 0}</span></div>
        <div class="tok-row"><span class="tok-label">Proposals made</span><span class="tok-value">${an.proposals_made || 0}</span></div>
        <div class="tok-row"><span class="tok-label">Rules seen before</span><span class="tok-value">${an.rules_seen || 0}</span></div>
      </div>

      <div class="ho-section">Proposals <span class="ho-count">${(lg.artifacts || []).length}</span></div>
      <div class="mem-list">${(lg.artifacts || []).map((p) =>
        `<div class="mem-row" data-art="${esc(p.id)}">` +
        `<span class="mem-type badge">${esc(p.kind)}</span>` +
        `<span class="badge${p.risk === 'high' ? ' danger-high' : ''}">${esc(p.risk)}</span>` +
        `<span class="badge${p.state === 'approved' ? ' on' : ''}">${esc(p.state)}</span>` +
        `<span class="mem-text">${esc((p.title || p.body || '').slice(0, 110))}` +
        (p.provenance
          ? `<span class="ho-sub sm">because ${esc(p.provenance.why || p.provenance.rule || 'it matched a pattern')}</span>` +
            `<span class="ho-sub sm">you said: “${esc((p.provenance.snippet || '').slice(0, 100))}” (turn ${esc(p.provenance.turn)})</span>`
          : '') +
        `</span>` +
        `<span class="badge">${esc(p.display)}</span>` +
        `<span class="mem-actions">` +
        (p.state === 'approved'
          ? '<button class="ho-btn small" data-act="pdisable">disable</button>'
          : '<button class="ho-btn small primary" data-act="papprove">approve</button>') +
        `<button class="ho-btn small" data-act="preject">reject</button>` +
        `<button class="ho-btn small" data-act="pedit">edit</button>` +
        `<button class="ho-btn small danger" data-act="pdel">×</button>` +
        `</span></div>`).join('') || '<div class="ho-empty sm">Nothing proposed yet.</div>'}</div>
      <div class="mcp-add">
        <input class="mcp-in" id="capPropTitle" placeholder="title" spellcheck="false">
        <input class="mcp-in" id="capPropBody" placeholder="what should be remembered" spellcheck="false">
        <select class="git-select" id="capPropKind">${['rule', 'preference', 'correction', 'skill'].map((k) => `<option value="${k}">${k}</option>`).join('')}</select>
        <button class="ho-btn small" id="capPropAdd">Propose</button>
      </div>
      <div class="ho-sub sm">A proposal changes nothing until you approve it. Approving a skill writes a skill file; anything else goes to memory, under the memory budget.</div>`;

    $('#capLearningSave').addEventListener('click', async () => {
      const r = await post('/api/capabilities/learning', { mode: $('#capLearning').value });
      if (!r.ok) { toast(r.error || 'could not save', true); return; }
      toast('learning set to ' + r.learning.id);
      renderLearning();
    });
    const anBox = $('#capAnalyzer');
    if (anBox) anBox.addEventListener('change', async () => {
      const r = await post('/api/analyzer', { enabled: anBox.checked });
      if (!r.ok) { toast(r.error || 'could not save', true); return; }
      toast('session analysis ' + (r.enabled ? 'on' : 'off'));
    });
    const anRun = $('#capAnalyzerRun');
    if (anRun) anRun.addEventListener('click', async () => {
      anRun.disabled = true;
      toast('reading finished sessions…');
      const r = await post('/api/analyzer/run', {});
      anRun.disabled = false;
      if (!r.ok) { toast(r.error || 'could not run', true); return; }
      toast(`${r.sessions_read} session(s) read, ${r.proposals} proposal(s) added`);
      renderLearning();
    });

    panel.querySelectorAll('[data-art]').forEach((row) => {
      const id = row.dataset.art;
      const act = async (what) => {
        const r = await post(`/api/learning/${encodeURIComponent(id)}/${what}`);
        if (!r.ok) { toast(r.error || 'could not do that', true); return; }
        toast(what === 'approve' ? ('approved, wrote ' + (r.wrote || 'nothing')) : what);
        renderLearning();
      };
      const on = (name, fn) => {
        const b = row.querySelector(`[data-act="${name}"]`);
        if (b) b.addEventListener('click', fn);
      };
      on('papprove', () => act('approve'));
      on('pdisable', () => act('disable'));
      on('preject', () => act('reject'));
      on('pedit', async () => {
        const next = prompt('Edit the proposal body', row.querySelector('.mem-text').textContent);
        if (next == null) return;
        const r = await api('/api/learning/' + encodeURIComponent(id),
          { method: 'PATCH', body: JSON.stringify({ body: next }) });
        toast(r.ok ? 'edited' : (r.error || 'could not edit'), !r.ok);
        if (r.ok) renderLearning();
      });
      on('pdel', async () => {
        if (!confirm('Delete this proposal? Anything it wrote is removed too.')) return;
        const r = await api('/api/learning/' + encodeURIComponent(id), { method: 'DELETE' });
        toast(r.ok ? 'deleted' : (r.error || 'could not delete'), !r.ok);
        if (r.ok) renderLearning();
      });
    });
    $('#capPropAdd').addEventListener('click', async () => {
      const body = $('#capPropBody').value.trim();
      if (!body) { toast('say what should be remembered', true); return; }
      const r = await post('/api/learning/propose', {
        kind: $('#capPropKind').value, title: $('#capPropTitle').value.trim(), body,
      });
      if (!r.ok) { toast(r.error || 'could not propose', true); return; }
      toast(r.auto_applied ? 'proposed and applied by the current mode' : 'proposed');
      renderLearning();
    });
  }

  // ── Snapshots (per session) ────────────────────────────────────────────
  // Moved out of Settings: snapshots belong to the session that took them,
  // so the panel is reached from the top bar and lists only this session's.
  // The backend marks each snapshot with the session that took it; snapshots
  // from before that change carry no marker and are counted in the note line
  // rather than silently vanishing.
  async function renderSnapshots() {
    const box = $('#snapList');
    if (!box) return;
    const sid = (window.Tacit && window.Tacit.getSid && window.Tacit.getSid()) || '';
    const project = (window.Tacit && window.Tacit.getWorkdir && window.Tacit.getWorkdir()) || '';
    box.innerHTML = '<div class="ho-loading">Loading snapshots…</div>';
    const d = await api('/api/snapshots?session=' + encodeURIComponent(sid));
    const rows = d.snapshots || [];
    let unfiled = 0;
    if (rows.length === 0) {
      const all = await api('/api/snapshots');
      unfiled = (all.snapshots || []).filter((s) => !s.session).length;
    }
    const snapNote = (t, isErr) => {
      const el = $('#snapNote');
      if (el) { el.textContent = t || ''; el.classList.toggle('err', !!isErr); }
    };

    box.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">${rows.length} snapshot(s) · ${esc(fmt(d.bytes))} bytes · target workspace: <code>${esc(project || '(none selected)')}</code></span>
        <button class="ho-btn small" id="snRefresh">Refresh</button>
      </div>
      ${project ? '' : '<div class="ho-err">Pick a workspace first — restoring needs a target.</div>'}
      ${rows.length === 0 && unfiled ? `<div class="ho-sub sm">${unfiled} earlier snapshot(s) predate per-session tracking and are not listed here.</div>` : ''}
      <div class="snap-list">
        ${rows.map((s) => `
          <div class="snap-card" data-name="${esc(s.name)}">
            <div class="mcp-head">
              <span class="snap-time">${esc(s.created || s.name)}</span>
              ${s.label ? `<span class="badge">${esc(s.label)}</span>` : ''}
              <span class="badge">${s.file_count} file(s)</span>
              <span class="badge">${esc(fmt(s.bytes))} B</span>
            </div>
            <div class="snap-files">${(s.files || []).slice(0, 12).map(esc).join(' · ')}${s.file_count > 12 ? ' …' : ''}</div>
            <div class="mcp-actions">
              <button class="ho-btn small" data-act="compare">Compare</button>
              <button class="ho-btn small" data-act="restore">Restore</button>
            </div>
            <div class="snap-extra" hidden></div>
          </div>`).join('') || '<div class="ho-empty sm">No snapshots in this session yet — the agent takes one automatically before its first edit of a turn.</div>'}
      </div>`;

    $('#snRefresh').addEventListener('click', renderSnapshots);

    box.querySelectorAll('.snap-card').forEach((card) => {
      const name = card.dataset.name;
      const extra = card.querySelector('.snap-extra');
      card.querySelector('[data-act="compare"]').addEventListener('click', async () => {
        if (!project) { snapNote('pick a workspace first', true); return; }
        const r = await post('/api/snapshots/compare', { name, project });
        extra.hidden = false;
        if (!r.ok) { extra.innerHTML = `<div class="ho-err">${esc(r.error || 'failed')}</div>`; return; }
        const list = (label, items, cls) => items.length
          ? `<div class="compare-group ${cls}"><span class="compare-label">${label} (${items.length})</span>` +
            items.slice(0, 40).map((f) => `<div class="compare-file">${esc(f)}</div>`).join('') + '</div>'
          : '';
        extra.innerHTML = r.count
          ? list('changed in your workspace', r.changed, 'chg') +
            list('added since', r.added, 'add') +
            list('removed since', r.removed, 'del')
          : '<div class="ho-sub">Identical to the snapshot.</div>';
      });
      card.querySelector('[data-act="restore"]').addEventListener('click', async () => {
        if (!project) { snapNote('pick a workspace first', true); return; }
        if (!confirm(`Restore ${name} into\n${project}\n\nFiles are overwritten with the snapshot copies. Continue?`)) return;
        const r = await post('/api/snapshots/restore', { name, project });
        snapNote(r.ok ? (r.message || 'restored') : (r.error || 'restore failed'), !r.ok);
        if (r.ok) renderSnapshots();
      });
    });
  }

  function openSnapshots() {
    const ov = $('#snapOverlay');
    if (!ov) return;
    ov.hidden = false;
    renderSnapshots();
  }
  function closeSnapshots() {
    const ov = $('#snapOverlay');
    if (ov) ov.hidden = true;
  }

  // ── Memory Vault ───────────────────────────────────────────────────────
  async function renderMemory() {
    const panel = $('#panel-memory');
    panel.innerHTML = '<div class="ho-loading">Loading memories…</div>';
    const plugins = await api('/api/plugins');
    const vault = (plugins.plugins || []).find((p) => p.id === 'memory_vault') || {};
    const d = await api('/api/memory');
    const st = d.stats || {};
    const startup = await api('/api/memory/startup');
    const rows = d.memories || [];

    const memRow = (m) => `
      <div class="mem-row ${m.enabled ? '' : 'off'}" data-id="${m.id}">
        <span class="mem-type badge">${esc(m.type)}</span>
        <span class="mem-scope badge">${esc(m.scope)}</span>
        <span class="mem-conf badge conf-${esc(m.confidence)}">${esc(m.confidence)}</span>
        <span class="mem-text">${esc((m.content || '').slice(0, 220))}</span>
        <span class="badge">${esc(fmt(m.token_estimate))} tok</span>
        <span class="badge">used ${m.use_count}×</span>
        <span class="mem-actions">
          <button class="ho-btn small" data-act="pin" title="Pin">${m.pinned ? '★' : '☆'}</button>
          <button class="ho-btn small" data-act="toggle">${m.enabled ? 'on' : 'off'}</button>
          <button class="ho-btn small" data-act="edit">edit</button>
          <button class="ho-btn small danger" data-act="del">×</button>
        </span>
      </div>`;

    panel.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">Vault is <strong>${vault.enabled ? 'enabled' : 'disabled'}</strong>.</span>
        <button class="ho-btn small" id="mvToggle">${vault.enabled ? 'Disable vault' : 'Enable vault'}</button>
        <button class="ho-btn small" id="mvExtract">Extract from session</button>
        <button class="ho-btn small" id="mvSummarise">Summarise session</button>
        <button class="ho-btn small" id="mvCompress">Compress startup set</button>
      </div>
      <div class="tok-table">
        <div class="tok-row"><span class="tok-label">Memories</span><span class="tok-value">${st.count || 0}</span><span class="tok-hint">${st.enabled || 0} enabled, ${st.pinned || 0} pinned</span></div>
        <div class="tok-row"><span class="tok-label">Startup block</span><span class="tok-value">${esc(startup.display || fmt(startup.tokens))}</span><span class="tok-hint">of ${st.budget || 0}-token budget</span></div>
        <div class="tok-row"><span class="tok-label">Held back by budget</span><span class="tok-value">${st.excluded_by_budget || 0}</span></div>
        <div class="tok-row"><span class="tok-label">All memories if injected</span><span class="tok-value">${esc(fmt(st.total_tokens))}</span><span class="tok-hint">the cost the budget avoids</span></div>
      </div>
      <div class="ho-toolbar">
        <label class="chk">Budget <input id="mvBudget" class="mcp-in sm" type="number" min="0" value="${st.budget || 0}"> tokens</label>
        <button class="ho-btn small" id="mvSaveBudget">Save</button>
        <span class="ho-sub">default 120 · hard max 500</span>
      </div>
      <div class="ho-section">Startup block preview</div>
      <pre class="mem-preview">${esc(startup.text || '(nothing would be injected)')}</pre>
      <div class="ho-section">Add a memory</div>
      <div class="mcp-add">
        <input class="mcp-in" id="memContent" placeholder="a durable fact, preference or decision" spellcheck="false">
        <select id="memType" class="git-select">${['preference', 'project_fact', 'decision', 'lesson', 'pattern', 'contact', 'other'].map((t) => `<option>${t}</option>`).join('')}</select>
        <select id="memConf" class="git-select">${['low', 'medium', 'high'].map((t) => `<option${t === 'medium' ? ' selected' : ''}>${t}</option>`).join('')}</select>
        <label class="chk"><input type="checkbox" id="memPin"> pin</label>
        <button class="ho-btn primary" id="memAdd">Add</button>
      </div>
      <div class="ho-section">Memories <span class="ho-count">${rows.length}</span></div>
      <div class="mem-list">${rows.map(memRow).join('') || '<div class="ho-empty sm">Nothing stored yet.</div>'}</div>`;

    $('#mvToggle').addEventListener('click', async () => {
      const p = vault.enabled ? 'disable' : 'enable';
      const r = await post(`/api/plugins/memory_vault/${p}`);
      note(r.ok ? `Memory Vault ${p}d` : (r.error || 'failed'), !r.ok);
      renderMemory();
    });

    $('#mvSaveBudget').addEventListener('click', async () => {
      const r = await post('/api/memory/budget', { budget: parseInt($('#mvBudget').value, 10) || 0 });
      note(r.ok ? `budget set to ${r.budget} tokens` : (r.error || 'failed'), !r.ok);
      renderMemory();
    });

    $('#memAdd').addEventListener('click', async () => {
      const content = $('#memContent').value.trim();
      if (!content) { note('content is required', true); return; }
      const r = await post('/api/memory', {
        content, type: $('#memType').value, confidence: $('#memConf').value,
        pinned: $('#memPin').checked, source: 'user',
      });
      note(r.ok ? 'memory added' : (r.error || 'failed'), !r.ok);
      if (r.ok) renderMemory();
    });

    $('#mvExtract').addEventListener('click', async () => {
      const sid = (window.Tacit && window.Tacit.getSid && window.Tacit.getSid()) || '';
      note('asking the model for candidate memories…');
      const r = await post('/api/memory/extract', { sid });
      if (!r.ok) { note(r.error || 'extract failed', true); return; }
      if (!r.candidates.length) { note('no candidates proposed'); return; }
      const box = document.createElement('div');
      box.className = 'mem-candidates';
      box.innerHTML = '<div class="ho-section">Proposed — approve or skip</div>' +
        r.candidates.map((c, i) => `
          <div class="mem-cand" data-i="${i}">
            <span class="badge">${esc(c.type)}</span>
            <span class="badge conf-${esc(c.confidence)}">${esc(c.confidence)}</span>
            <span class="badge">${esc(c.token_display || fmt(c.token_estimate))}</span>
            <input class="mcp-in" value="${esc(c.content)}">
            <button class="ho-btn small primary" data-act="ok">Approve</button>
            <button class="ho-btn small" data-act="no">Skip</button>
          </div>`).join('');
      panel.insertBefore(box, panel.querySelector('.ho-section'));
      box.querySelectorAll('.mem-cand').forEach((row) => {
        const c = r.candidates[parseInt(row.dataset.i, 10)];
        row.querySelector('[data-act="ok"]').addEventListener('click', async () => {
          const content = row.querySelector('input').value;
          const res = await post('/api/memory', { content, type: c.type, confidence: c.confidence,
            source: 'session_extract' });
          row.remove();
          note(res.ok ? 'approved' : (res.error || 'failed'), !res.ok);
          if (res.ok) renderMemory();
        });
        row.querySelector('[data-act="no"]').addEventListener('click', () => row.remove());
      });
    });

    $('#mvSummarise').addEventListener('click', async () => {
      const sid = (window.Tacit && window.Tacit.getSid && window.Tacit.getSid()) || '';
      if (!sid) { note('no session is open', true); return; }
      note('summarising this session…');
      const preview = await post('/api/memory/summarise', { sid });
      if (!preview.ok) { note(preview.error || 'could not summarise', true); return; }
      const box = document.createElement('div');
      box.className = 'mem-candidates';
      box.innerHTML =
        `<div class="ho-section">Session summary — ${esc(preview.display)} tokens</div>` +
        `<pre class="mem-preview">${esc(preview.summary)}</pre>` +
        `<div class="mcp-add">
           <input class="mcp-in sm" id="mvSumTtl" type="number" min="0" placeholder="ttl days">
           <button class="ho-btn small primary" id="mvSumSave">Save to memory</button>
           <button class="ho-btn small" id="mvSumCancel">Cancel</button>
         </div>`;
      panel.insertBefore(box, panel.querySelector('.ho-section'));
      $('#mvSumCancel').addEventListener('click', () => box.remove());
      $('#mvSumSave').addEventListener('click', async () => {
        const ttl = parseInt($('#mvSumTtl').value, 10) || 0;
        const saved = await post('/api/memory/summarise',
          { sid, text: preview.summary, save: true, ttl_days: ttl });
        note(saved.ok ? 'saved as a session summary' : (saved.error || 'could not save'), !saved.ok);
        box.remove();
        if (saved.ok) renderMemory();
      });
    });

    $('#mvCompress').addEventListener('click', async () => {
      note('compressing the startup set…');
      const preview = await post('/api/memory/compress', { apply: false });
      if (!preview.ok) { note(preview.error || 'compress failed', true); return; }
      const html = `
        <div class="ho-section">Compression preview — ${esc(preview.before_display)} → ${esc(preview.after_display)} (saves ${esc(preview.saved_display)})</div>
        <pre class="mem-preview">${esc(preview.after)}</pre>
        <div class="ho-toolbar">
          <span class="ho-sub">Nothing is changed until you apply it.</span>
          <button class="ho-btn primary" id="memApply">Apply (disables the originals)</button>
          <button class="ho-btn" id="memCancel">Cancel</button>
        </div>`;
      const box = document.createElement('div');
      box.className = 'mem-candidates';
      box.innerHTML = html;
      panel.insertBefore(box, panel.querySelector('.ho-section'));
      $('#memCancel').addEventListener('click', () => box.remove());
      $('#memApply').addEventListener('click', async () => {
        const applied = await post('/api/memory/compress', { apply: true });
        note(applied.ok ? `applied — saved ${applied.saved_display}` : (applied.error || 'failed'), !applied.ok);
        box.remove();
        renderMemory();
      });
    });

    panel.querySelectorAll('.mem-row').forEach((row) => {
      const id = row.dataset.id;
      const m = rows.find((x) => String(x.id) === String(id));
      row.querySelector('[data-act="pin"]').addEventListener('click', async () => {
        await patch('/api/memory/' + id, { pinned: !m.pinned });
        renderMemory();
      });
      row.querySelector('[data-act="toggle"]').addEventListener('click', async () => {
        await patch('/api/memory/' + id, { enabled: !m.enabled, pinned: m.enabled ? false : m.pinned });
        renderMemory();
      });
      row.querySelector('[data-act="edit"]').addEventListener('click', async () => {
        const next = prompt('Edit memory', m.content);
        if (next == null) return;
        const r = await patch('/api/memory/' + id, { content: next });
        note(r.ok ? 'updated' : (r.error || 'failed'), !r.ok);
        renderMemory();
      });
      row.querySelector('[data-act="del"]').addEventListener('click', async () => {
        if (!confirm('Delete this memory?')) return;
        await api('/api/memory/' + id, { method: 'DELETE' });
        renderMemory();
      });
    });
  }

  // ── Plugins ────────────────────────────────────────────────────────────
  async function renderPlugins() {
    const panel = $('#panel-plugins');
    panel.innerHTML = '<div class="ho-loading">Loading plugins…</div>';
    const d = await api('/api/plugins');
    const rows = d.plugins || [];
    const impact = d.impact || { total: 0 };
    panel.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">Disabled by default. A plugin contributes nothing until you enable it.</span>
        <span class="badge">startup impact: ${esc(fmt(impact.total))} tokens</span>
      </div>
      ${rows.map((p) => `
        <div class="plug-card" data-id="${esc(p.id)}">
          <div class="mcp-head">
            <span class="mcp-name">${esc(p.name)}</span>
            <span class="badge">v${esc(p.version)}</span>
            <span class="badge ${p.enabled ? 'on' : ''}">${p.enabled ? 'enabled' : 'disabled'}</span>
            <span class="badge">${esc(fmt(p.token_budget))} tok</span>
            <span class="badge">${esc(p.source)}</span>
            ${(p.provides || []).map((x) => `<span class="badge">${esc(x)}</span>`).join('')}
          </div>
          <div class="mcp-meta">${esc(p.description)}</div>
          ${(p.permissions || []).length ? `<div class="mcp-meta"><span class="ho-sub">permissions: ${esc(p.permissions.join(', '))}</span></div>` : ''}
          ${p.error ? `<div class="ho-err">${esc(p.error)}</div>` : ''}
          <div class="mcp-actions">
            <button class="ho-btn small ${p.enabled ? '' : 'primary'}" data-act="toggle">${p.enabled ? 'Disable' : 'Enable'}</button>
            ${(p.settings || []).length ? '<button class="ho-btn small" data-act="settings">Settings</button>' : ''}
            <button class="ho-btn small" data-act="logs">Logs</button>
          </div>
          <div class="plug-extra" hidden></div>
        </div>`).join('')}`;

    panel.querySelectorAll('.plug-card').forEach((card) => {
      const id = card.dataset.id;
      const p = rows.find((x) => x.id === id);
      const extra = card.querySelector('.plug-extra');
      card.querySelector('[data-act="toggle"]').addEventListener('click', async () => {
        const act = p.enabled ? 'disable' : 'enable';
        const r = await post(`/api/plugins/${encodeURIComponent(id)}/${act}`);
        note(r.ok ? `${p.name} ${act}d` : (r.error || 'failed'), !r.ok);
        renderPlugins();
      });
      card.querySelector('[data-act="logs"]').addEventListener('click', async () => {
        const r = await api(`/api/plugins/${encodeURIComponent(id)}/logs`);
        extra.hidden = false;
        extra.innerHTML = '<pre class="mem-preview">' +
          esc((r.logs || []).map((l) => `${l.level}  ${l.message}`).join('\n') || 'no log entries') + '</pre>';
      });
      const setBtn = card.querySelector('[data-act="settings"]');
      if (setBtn) setBtn.addEventListener('click', async () => {
        const r = await api(`/api/plugins/${encodeURIComponent(id)}/settings`);
        extra.hidden = false;
        extra.innerHTML = (r.schema || []).map((s) => {
          const v = r.values[s.key];
          const input = s.type === 'bool'
            ? `<input type="checkbox" class="plug-set" data-key="${esc(s.key)}" ${v ? 'checked' : ''}>`
            : `<input class="mcp-in sm plug-set" data-key="${esc(s.key)}" value="${esc(v)}">`;
          return `<label class="chk">${esc(s.label || s.key)} ${input}</label>`;
        }).join('') + '<button class="ho-btn small primary" data-act="save">Save</button>';
        extra.querySelector('[data-act="save"]').addEventListener('click', async () => {
          const values = {};
          extra.querySelectorAll('.plug-set').forEach((el) => {
            values[el.dataset.key] = el.type === 'checkbox' ? el.checked : el.value;
          });
          const saved = await put(`/api/plugins/${encodeURIComponent(id)}/settings`, values);
          note(saved.ok ? 'settings saved' : (saved.error || 'failed'), !saved.ok);
        });
      });
    });
  }

  function fmt(n) {
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    if (n < 1e6) return (n / 1000).toFixed(1).replace('.0', '') + 'k';
    return (n / 1e6).toFixed(1).replace('.0', '') + 'M';
  }

  install();
  window.TacitPlatform = { render, openSnapshots };
})();
