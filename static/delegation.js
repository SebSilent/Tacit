/**
 * The Delegation panel: what sub-agents and research are doing, live.
 *
 * Each sub-agent/research gets its own expandable card showing the full trace:
 * thinking (reason), tool calls with args/results, and text output.
 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  let models = [];
  let delegateModel = '';
  // key -> {label, state, ts, trace: [{type, name, args, result, is_error, delta, ...}]}
  let cards = new Map();
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
      const traceHtml = renderTrace(c.trace || []);
      return `
      <div class="dg-card ${c.state || 'active'}" data-key="${esc(k)}">
        <div class="dg-head">
          <span class="dg-dot"></span>
          <span class="dg-label">${esc(c.label || k)}</span>
          <span class="dg-age">${age}s</span>
          <button class="dg-toggle" title="Expand/collapse" aria-expanded="false">
            <svg viewBox="0 0 24 24" width="14" height="14"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
          </button>
        </div>
        <div class="dg-activity">${esc(c.activity || 'working…')}</div>
        <div class="dg-trace" hidden>${traceHtml}</div>
      </div>`;
    }).join('');

    // Attach toggle handlers
    box.querySelectorAll('.dg-toggle').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const card = btn.closest('.dg-card');
        const trace = card?.querySelector('.dg-trace');
        const expanded = trace && !trace.hidden;
        if (trace) {
          trace.hidden = expanded;
          btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
          btn.querySelector('svg').style.transform = expanded ? '' : 'rotate(180deg)';
        }
      });
    });
  }

  function renderTrace(trace) {
    if (!trace.length) return '<div class="dg-empty">working…</div>';
    return trace.map((ev) => {
      if (ev.type === 'reason') {
        return `<div class="dg-reason">${esc(ev.delta || '')}</div>`;
      }
      if (ev.type === 'tool_start') {
        const args = ev.args ? `<pre class="dg-args">${esc(JSON.stringify(ev.args, null, 2))}</pre>` : '';
        return `<div class="dg-tool"><span class="dg-tool-name">→ ${esc(ev.name || 'tool')}</span>${args}</div>`;
      }
      if (ev.type === 'tool_end') {
        const status = ev.is_error ? '✗' : '✓';
        const result = ev.result ? `<pre class="dg-result">${esc(String(ev.result).slice(0, 2000))}</pre>` : '';
        return `<div class="dg-tool ${ev.is_error ? 'dg-tool-err' : ''}"><span class="dg-tool-name">${status} ${esc(ev.name || 'tool')}</span>${result}</div>`;
      }
      if (ev.type === 'text') {
        return `<div class="dg-text">${esc(ev.delta || '')}</div>`;
      }
      if (ev.type === 'notify') {
        return `<div class="dg-notify">${esc(ev.message || '')}</div>`;
      }
      return '';
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
    // Use parent_call_id to group all events from the same delegation together.
    const parentId = m.parent_call_id || '';
    const key = m.research ? ('research:' + (m.aspect || 'general'))
                           : ('call:' + (parentId || m.id || m.name || 'x'));
    const label = m.research
      ? ('research · ' + (m.aspect || 'general'))
      : ('sub-agent ' + String(parentId || m.id || '').slice(0, 8));
    let activity = '';
    let isDone = false;
    if (m.type === 'tool_start') {
      activity = '→ ' + (m.name || 'working');
    } else if (m.type === 'tool_end') {
      activity = (m.is_error ? '✗ ' : '✓ ') + (m.name || '');
    } else if (m.type === 'reason') {
      activity = 'thinking…';
    } else if (m.type === 'text') {
      // Sub-agent finishes with a text event (the report). Show a snippet.
      const delta = m.delta || '';
      if (delta.trim()) {
        activity = delta.slice(0, 80).replace(/\n/g, ' ') + (delta.length > 80 ? '…' : '');
        // If this looks like a substantial final report, mark done.
        // Heuristic: non-trivial text after we've seen some tool activity.
        const existing = cards.get(key);
        const hadTools = existing && existing.trace && existing.trace.some(e => e.type === 'tool_start');
        if (hadTools && delta.length > 100) isDone = true;
      }
    } else if (m.message) {
      activity = m.message;
      // Explicit completion notify from backend
      if (m.message && (m.message.includes('complete') || m.message.includes('finished'))) {
        isDone = true;
      }
    }
    const existing = cards.get(key);
    const trace = (existing && existing.trace) || [];
    // Append this event to the trace
    trace.push(m);
    // Determine done state: explicit done flag, or tool_end without error,
    // or a substantial text event after tool activity.
    let state = 'active';
    if (isDone) state = 'done';
    else if (m.type === 'tool_end' && !m.is_error) state = 'done';
    else if (existing && existing.state === 'done') state = 'done'; // sticky once done
    cards.set(key, {
      label: label || (existing && existing.label) || key,
      activity: activity || (existing && existing.activity) || 'working…',
      state: state,
      ts: Date.now(),
      trace: trace,
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