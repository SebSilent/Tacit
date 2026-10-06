/* The Assistant: a per-session side conversation.
 *
 * It reads the session and helps the person direct it. The main agent never
 * sees it. Everything travels over the socket app.js already owns, so this
 * panel adds no second connection.
 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  let sid = '';
  let messages = [];
  let settings = null;
  let preview = null;
  let models = [];
  let thinkingLevels = [];
  let sessionModel = '';
  let sessionThinking = 'medium';
  let streaming = null;      // { el, text, raf }
  let busy = false;

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    try { return await r.json(); } catch (e) { return { ok: false, error: 'bad response' }; }
  }
  const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });
  const send = (obj) => window.Tacit && window.Tacit.sendMessage && window.Tacit.sendMessage(obj);

  function toast(text, isErr) {
    if (window.Tacit && window.Tacit.toast) window.Tacit.toast(text, isErr);
  }

  // ── markdown ───────────────────────────────────────────────────────────
  // The assistant answers in markdown, rendered by the same renderer the main
  // chat uses (shared over window.Tacit.md), so a drafted prompt arrives as a
  // fenced code block with a one-click copy button. If that bridge is missing
  // — an older cached app.js — the fallback is plain escaped text, never
  // nothing and never raw HTML.
  function renderMd(text) {
    if (window.Tacit && typeof window.Tacit.md === 'function') {
      try { return window.Tacit.md(text); } catch (e) { return esc(text); }
    }
    // The `md` class sets white-space: normal, so a plain-text fallback has to
    // carry its own line breaks or a no-bridge reply reads as one long line.
    return esc(text).replace(/\n/g, '<br>');
  }

  // Copy buttons arrive inside rendered markdown, so one delegated handler per
  // container covers every block, streamed or re-rendered.
  function bindCopy(box) {
    if (!box || box._copyBound) return;
    box._copyBound = true;
    box.addEventListener('click', (e) => {
      const btn = e.target.closest('.copy-btn');
      if (!btn) return;
      const block = btn.closest('.code-block');
      if (!block) return;
      const code = block.querySelector('code').textContent;
      navigator.clipboard.writeText(code).then(() => {
        btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
      });
    });
  }

  // ── rendering ──────────────────────────────────────────────────────────
  function renderMessages() {
    const box = $('#asMessages');
    if (!box) return;
    if (!messages.length) {
      box.innerHTML = '<div class="as-empty">Ask about this session. Draft a prompt, think an ' +
        'approach through, or ask what the agent just did.<br><br>' +
        '<span class="as-note">The agent cannot see this conversation.</span></div>';
      return;
    }
    // Your own prompts stay plain text; the assistant's answers render as
    // markdown. The `md` class switches pre-wrap off, which plain text needs
    // and rendered HTML must not have.
    box.innerHTML = messages.map((m) => `
      <div class="as-msg ${m.role === 'user' ? 'me' : 'them'}">
        <div class="as-body${m.role === 'user' ? '' : ' md'}">${
          m.role === 'user' ? esc(m.content || '') : renderMd(m.content || '')}</div>
      </div>`).join('');
    bindCopy(box);
    box.scrollTop = box.scrollHeight;
  }

  function renderContext() {
    const box = $('#asContext');
    if (!box || !preview) return;
    const t = preview.tokens;
    box.innerHTML = `
      <div class="as-cost">
        <span class="as-cost-num">${esc(preview.display)}</span>
        <span class="as-cost-label">tokens of session context</span>
      </div>
      <div class="as-break">
        <span>you ${t.session}</span><span>persona ${t.persona}</span>
        <span>history ${t.history}</span><span>tools ${t.tools}</span>
      </div>`;
  }

  function renderControls() {
    const box = $('#asControls');
    if (!box || !settings) return;
    const chk = (key, label, hint) => `
      <label class="chk as-chk" title="${esc(hint || '')}">
        <input type="checkbox" data-set="${key}" ${settings[key] ? 'checked' : ''}> ${esc(label)}
      </label>`;

    // Same three pickers as the main bar: provider, model, thinking. They open
    // on whatever the session is already using, and only differ once you change
    // them.
    const ref = settings.model || sessionModel || '';
    const providers = [];
    models.forEach((m) => { if (!providers.includes(m.provider)) providers.push(m.provider); });
    const inSession = models.find((m) => m.id === ref) || null;
    const provider = (inSession && inSession.provider) || providers[0] || '';
    const providerModels = models.filter((m) => m.provider === provider);
    const modelId = (inSession && inSession.id) || (providerModels[0] || {}).id || '';
    const thinking = settings.thinking || sessionThinking || 'medium';
    const opt = (v, label, on) =>
      `<option value="${esc(v)}"${on ? ' selected' : ''}>${esc(label)}</option>`;

    box.innerHTML = `
      <div class="as-group">What it can read</div>
      <div class="as-row">
        ${chk('include_user', 'Your prompts')}
        ${chk('include_assistant', 'Agent replies')}
        ${chk('include_meta', 'Session info')}
        ${chk('include_tools', 'Tool calls', 'A one-line digest of the tools behind each answer. Off by default: it is the expensive part of the transcript.')}
        ${chk('tools', 'Read-only tools', 'Lets the assistant read files. Off by default: schemas are expensive.')}
      </div>
      <label class="chk as-turns">Last <input type="number" min="1" max="200" value="${settings.turns}" data-set="turns"> turns</label>
      <div class="as-group">Provider · Model · Thinking</div>
      <div class="as-row as-pickers">
        <select class="ctl-select as-sel" data-set="provider">
          ${providers.map((p) => opt(p, p, p === provider)).join('')}
        </select>
        <select class="ctl-select as-sel" data-set="model">
          ${providerModels.map((m) => opt(m.id, m.name || m.id, m.id === modelId)).join('')}
        </select>
        <select class="ctl-select as-sel" data-set="thinking">
          ${thinkingLevels.map((l) => opt(l, l, l === thinking)).join('')}
        </select>
      </div>`;

    box.querySelectorAll('[data-set]').forEach((el) => {
      el.addEventListener('change', () => {
        const key = el.dataset.set;
        if (key === 'provider') {
          const first = models.find((m) => m.provider === el.value);
          if (first) save({ model: first.id });
          return;
        }
        if (key === 'turns') { save({ turns: parseInt(el.value, 10) || 1 }); return; }
        // A checkbox's .value is "on" whether it is ticked or not, so sending it
        // made every switch sticky: bool("on") is True, and there was no way to
        // turn anything back off.
        if (el.type === 'checkbox') { save({ [key]: el.checked }); return; }
        save({ [key]: el.value });
      });
    });
  }

  function renderAll() {
    renderControls();
    renderContext();
    renderMessages();
  }

  // ── data ───────────────────────────────────────────────────────────────
  async function load() {
    // The panel can be opened before app.js has told us which session is
    // active, so fall back to asking directly rather than rendering nothing.
    if (!sid && window.Tacit && window.Tacit.getSid) sid = window.Tacit.getSid() || '';
    if (!sid) return;
    const d = await api('/api/assistant/' + encodeURIComponent(sid));
    if (!d.ok) {
      messages = [];
      renderMessages();
      return;
    }
    messages = d.messages || [];
    settings = d.settings || settings;
    preview = d.preview || preview;
    models = d.models || [];
    thinkingLevels = d.thinking_levels || [];
    sessionModel = d.session_model || '';
    sessionThinking = d.session_thinking || 'medium';
    renderAll();
  }

  async function save(patch) {
    if (!sid) return;
    const d = await post('/api/assistant/' + encodeURIComponent(sid) + '/settings', patch);
    if (!d.ok) { toast(d.error || 'could not save that', true); return; }
    settings = d.settings;
    preview = d.preview;
    renderControls();
    renderContext();
  }

  function sessionChanged(next) {
    if (next === sid) return;
    sid = next || '';
    messages = [];
    streaming = null;
    busy = false;
    setBusy(false);
    if ($('#asMessages')) $('#asMessages').innerHTML = '';
    load();
  }

  function setBusy(on) {
    const btn = $('#asSend');
    if (btn) btn.disabled = !!on;
    const box = $('#asPanel');
    if (box) box.classList.toggle('busy', !!on);
  }

  // A stream that ends must flush its last frame: the rAF throttle can
  // otherwise drop the tail between the final delta and the done event.
  function finishStream() {
    if (!streaming) return;
    if (streaming.raf) { cancelAnimationFrame(streaming.raf); streaming.raf = 0; }
    if (streaming.el) streaming.el.innerHTML = renderMd(streaming.text || '');
  }

  // ── the socket's assistant_* events ────────────────────────────────────
  function handle(m) {
    if (m.sid && sid && m.sid !== sid) return;   // a different session's stream
    switch (m.type) {
      case 'assistant_text': {
        if (!streaming) {
          streaming = { text: '', raf: 0 };
          const box = $('#asMessages');
          if (box) {
            const empty = box.querySelector('.as-empty');
            if (empty) empty.remove();
            const el = document.createElement('div');
            el.className = 'as-msg them';
            el.innerHTML = '<div class="as-body md"></div>';
            box.appendChild(el);
            box.scrollTop = box.scrollHeight;
          }
        }
        streaming.text += m.delta || '';
        if (streaming.el === undefined) {
          const box = $('#asMessages');
          streaming.el = box ? box.querySelector('.as-msg.them:last-child .as-body') : null;
        }
        if (streaming.el) {
          // Markdown re-renders at most once per frame, the way the main chat
          // throttles its own stream; the raw text rides on the element so a
          // pending frame never loses a delta.
          streaming.el._raw = streaming.text;
          if (!streaming.raf) {
            streaming.raf = requestAnimationFrame(() => {
              streaming.raf = 0;
              if (streaming && streaming.el) {
                streaming.el.innerHTML = renderMd(streaming.el._raw || '');
                const box = $('#asMessages');
                if (box) box.scrollTop = box.scrollHeight;
              }
            });
          }
        }
        break;
      }
      case 'assistant_tool_start':
        toast('assistant: ' + m.name);
        break;
      case 'assistant_tool_end':
        break;
      case 'assistant_error':
        finishStream();
        toast('Assistant: ' + (m.message || 'failed'), true);
        busy = false; setBusy(false); streaming = null;
        break;
      case 'assistant_saved':
        finishStream();
        busy = false; setBusy(false); streaming = null;
        if (m.preview) preview = m.preview;
        load();
        break;
      case 'assistant_cleared':
        messages = [];
        if (m.preview) preview = m.preview;
        renderMessages(); renderContext();
        break;
      case 'assistant_settings':
        settings = m.settings; preview = m.preview;
        renderAll();
        break;
      case 'assistant_done':
        finishStream();
        streaming = null;
        break;
      default:
        break;
    }
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  function submit() {
    const box = $('#asInput');
    if (!box) return;
    const text = box.value.trim();
    if (!text || busy || !sid) return;
    messages.push({ role: 'user', content: text });
    renderMessages();
    box.value = '';
    box.style.height = 'auto';
    busy = true;
    setBusy(true);
    send({ type: 'assistant_prompt', message: text });
  }

  function install() {
    const panel = $('#asPanel');
    if (!panel) return;

    $('#asSend').addEventListener('click', submit);
    $('#asInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
    });
    $('#asInput').addEventListener('input', (e) => {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px';
    });
    $('#asClear').addEventListener('click', async () => {
      if (!confirm('Clear this session\'s assistant conversation?')) return;
      send({ type: 'assistant_clear' });
    });
    $('#asToggle').addEventListener('click', togglePanel);
    $('#asClose').addEventListener('click', togglePanel);
    $('#asSettingsBtn').addEventListener('click', () => {
      const box = $('#asSettingsWrap');
      if (box) box.hidden = !box.hidden;
    });
  }

  function togglePanel() {
    const panel = $('#asPanel');
    if (!panel) return;
    panel.classList.toggle('open');
    const open = panel.classList.contains('open');
    try { localStorage.setItem('tacit.assistantOpen', open ? '1' : '0'); } catch (e) { /* ignore */ }
    const btn = $('#asToggle');
    if (btn) btn.classList.toggle('on', open);
    shiftMain(open);
    if (open) load();
  }

  // The panel is a flex sibling of the main column, so the layout does the work
  // and nothing has to be nudged or overlapped.
  function shiftMain() { /* layout handles it */ }

  install();
  try {
    if (localStorage.getItem('tacit.assistantOpen') === '1') {
      const panel = $('#asPanel');
      if (panel) {
        panel.classList.add('open');
        const btn = $('#asToggle');
        if (btn) btn.classList.add('on');
        shiftMain(true);
      }
    }
  } catch (e) { /* ignore */ }

  window.TacitAssistant = { handle, sessionChanged, load, togglePanel };
})();