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

  // ── tabs + panels ──────────────────────────────────────────────────────
  const TABS = [
    { id: 'dashboard', label: 'Tokens' },
    { id: 'mcp', label: 'MCP' },
    { id: 'memory', label: 'Memory' },
    { id: 'snapshots', label: 'Snapshots' },
    { id: 'plugins', label: 'Plugins' },
  ];

  function install() {
    const tabs = $('#hoTabs');
    const body = $('.ho-body');
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
  }

  function render(tab) {
    if (tab === 'dashboard') renderDashboard();
    else if (tab === 'mcp') renderMcp();
    else if (tab === 'memory') renderMemory();
    else if (tab === 'snapshots') renderSnapshots();
    else if (tab === 'plugins') renderPlugins();
  }

  // ── Tokens dashboard ───────────────────────────────────────────────────
  async function renderDashboard() {
    const panel = $('#panel-dashboard');
    panel.innerHTML = '<div class="ho-loading">Measuring…</div>';
    const sid = (window.Tacit && window.Tacit.getSid && window.Tacit.getSid()) || '';
    const d = await api('/api/tokens/dashboard?sid=' + encodeURIComponent(sid));
    if (!d.ok) { panel.innerHTML = `<div class="ho-err">${esc(d.error || 'failed')}</div>`; return; }
    const pf = await api('/api/profiles');
    const activeProfile = pf.active || '';
    const profileRow = (p) => `
      <div class="prof-row ${p.active ? 'on' : ''}" data-name="${esc(p.name)}">
        <span class="prof-name">${esc(p.label)}</span>
        <span class="badge">${esc(p.cost_display)} tokens</span>
        ${p.active ? '<span class="badge on">active</span>' : `<button class="ho-btn small" data-act="apply">Apply</button>`}
        <span class="prof-desc">${esc(p.description)}</span>
        ${p.builtin ? '' : '<button class="ho-btn small danger" data-act="del">×</button>'}
      </div>`;
    const profiles = (pf.profiles || []).map(profileRow).join('');
    const mark = d.exact ? '' : '<span class="ho-sub"> estimates (chars/4)</span>';
    const row = (label, value, hint) =>
      `<div class="tok-row"><span class="tok-label">${esc(label)}</span>` +
      `<span class="tok-value">${esc(value)}</span>` +
      (hint ? `<span class="tok-hint">${esc(hint)}</span>` : '') + '</div>';

    panel.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">Local accounting${mark} — nothing leaves this machine.</span>
        <button class="ho-btn small" id="tkRefresh">Refresh</button>
      </div>
      <div class="ho-section">Profile <span class="ho-sub sm">what is switched on, and what it costs</span></div>
      <div class="prof-list">${profiles}</div>
      <div class="mcp-add">
        <input class="mcp-in" id="profName" placeholder="save current setup as…" spellcheck="false">
        <button class="ho-btn small" id="profSave">Save profile</button>
        <span class="ho-sub sm">Switching a profile applies immediately and updates the numbers below.</span>
      </div>
      <div class="ho-section">This session</div>
      <div class="tok-table">
        ${row('Prompt tokens billed', d.prompt_tokens_display)}
        ${row('Completion tokens billed', d.completion_tokens_display)}
        ${row('Total billed', d.total_tokens_display)}
        ${row('MCP tool calls', d.mcp_calls)}
      </div>
      <div class="ho-section">Startup prompt</div>
      <div class="tok-table">
        ${row('Base system prompt', d.base_prompt_tokens_display, 'the ~980-character core')}
        ${row('Built-in tool schemas', d.tool_schema_tokens_display, d.tool_count + ' tools')}
        ${row('MCP schemas injected', d.mcp_injected_tokens_display, 'activated or pinned')}
        ${row('Memory block', d.memory_tokens_display, d.memory_enabled ? 'vault on' : 'vault off')}
      </div>
      <div class="ho-section">Context discipline</div>
      <div class="tok-table">
        ${row('Estimated full-context baseline', d.full_context_baseline_display, 'if every discovered schema and memory were injected directly')}
        ${row('Tacit actual startup', d.actual_startup_display, 'what is really injected')}
        ${row('Saved by lazy MCP activation', d.saved_lazy_tools_display)}
        ${row('Saved by memory budgeting', d.saved_memory_budget_display)}
        ${row('Saved by compaction', d.saved_compaction_display)}
        ${row('Saved by delegation', d.saved_subagent_display)}
      </div>
      <div class="tok-hero">
        <span class="tok-hero-num">${esc(d.saved_by_discipline_display)}</span>
        <span class="tok-hero-label">Saved by context discipline</span>
      </div>`;
    $('#tkRefresh').addEventListener('click', renderDashboard);

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
      <div class="ho-section">Import DSH</div>
      <div class="mcp-add">
        <input class="mcp-in" id="dshPkg" placeholder="npm package or command (e.g. @scope/dsh-tool)" spellcheck="false">
        <button class="ho-btn primary" id="dshImport">Import as MCP server</button>
        <button class="ho-btn small" id="dshStatusBtn">Show status</button>
        <button class="ho-btn small" id="dshScaffold">Generate adapter</button>
      </div>
      <div class="ho-sub sm">DSH plugins are Node/Cordis bundles. Anything that speaks MCP works directly; the rest gets an adapter scaffold. Nothing is installed or run.</div>
      <div id="dshOut"></div>
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

    const dshOut = () => $('#dshOut');
    $('#dshImport').addEventListener('click', async () => {
      const pkg = $('#dshPkg').value.trim();
      if (!pkg) { note('a package or command is required', true); return; }
      const r = await post('/api/dsh/import', { package: pkg });
      dshOut().innerHTML = `<pre class="mem-preview">${esc(r.message || r.error || '')}</pre>`;
      note(r.ok ? 'imported as a disabled MCP server' : (r.error || 'import failed'), !r.ok);
      if (r.ok) renderMcp();
    });
    $('#dshStatusBtn').addEventListener('click', async () => {
      const r = await api('/api/dsh/status');
      dshOut().innerHTML = `<pre class="mem-preview">${esc((r.plugins || []).map((p) =>
        `${p.name || p.package}: ${p.status} — ${p.detail}`).join('\n') || 'No DSH plugins registered yet.')}</pre>`;
    });
    $('#dshScaffold').addEventListener('click', async () => {
      const pkg = $('#dshPkg').value.trim();
      if (!pkg) { note('enter the plugin id first', true); return; }
      const r = await post('/api/dsh/scaffold', { plugin_id: pkg });
      dshOut().innerHTML = `<pre class="mem-preview">${esc(r.message || r.error || '')}</pre>`;
      note(r.ok ? 'adapter scaffold written' : (r.error || 'failed'), !r.ok);
    });
  }

  // ── Snapshots ──────────────────────────────────────────────────────────
  async function renderSnapshots() {
    const panel = $('#panel-snapshots');
    panel.innerHTML = '<div class="ho-loading">Loading snapshots…</div>';
    const d = await api('/api/snapshots');
    const rows = d.snapshots || [];
    const project = (window.Tacit && window.Tacit.getWorkdir && window.Tacit.getWorkdir()) || '';

    panel.innerHTML = `
      <div class="ho-toolbar">
        <span class="ho-sub">${rows.length} snapshot(s) · ${esc(fmt(d.bytes))} bytes · target workspace: <code>${esc(project || '(none selected)')}</code></span>
        <button class="ho-btn small" id="snRefresh">Refresh</button>
      </div>
      ${project ? '' : '<div class="ho-err">Pick a workspace first — restoring needs a target.</div>'}
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
          </div>`).join('') || '<div class="ho-empty sm">No snapshots yet — the agent takes one with the snapshot tool before risky edits.</div>'}
      </div>`;

    $('#snRefresh').addEventListener('click', renderSnapshots);

    panel.querySelectorAll('.snap-card').forEach((card) => {
      const name = card.dataset.name;
      const extra = card.querySelector('.snap-extra');
      card.querySelector('[data-act="compare"]').addEventListener('click', async () => {
        if (!project) { note('pick a workspace first', true); return; }
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
        if (!project) { note('pick a workspace first', true); return; }
        if (!confirm(`Restore ${name} into\n${project}\n\nFiles are overwritten with the snapshot copies. Continue?`)) return;
        const r = await post('/api/snapshots/restore', { name, project });
        note(r.ok ? (r.message || 'restored') : (r.error || 'restore failed'), !r.ok);
      });
    });
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
  window.TacitPlatform = { render };
})();
