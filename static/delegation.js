/**
 * The Delegation panel: what sub-agents and research are doing, live, on the left.
 *
 * The main transcript never shows a sub-agent's tool calls — they run in their own
 * context by design, and only the report comes back. That is the right shape for
 * the window and the wrong shape for a person watching: a `task` call could run
 * twelve steps, and the interface showed nothing but a spinner. This panel is the
 * missing view. It rides the same socket as everything else (no second
 * connection), and it is fed by the `delegation_activity` events the chat router
 * forwards for sub-agent and research tool calls.
 *
 * The panel also opens itself when a delegation launches: the transcript shows a
 * `task` or `research` tool card and then nothing until the report lands, so the
 * one view of the work in flight opens on its own instead of waiting to be found.
 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  let models = [];
  let delegateModel = '';
  let cards = new Map();     // key -> {label, activity, state, ts}
  let order = [];            // insertion order of keys
  let settingsOpen = false;

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    try { return await r.json(); } catch (e) { return { ok: false, error: 'bad response' }; }
  }
  const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });

  function toast(text, isErr) {
    if (window.Tacit && window.Tacit.toast) window.Tacit.toast(text, isErr);
  }

  // ── rendering ──────────────────────────────────────────────────────────
  function renderMessages() {
    const box = $('#dgMessages');
    if (!box) return;
    if (!order.length) {
      box.innerHTML = '<div class="as-empty">Nothing is running. When the agent delegates to a ' +
        'sub-agent or calls research, what those are doing shows up here, live.<br><br>' +
        '<span class="as-note">Sub-agents run in their own context; only their report reaches ' +
        'the transcript.</span></div>';
      return;
    }
    box.innerHTML = order.map((k) => {
      const c = cards.get(k);
      const age = Math.max(0, Math.round((Date.now() - (c.ts || 0)) / 1000));
      return `
      <div class="dg-card ${c.state || 'active'}">
        <div class="dg-head">
          <span class="dg-dot"></span>
          <span class="dg-label">${esc(c.label || k)}</span>
          <span class="dg-age">${age}s</span>
        </div>
        <div class="dg-activity">${esc(c.activity || 'working…')}</div>
      </div>`;
    }).join('');
  }

  function renderSettings() {
    const box = $('#dgSettings');
    if (!box) return;
    const providers = [];
    models.forEach((m) => { if (!providers.includes(m.provider)) providers.push(m.provider); });
    const chosen = models.find((m) => m.id === delegateModel) || null;
    const provider = (chosen && chosen.provider) || providers[0] || '';
    const providerModels = models.filter((m) => m.provider === provider);
    const opt = (v, label, on) =>
      `<option value="${esc(v)}"${on ? ' selected' : ''}>${esc(label)}</option>`;
    box.innerHTML = `
      <div class="as-group">Model for sub-agents & research</div>
      <div class="as-row as-pickers">
        <select class="ctl-select as-sel" data-set="provider">
          <option value="">(session model)</option>
          ${providers.map((p) => opt(p, p, p === provider && !!delegateModel)).join('')}
        </select>
        <select class="ctl-select as-sel" data-set="model">
          <option value="">(session model)</option>
          ${providerModels.map((m) => opt(m.id, m.name || m.id, m.id === delegateModel)).join('')}
        </select>
      </div>`;
    box.querySelectorAll('[data-set]').forEach((el) => {
      el.addEventListener('change', async () => {
        if (el.dataset.set === 'provider') {
          const first = models.find((m) => m.provider === el.value);
          await save({ delegate_model: first ? first.id : '' });
          return;
        }
        await save({ delegate_model: el.value });
      });
    });
  }

  async function save(patch) {
    const d = await post('/api/harness/delegate-model', patch);
    if (!d.ok) { toast(d.error || 'could not save that', true); return; }
    delegateModel = d.delegate_model || '';
    renderSettings();
    toast(delegateModel ? 'delegation model: ' + delegateModel
                        : 'delegation runs on the session model');
  }

  async function load() {
    const d = await api('/api/harness/delegate-model');
    if (d.ok) {
      models = d.models || [];
      delegateModel = d.delegate_model || '';
      renderSettings();
    }
  }

  // ── the socket's delegation_activity events ────────────────────────────
  function handle(m) {
    // A card arriving while the panel is closed means a delegation is running
    // that the person cannot see. This is the backstop for the tool_start hook
    // below: it catches a socket that reconnected mid-turn, a page that loaded
    // after the launch, and any path that never produced a transcript card.
    if (!isOpen()) openPanel();
    // One card per sub-agent call id; research aspects key on their label.
    const key = m.research ? ('research:' + (m.aspect || 'general'))
                           : ('call:' + (m.id || m.name || 'x'));
    const label = m.research
      ? ('research · ' + (m.aspect || 'general'))
      : ('sub-agent ' + String(m.id || '').slice(0, 8));
    let activity = '';
    if (m.type === 'tool_start') activity = '→ ' + (m.name || 'working');
    else if (m.type === 'tool_end') activity = (m.is_error ? '✗ ' : '✓ ') + (m.name || '');
    else if (m.message) activity = m.message;
    const existing = cards.get(key);
    cards.set(key, {
      label: label || (existing && existing.label) || key,
      activity: activity || (existing && existing.activity) || 'working…',
      state: m.type === 'tool_end' && !m.is_error ? 'done' : 'active',
      ts: Date.now(),
    });
    if (!order.includes(key)) order.push(key);
    if (order.length > 12) {           // bounded: a panel, not a transcript
      const drop = order.shift();
      cards.delete(drop);
    }
    renderMessages();
  }

  function clear() {
    cards.clear();
    order = [];
    renderMessages();
  }

  // ── auto-open on launch ────────────────────────────────────────────────
  function isOpen() {
    const panel = $('#dgPanel');
    return !!(panel && panel.classList.contains('open'));
  }

  function openPanel() {
    const panel = $('#dgPanel');
    if (!panel || panel.classList.contains('open')) return;
    panel.classList.add('open');
    const btn = $('#dgToggle');
    if (btn) btn.classList.add('on');
    try { localStorage.setItem('tacit.delegationOpen', '1'); } catch (e) { /* ignore */ }
    load();
  }

  // A delegation launches when the main transcript draws its tool card: the
  // `task` and `research` tool_start events arrive before any sub-agent has run
  // a step, so the panel is open before the first delegation_activity lands.
  // app.js calls this from its tool_start handler.
  function maybeLaunch(name) {
    if (name === 'task' || name === 'research') openPanel();
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  function install() {
    const panel = $('#dgPanel');
    if (!panel) return;
    $('#dgClose').addEventListener('click', togglePanel);
    $('#dgToggle').addEventListener('click', togglePanel);
    $('#dgSettingsBtn').addEventListener('click', () => {
      const box = $('#dgSettingsWrap');
      if (box) {
        box.hidden = !box.hidden;
        settingsOpen = !box.hidden;
        if (settingsOpen) load();
      }
    });
    renderMessages();
  }

  function togglePanel() {
    const panel = $('#dgPanel');
    if (!panel) return;
    panel.classList.toggle('open');
    const open = panel.classList.contains('open');
    try { localStorage.setItem('tacit.delegationOpen', open ? '1' : '0'); } catch (e) { /* ignore */ }
    const btn = $('#dgToggle');
    if (btn) btn.classList.toggle('on', open);
    if (open) load();
  }

  install();
  try {
    if (localStorage.getItem('tacit.delegationOpen') === '1') {
      const panel = $('#dgPanel');
      if (panel) {
        panel.classList.add('open');
        const btn = $('#dgToggle');
        if (btn) btn.classList.add('on');
      }
    }
  } catch (e) { /* ignore */ }

  window.TacitDelegation = { handle, clear, togglePanel, load, maybeLaunch };
})();