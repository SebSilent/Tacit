/* Tacit Harness — providers · skills · tools manager.
 * Talks to /api/providers, /api/skills, /api/tools, /api/harness/*.
 * Edits ~/.tacit/models.json, prefs.json and skills/ on the server;
 * nothing here is injected into the LLM prompt. */
(() => {
'use strict';
const $ = s => document.querySelector(s);
const overlay = $('#harnessOverlay');
const note = $('#hoNote');

function setNote(t, err) { note.textContent = t || ''; note.classList.toggle('err', !!err); }

async function api(path, opts = {}) {
  try {
    const r = await fetch(path, opts);
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok && !j.error) j.error = 'HTTP ' + r.status;
    return j;
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
const post = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {})
});

function esc(t) {
  return String(t ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── shell: open / close / tabs ───────────────────────────────────────
$('#harnessBtn').addEventListener('click', openOverlay);
$('#hoClose').addEventListener('click', closeOverlay);
overlay.addEventListener('click', e => { if (e.target === overlay) closeOverlay(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !overlay.hidden) closeOverlay(); });

$('#hoTabs').addEventListener('click', e => {
  const btn = e.target.closest('.ho-tab'); if (!btn) return;
  document.querySelectorAll('.ho-tab').forEach(b => b.classList.toggle('active', b === btn));
  const tab = btn.dataset.tab;
  document.querySelectorAll('.ho-panel').forEach(p => p.hidden = (p.id !== 'panel-' + tab));
  loadPanel(tab);
});

function openOverlay() { overlay.hidden = false; setNote(''); loadPanel(activeTab()); }
function closeOverlay() { overlay.hidden = true; setNote(''); }
function activeTab() {
  const t = document.querySelector('.ho-tab.active');
  return t ? t.dataset.tab : 'providers';
}
function loadPanel(tab) {
  if (tab === 'providers') return renderProviders();
  if (tab === 'skills') return renderSkills();
  if (tab === 'tools') return renderTools();
  if (tab === 'appearance') return renderAppearancePanel();
}

// ═══════════════════════════ APPEARANCE ═══════════════════════════
function renderAppearancePanel() {
  const panel = $('#panel-appearance');
  const T = window.LCTheme;
  if (!T) { panel.innerHTML = '<div class="ho-empty">Theme engine unavailable.</div>'; return; }
  const cur = T.current();
  panel.innerHTML = `
    <div class="ho-toolbar"><span class="ho-sub">Pick a look — applies instantly, stored per-browser (<code>lc.theme</code>).</span></div>
    <div class="theme-cards">` +
    T.themes.map(t => `
      <button class="theme-card ${t.id === cur ? 'active' : ''}" data-theme="${esc(t.id)}">
        <span class="theme-dot sw-${esc(t.id)}"></span>
        <span class="theme-meta"><b>${esc(t.name)}</b><small>${esc(t.hint)}</small></span>
        <span class="theme-check">${t.id === cur ? 'Active' : ''}</span>
      </button>`).join('') +
    `</div>`;
  panel.querySelectorAll('.theme-card').forEach(b => b.addEventListener('click', () => {
    T.apply(b.dataset.theme);
    renderAppearancePanel();
  }));
}

// Keep session actions in the sidebar / command palette, not in the harness.

// ═══════════════════════════ PROVIDERS ══════════════════════════════
async function renderProviders() {
  const panel = $('#panel-providers');
  panel.innerHTML = '<div class="ho-loading">Loading providers…</div>';
  const data = await api('/api/providers');
  const providers = data.providers || {};
  const defaultModel = data.default || '';

  let html = `
    <div class="ho-toolbar">
      <button class="ho-btn primary" id="pvAddBtn">
        <svg viewBox="0 0 24 24" width="14" height="14"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
        Add provider
      </button>
      <span class="ho-sub">Registry: <code>~/.tacit/models.json</code></span>
    </div>
    <div class="ho-form" id="pvForm" hidden>
      <div class="ho-form-grid">
        <label>Name <input id="pvName" placeholder="my-lan" spellcheck="false"></label>
        <label>Base URL <input id="pvBase" placeholder="http://192.168.x.x:8100/v1" spellcheck="false"></label>
        <label>API key <input id="pvKey" placeholder="noop (or ENV_VAR_NAME)" spellcheck="false"></label>
        <label>API
          <select id="pvApi"><option value="openai-completions">openai-completions</option><option value="openai-responses">openai-responses</option></select>
        </label>
      </div>
      <div class="ho-form-actions">
        <button class="ho-btn primary" id="pvCreate">Create</button>
        <button class="ho-btn" id="pvCancel">Cancel</button>
      </div>
    </div>
    <div class="prov-list">`;

  const names = Object.keys(providers);
  if (!names.length) {
    html += '<div class="ho-empty">No providers configured.</div>';
  }
  for (const name of names) {
    const p = providers[name];
    const keyBadge = p.apiKey.kind === 'env'
      ? `<span class="badge key" title="resolved from env var ${esc(p.apiKey.env)}">env:${esc(p.apiKey.env)}${p.apiKey.env_set ? '' : ' (unset)'}</span>`
      : (p.apiKey.kind === 'literal' ? '<span class="badge key">key</span>' : '<span class="badge key">no-key</span>');
    html += `
      <div class="prov-card" data-prov="${esc(name)}">
        <div class="prov-head">
          <div class="prov-name">${esc(name)} ${keyBadge}</div>
          <div class="prov-actions">
            <button class="ho-btn small" data-act="probe" title="Pull the live model list from the endpoint">Re-scan</button>
            <button class="ho-btn small danger" data-act="del" title="Delete provider">Delete</button>
          </div>
        </div>
        <div class="prov-meta"><code>${esc(p.baseUrl || '')}</code> · ${esc(p.api || '')}</div>
        <div class="model-list">`;
    const models = p.models || [];
    if (!models.length) {
      html += '<div class="ho-empty sm">No models discovered — hit Re-scan to pull the live list from the endpoint.</div>';
    }
    for (const m of models) {
      const fullId = `${name}/${m.id}`;
      const isDefault = fullId === defaultModel;
      html += `
        <div class="model-row ${isDefault ? 'is-default' : ''}" data-model="${esc(m.id)}">
          <span class="model-id">${esc(m.id)}</span>
          <span class="model-ctx">${m.contextWindow ? Math.round(m.contextWindow / 1024) + 'k' : ''}</span>
          <span class="model-spacer"></span>
          ${isDefault ? '<span class="badge default">default</span>' : `<button class="ho-btn small" data-act="set-default">Set default</button>`}
          <button class="ho-btn small danger" data-act="del-model" title="Remove model">×</button>
        </div>`;
    }
    html += `</div>
        <div class="model-add">
          <input class="model-add-id" placeholder="model id, e.g. qwythos-mtp:latest" spellcheck="false">
          <input class="model-add-name" placeholder="display name (optional)" spellcheck="false">
          <label class="chk"><input type="checkbox" class="model-add-reason" checked> reasoning</label>
          <button class="ho-btn small primary" data-act="add-model">Add model</button>
        </div>
        <div class="probe-results" hidden></div>
      </div>`;
  }
  html += '</div>';
  panel.innerHTML = html;

  // ── wiring ──
  $('#pvAddBtn').addEventListener('click', () => { $('#pvForm').hidden = false; $('#pvName').focus(); });
  $('#pvCancel').addEventListener('click', () => { $('#pvForm').hidden = true; });
  $('#pvCreate').addEventListener('click', async () => {
    const body = {
      name: $('#pvName').value.trim(),
      baseUrl: $('#pvBase').value.trim(),
      apiKey: $('#pvKey').value.trim(),
      api: $('#pvApi').value,
    };
    const r = await api('/api/providers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.ok) {
      const n = (r.discovered || []).length;
      setNote(n ? `Provider "${body.name}" created — auto-discovered ${n} model(s) from the endpoint`
                : (r.probe_error
                    ? `Provider created, but auto-discovery failed: ${r.probe_error} — use Re-scan once it's reachable`
                    : `Provider "${body.name}" created`));
      $('#pvForm').hidden = true; renderProviders(); refreshSidebar();
    }
    else setNote(r.error || 'Create failed', true);
  });

  panel.querySelectorAll('.prov-card').forEach(card => {
    const prov = card.dataset.prov;

    card.querySelector('[data-act="del"]').addEventListener('click', async () => {
      if (!confirm(`Delete provider "${prov}" and all its models?`)) return;
      const r = await api('/api/providers/' + encodeURIComponent(prov), { method: 'DELETE' });
      if (r.ok) { setNote(`Provider "${prov}" deleted`); renderProviders(); refreshSidebar(); }
      else setNote(r.error || 'Delete failed', true);
    });

    card.querySelector('[data-act="probe"]').addEventListener('click', async () => {
      const box = card.querySelector('.probe-results');
      box.hidden = false;
      box.innerHTML = '<span class="ho-sub">Probing…</span>';
      const r = await api(`/api/providers/${encodeURIComponent(prov)}/probe`, { method: 'POST' });
      if (!r.ok) { box.innerHTML = `<span class="ho-err">${esc(r.error)}</span>`; return; }
      const fresh = r.new || [];
      if (!fresh.length) { box.innerHTML = `<span class="ho-sub">Endpoint reachable — ${r.models.length} model(s), all already added.</span>`; return; }
      box.innerHTML = `
        <div class="probe-head">Found ${fresh.length} new model(s) on the endpoint:</div>
        <div class="probe-ids">${fresh.map(id => `<label class="chk"><input type="checkbox" class="probe-pick" value="${esc(id)}" checked> ${esc(id)}</label>`).join('')}</div>
        <button class="ho-btn small primary probe-import">Import selected</button>`;
      box.querySelector('.probe-import').addEventListener('click', async () => {
        const ids = [...box.querySelectorAll('.probe-pick')].filter(c => c.checked).map(c => c.value);
        if (!ids.length) { setNote('Nothing selected', true); return; }
        const imp = await post(`/api/providers/${encodeURIComponent(prov)}/import`, { ids });
        if (imp.ok) { setNote(`Imported ${imp.added.length} model(s)`); renderProviders(); refreshSidebar(); }
        else setNote(imp.error || 'Import failed', true);
      });
    });

    card.querySelector('[data-act="add-model"]').addEventListener('click', async () => {
      const id = card.querySelector('.model-add-id').value.trim();
      const name = card.querySelector('.model-add-name').value.trim();
      const reasoning = card.querySelector('.model-add-reason').checked;
      if (!id) { setNote('Model id required', true); return; }
      const r = await post(`/api/providers/${encodeURIComponent(prov)}/models`, { id, name, reasoning });
      if (r.ok) { setNote(`Model "${id}" added to ${prov}`); renderProviders(); refreshSidebar(); }
      else setNote(r.error || 'Add failed', true);
    });

    card.querySelectorAll('.model-row').forEach(row => {
      const mid = row.dataset.model;
      const setDef = row.querySelector('[data-act="set-default"]');
      if (setDef) setDef.addEventListener('click', async () => {
        const r = await post('/api/default', { model: `${prov}/${mid}` });
        if (r.ok) { setNote(`Default model → ${r.default}`); renderProviders(); refreshSidebar(); }
        else setNote(r.error || 'Failed', true);
      });
      row.querySelector('[data-act="del-model"]').addEventListener('click', async () => {
        const r = await post(`/api/providers/${encodeURIComponent(prov)}/models/remove`, { id: mid });
        if (r.ok) { setNote(`Removed ${prov}/${mid}`); renderProviders(); refreshSidebar(); }
        else setNote(r.error || 'Failed', true);
      });
    });
  });
}

// ═══════════════════════════ SKILLS ═════════════════════════════════
async function renderSkills() {
  const panel = $('#panel-skills');
  panel.innerHTML = '<div class="ho-loading">Loading skills…</div>';
  const d = await api('/api/skills');

  const skillCard = (s, deletable) => `
    <div class="skill-card" data-name="${esc(s.name)}">
      <div class="skill-head">
        <span class="skill-name">${esc(s.name)}</span>
        <span class="badge">${esc(s.category || (deletable ? 'user' : 'bundled'))}</span>
        ${deletable ? '<button class="ho-btn small danger skill-del" title="Delete">Delete</button>' : ''}
      </div>
      <div class="skill-desc">${esc(s.description || s.topic || '')}</div>
      <details class="skill-body"><summary>content</summary><pre>${esc(s.body || '')}</pre></details>
    </div>`;

  let html = `
    <div class="ho-toolbar">
      <button class="ho-btn primary" id="skAddBtn">
        <svg viewBox="0 0 24 24" width="14" height="14"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
        Add user skill
      </button>
      <span class="ho-sub">User skills: <code>${esc(d.user_skills_dir || '')}</code></span>
    </div>
    <div class="ho-form" id="skForm" hidden>
      <div class="ho-form-grid">
        <label>Name <input id="skName" placeholder="my-skill" spellcheck="false"></label>
        <label>Description <input id="skDesc" placeholder="when to use this skill" spellcheck="false"></label>
      </div>
      <label class="block-label">Body (markdown instructions)
        <textarea id="skBody" rows="6" placeholder="# Steps…"></textarea>
      </label>
      <div class="ho-form-actions">
        <button class="ho-btn primary" id="skCreate">Create</button>
        <button class="ho-btn" id="skCancel">Cancel</button>
      </div>
    </div>`;

  html += `<div class="ho-section">Your skills <span class="ho-count">${(d.user_skills || []).length}</span></div>
    <div class="skill-grid">`;
  html += (d.user_skills || []).length
    ? d.user_skills.map(s => skillCard(s, true)).join('')
    : '<div class="ho-empty sm">No user skills yet — add one above.</div>';
  html += '</div>';

  html += `<div class="ho-section">Bundled tool skills <span class="ho-count">${(d.tool_skills || []).length}</span></div>
    <div class="skill-grid">${(d.tool_skills || []).map(s => skillCard({ ...s, category: 'tool' }, false)).join('') || '<div class="ho-empty sm">none</div>'}</div>`;

  html += `<div class="ho-section">Bundled knowledge <span class="ho-count">${(d.knowledge || []).length}</span></div>
    <div class="skill-grid">${(d.knowledge || []).map(s => skillCard(s, false)).join('') || '<div class="ho-empty sm">none</div>'}</div>`;

  panel.innerHTML = html;

  $('#skAddBtn').addEventListener('click', () => { $('#skForm').hidden = false; $('#skName').focus(); });
  $('#skCancel').addEventListener('click', () => { $('#skForm').hidden = true; });
  $('#skCreate').addEventListener('click', async () => {
    const body = { name: $('#skName').value.trim(), description: $('#skDesc').value.trim(), body: $('#skBody').value };
    const r = await post('/api/skills/user', body);
    if (r.ok) { setNote(`Skill "${r.name}" created`); $('#skForm').hidden = true; renderSkills(); }
    else setNote(r.error || 'Create failed', true);
  });
  panel.querySelectorAll('.skill-del').forEach(btn => btn.addEventListener('click', async e => {
    const name = e.target.closest('.skill-card').dataset.name;
    if (!confirm(`Delete skill "${name}"?`)) return;
    const r = await api('/api/skills/user/' + encodeURIComponent(name), { method: 'DELETE' });
    if (r.ok) { setNote(`Skill "${name}" deleted`); renderSkills(); }
    else setNote(r.error || 'Delete failed', true);
  }));
}

// ═══════════════════════════ TOOLS ══════════════════════════════════
async function renderTools() {
  const panel = $('#panel-tools');
  panel.innerHTML = '<div class="ho-loading">Loading tools…</div>';
  const d = await api('/api/tools');

  const toolRow = t => `
    <div class="tool-row ${t.enabled ? '' : 'off'}" data-name="${esc(t.name)}">
      <span class="tool-name">${esc(t.name)}</span>
      <span class="tool-desc">${esc(t.description || '')}</span>
      <button class="tgl ${t.enabled ? 'on' : ''}" data-act="toggle" role="switch" aria-checked="${t.enabled}" title="${t.enabled ? 'Disable' : 'Enable'}">
        <span class="tgl-knob"></span>
      </button>
    </div>`;

  panel.innerHTML = `
    <div class="ho-toolbar"><span class="ho-sub">${esc(d.note || '')}</span></div>
    <div class="ho-section">Built-in tools <span class="ho-count">${(d.builtin || []).length}</span></div>
    <div class="tool-list">${(d.builtin || []).map(toolRow).join('')}</div>`;

  panel.querySelectorAll('.tool-row [data-act="toggle"]').forEach(btn => {
    btn.addEventListener('click', async e => {
      const row = e.target.closest('.tool-row');
      const name = row.dataset.name;
      const enable = !btn.classList.contains('on');
      const r = await post('/api/tools/toggle', { name, enabled: enable });
      if (r.ok) {
        btn.classList.toggle('on', enable);
        btn.setAttribute('aria-checked', enable);
        row.classList.toggle('off', !enable);
        setNote(`${name} ${enable ? 'enabled' : 'disabled'} (applies to new agent sessions)`);
      } else setNote(r.error || 'Toggle failed', true);
    });
  });
}

// ── keep the sidebar model dropdown in sync after registry edits ────
function refreshSidebar() {
  if (window.LC && window.LC.refreshInfo) window.LC.refreshInfo();
}

// expose for app.js if needed
window.LCHarness = { open: openOverlay, close: closeOverlay };
})();
