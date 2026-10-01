/* Tacit Web — Git panel.
 *
 * Manual version control for the operator. The LLM can never run git (the
 * extensions/git-guard veto blocks every shell tool that mentions it); this
 * panel is the human's surface: status, diffs, commits, branches, remotes,
 * and a git/gh console. Everything goes through /api/git/* on the Node host,
 * which runs git directly in the current session's working directory.
 */
(() => {
'use strict';

const $ = s => document.querySelector(s);
const overlay = $('#gitOverlay');
if (!overlay) return;

let state = { workdir: '', root: '', isRepo: false, status: null, branches: [], remotes: [], log: [] };
let consoleLines = [];
const MAX_CONSOLE_LINES = 400;
let busy = false;
let wdOverride = '';   // panel-scoped project folder (any repo), blank = session folder
let projects = [];      // recent workspaces only — no assumed project layout

const esc = t => String(t == null ? '' : t)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function workdir() {
  return wdOverride || (window.Tacit && window.Tacit.getWorkdir && window.Tacit.getWorkdir()) || '';
}
function sid() {
  return (window.Tacit && window.Tacit.getSid && window.Tacit.getSid()) || '';
}
function qs(extra = {}) {
  const p = new URLSearchParams();
  const w = workdir(); if (w) p.set('workdir', w);
  const s = sid(); if (s) p.set('sid', s);
  for (const [k, v] of Object.entries(extra)) if (v != null) p.set(k, v);
  return p.toString();
}
async function api(path, extra = {}) {
  const r = await fetch(path, extra);
  try { return await r.json(); } catch { return { ok: false, error: 'bad response' }; }
}
function note(t, err) {
  const el = $('#gitNote');
  el.textContent = t || '';
  el.classList.toggle('err', !!err);
}

// ── console ────────────────────────────────────────────────────────────────
function logLine(text) {
  consoleLines.push(text);
  if (consoleLines.length > MAX_CONSOLE_LINES) consoleLines = consoleLines.slice(-MAX_CONSOLE_LINES);
  renderConsole();
}
function renderConsole() {
  const el = $('#gitOut');
  if (!el) return;
  el.textContent = consoleLines.join('\n');
  el.scrollTop = el.scrollHeight;
}
async function runCommand(cmd, { silent = false } = {}) {
  cmd = String(cmd || '').trim();
  if (!cmd) return { ok: false };
  logLine('$ ' + cmd);
  const r = await api('/api/git/run', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command: cmd, workdir: workdir(), sid: sid() }),
  });
  const out = (r.output || r.error || '').trim();
  if (out) logLine(out);
  if (!r.ok && r.error) logLine('! ' + r.error);
  if (!r.ok && r.needs_identity) {
    // a commit was refused because there is no identity — send them to the field
    const field = $('#gitIdName');
    if (field) field.focus();
  }
  if (!silent) note(r.ok ? cmd + ' — ok' : (r.error || 'command failed'), !r.ok);
  return r;
}

// ── rendering ──────────────────────────────────────────────────────────────
function renderRepo() {
  const repoEl = $('#gitRepo');
  repoEl.textContent = state.isRepo ? (state.root || state.workdir) : (state.workdir + '  (not a repository)');
  repoEl.title = state.isRepo ? state.root : state.workdir;
  const wdInput = $('#gitWorkdir');
  if (wdInput && document.activeElement !== wdInput) wdInput.value = state.workdir || state.root || '';
}

function renderBranchBar() {
  const el = $('#gitBranchBar');
  if (!state.isRepo) {
    el.innerHTML = `<button class="ho-btn primary" id="gitInitBtn">Initialize repository (git init)</button>`;
    $('#gitInitBtn').addEventListener('click', () => runCommand('git init').then(refresh));
    return;
  }
  const st = state.status || { branch: '', ahead: 0, behind: 0, upstream: '' };
  const local = state.branches || [];
  const options = local.map(b => `<option value="${esc(b)}"${b === st.branch ? ' selected' : ''}>${esc(b)}</option>`).join('');
  const track = st.upstream
    ? `<span class="git-track">↑${st.ahead} ↓${st.behind} · ${esc(st.upstream)}</span>`
    : `<span class="git-track dim">no upstream</span>`;
  el.innerHTML = `
    <div class="git-current"><span class="git-branch-icon">⎇</span><strong>${esc(st.branch || 'HEAD')}</strong>${track}</div>
    <div class="git-branch-row">
      <select id="gitBranchSelect" class="git-select">${options || '<option value="">(no branches)</option>'}</select>
      <button class="ho-btn small" id="gitCheckoutBtn">Checkout</button>
      <button class="ho-btn small" id="gitNewBranchBtn">New branch</button>
    </div>`;
  $('#gitCheckoutBtn').addEventListener('click', async () => {
    const b = $('#gitBranchSelect').value;
    if (b && b !== st.branch) { await runCommand(`git checkout ${b}`); refresh(); }
  });
  $('#gitNewBranchBtn').addEventListener('click', async () => {
    const name = prompt('New branch name:');
    if (name && name.trim()) { await runCommand(`git checkout -b ${name.trim()}`); refresh(); }
  });
}

function statusGlyph(f) {
  if (f.untracked) return { g: 'U', cls: 'u' };
  if (f.index === 'A' || f.worktree === 'A') return { g: 'A', cls: 'a' };
  if (f.index === 'D' || f.worktree === 'D') return { g: 'D', cls: 'd' };
  if (f.index === 'R' || f.worktree === 'R') return { g: 'R', cls: 'r' };
  return { g: 'M', cls: 'm' };
}

function renderChanges() {
  const el = $('#gitChanges');
  const files = (state.status && state.status.files) || [];
  $('#gitChangeCount').textContent = files.length;
  if (!files.length) { el.innerHTML = '<div class="ho-empty sm">working tree clean</div>'; return; }
  el.innerHTML = files.map((f, i) => {
    const { g, cls } = statusGlyph(f);
    const staged = f.index !== ' ' && f.index !== '?';
    const action = staged
      ? `<button class="git-file-btn" data-i="${i}" data-act="unstage" title="Unstage">–</button>`
      : `<button class="git-file-btn" data-i="${i}" data-act="stage" title="Stage">+</button>`;
    return `<div class="git-file"><span class="git-glyph ${cls}">${g}</span>
      <span class="git-file-path" data-i="${i}" data-act="diff" title="${esc(f.path)}">${esc(f.path)}</span>
      ${action}</div>`;
  }).join('');
  el.querySelectorAll('[data-act]').forEach(node => node.addEventListener('click', async (e) => {
    e.stopPropagation();
    const f = files[parseInt(node.dataset.i, 10)];
    if (!f) return;
    const act = node.dataset.act;
    if (act === 'diff') { await showDiff(f); return; }
    if (act === 'stage') await runCommand(`git add -- ${quotePath(f.path)}`);
    else await runCommand(`git restore --staged -- ${quotePath(f.path)}`);
    refresh();
  }));
}

function quotePath(p) {
  return /[\s"]/.test(p) ? '"' + p.replace(/"/g, '\\"') + '"' : p;
}

async function showDiff(f) {
  const staged = f.index !== ' ' && f.index !== '?';
  const d = await api('/api/git/diff?' + qs({ path: f.path, staged: staged ? '1' : '' }));
  logLine('$ git diff' + (staged ? ' --cached' : '') + ' -- ' + f.path);
  logLine((d.diff || '(no diff)').trim());
}

function renderRemotes() {
  const el = $('#gitRemotes');
  const remotes = state.remotes || [];
  el.innerHTML = remotes.map(r => `<div class="git-remote"><strong>${esc(r.name)}</strong><code>${esc(r.url)}</code></div>`).join('')
    || '<div class="ho-empty sm">no remotes</div>';
  el.innerHTML += `
    <div class="git-remote-add">
      <input id="gitRemoteUrl" placeholder="https://github.com/user/repo.git" spellcheck="false">
      <button class="ho-btn small" id="gitRemoteAddBtn">Add origin</button>
    </div>`;
  $('#gitRemoteAddBtn').addEventListener('click', async () => {
    const u = $('#gitRemoteUrl').value.trim();
    if (!u) return;
    const name = remotes.some(r => r.name === 'origin') ? 'upstream' : 'origin';
    await runCommand(`git remote add ${name} ${u}`);
    refresh();
  });
}

function renderLog() {
  const el = $('#gitLog');
  const commits = state.log || [];
  if (!commits.length) { el.innerHTML = '<div class="ho-empty sm">no commits yet</div>'; return; }
  el.innerHTML = commits.map(c => `
    <div class="git-commit-row">
      <code class="git-hash">${esc(c.short)}</code>
      <span class="git-subject" title="${esc(c.subject)}">${esc(c.subject)}</span>
      ${c.refs ? `<span class="git-ref">${esc(c.refs)}</span>` : ''}
      <span class="git-meta">${esc(c.author)} · ${esc((c.date || '').replace('T', ' ').slice(0, 16))}</span>
    </div>`).join('');
}

let idOptions = [];

async function refreshIdentity() {
  const sel = $('#gitIdSelect');
  const status = $('#gitIdentity');
  if (!sel || !status) return;
  const r = await api('/api/git/identity?' + qs());
  if (!r.ok) { status.textContent = r.error || 'unknown'; return; }

  const label = (i, tag) => `${i.name} <${i.email}> — ${tag}`;
  idOptions = [];
  if (r.repository) idOptions.push({ text: label(r.repository, 'repository config'), ident: r.repository });
  if (r.settings) idOptions.push({ text: label(r.settings, 'saved here'), ident: r.settings });
  if (r.github) idOptions.push({ text: label(r.github, 'GitHub account'), ident: r.github });
  if (r.environment) idOptions.push({ text: label(r.environment, 'environment'), ident: r.environment });

  const cur = r.effective;
  const same = (a, b) => a && b && a.name === b.name && a.email === b.email;
  sel.innerHTML = idOptions.map((o, i) =>
    `<option value="${i}"${same(o.ident, cur) ? ' selected' : ''}>${esc(o.text)}</option>`
  ).join('') + `<option value="custom"${cur ? '' : ' selected'}>Write my own…</option>`;

  if (cur) {
    status.innerHTML = `<span class="git-badge repo">${esc(cur.name)}</span>` +
      `<span class="git-badge">${esc(cur.email)}</span>` +
      `<span class="git-badge">from ${esc(cur.source)}</span>`;
  } else {
    status.innerHTML = '<span class="git-badge none">none set — commits will be refused until you pick one</span>';
  }
  // keep the custom fields showing whatever is saved, not a stale value
  if (r.settings) {
    const n = $('#gitIdName'), e = $('#gitIdEmail');
    if (n) n.value = r.settings.name;
    if (e) e.value = r.settings.email;
  }
  toggleIdentityCustom();
}

function toggleIdentityCustom() {
  const sel = $('#gitIdSelect');
  const box = $('#gitIdCustom');
  if (sel && box) box.hidden = sel.value !== 'custom';
}

function renderAll() {
  renderRepo();
  renderBranchBar();
  renderChanges();
  renderRemotes();
  renderLog();
  renderProjectStatus();
}

// ── data loading ───────────────────────────────────────────────────────────
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const st = await api('/api/git/status?' + qs());
    state.isRepo = !!st.isRepo;
    state.root = st.root || '';
    state.workdir = st.workdir || workdir();
    state.status = st.status || null;
    state.remotes = st.remotes || [];
    if (state.isRepo) {
      const [lg, br] = await Promise.all([
        api('/api/git/log?' + qs({ limit: '40' })),
        api('/api/git/branches?' + qs()),
      ]);
      state.log = (lg.ok && lg.commits) || [];
      state.branches = br.local || [];
    } else {
      state.log = []; state.branches = [];
    }
    renderAll();
    refreshIdentity();
    if (st.isRepo === false && st.error) note(st.error, false);
  } finally {
    busy = false;
  }
}

// ── Projects: every folder under the discovered project roots ──────────────
async function loadProjects() {
  // Only used to enrich the workspace status badge (repo / branch / GitHub).
  // There is no workspace *picker* here any more: the row below browses the
  // filesystem directly instead of offering a guessed list.
  const r = await api('/api/projects?full=1');
  projects = (r.ok && r.projects) || [];
  renderProjectStatus();
}

/** GitHub owner/name for the current repo. The repo's own remotes are the
 *  ground truth (they come back with the status call); the enriched recent
 *  workspaces list is only a fallback, so the badge never depends on the
 *  workspace happening to be in that list. */
function ghFullName() {
  const re = /^(?:[a-z][a-z0-9+.-]*:\/\/)?(?:[^@/\s]+@)?github\.com[/:]([\w.-]+)\/([\w.-]+?)(?:\.git)?\/?$/i;
  for (const r of (state.remotes || [])) {
    const m = re.exec(String(r.url || '').trim());
    if (m) return `${m[1]}/${m[2]}`;
  }
  const wd = workdir();
  const proj = projects.find(x => x.path === wd) || projects.find(x => x.path === state.root);
  return (proj && proj.github_full_name) || '';
}

function renderProjectStatus() {
  const el = $('#gitProjectStatus');
  if (!el) return;
  if (!state.isRepo) {
    el.innerHTML = `<span class="git-badge none">not a repository — Initialize to start version control</span>`;
    return;
  }
  const gh = ghFullName();
  const parts = [`<span class="git-badge repo">✓ repository</span>`];
  if (state.status && state.status.branch) parts.push(`<span class="git-badge branch">⎇ ${esc(state.status.branch)}</span>`);
  if (gh) {
    parts.push(`<span class="git-badge gh" id="gitProjVis" data-repo="${esc(gh)}">checking…</span>`);
    parts.push(`<a class="git-badge link" href="https://github.com/${esc(gh)}" target="_blank" rel="noopener">${esc(gh)} ↗</a>`);
  } else {
    parts.push(`<span class="git-badge none">local only — not on GitHub</span>`);
  }
  el.innerHTML = parts.join(' ');
  const vis = $('#gitProjVis');
  if (vis) loadVisibility(vis.dataset.repo, vis);
}

async function loadVisibility(repo, el) {
  const r = await api('/api/github/repo?repo=' + encodeURIComponent(repo));
  if (!el || !el.isConnected) return;
  if (r.found) {
    el.textContent = (r.private ? 'private' : 'public') + (r.fork ? ' · fork' : '');
    el.classList.add(r.private ? 'priv' : 'pub');
  } else {
    el.textContent = r.error || 'not found on GitHub';
    el.classList.add('none');
  }
}

async function createRepo() {
  const wd = workdir();
  if (!wd) { note('select a project first', true); return; }
  const nameEl = $('#gitGhNewName');
  const base = (state.root || wd).split(/[\\/]/).filter(Boolean).pop() || '';
  const name = ((nameEl && nameEl.value.trim()) || base).replace(/\s+/g, '-');
  if (!name) { note('repository name required', true); return; }
  const vis = ($('#gitGhNewVis') && $('#gitGhNewVis').value) || 'private';
  const fresh = !!($('#gitGhFresh') && $('#gitGhFresh').checked);
  if (fresh && !confirm('Fresh start: replace the local branch history with a single "Initial commit" and force-push it?\nThe files are kept; the old commits are dropped.')) return;
  note('creating GitHub repository ' + name + ' (' + vis + ')…');
  const r = await api('/api/github/create', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, private: vis === 'private', recreate: fresh }),
  });
  if (!r.ok) { note(r.error || 'create failed', true); return; }
  logLine(`$ gh repo create ${name} --${vis}${fresh ? '  (recreating, fresh history)' : ''}`);
  logLine(`created ${r.full_name} (${r.private ? 'private' : 'public'})  ${r.html_url}`);
  if (nameEl) nameEl.value = '';
  if (!state.isRepo) { await runCommand('git init'); }
  const hasOrigin = (state.remotes || []).some(x => x.name === 'origin');
  await runCommand(`git remote ${hasOrigin ? 'set-url' : 'add'} origin ${r.clone_url}`);
  if (fresh) {
    await freshHistoryPush(r.default_branch || 'main');
  } else {
    if (!state.isRepo) await runCommand('git branch -M main');
    await runCommand('git add -A');
    await runCommand('git commit -m "Initial commit"', { silent: true });
    await runCommand(`git push -u origin ${(state.status && state.status.branch) || 'main'}`);
  }
  await refresh();
  await loadProjects();
  note('created and pushed ' + r.full_name);
}

/** Rebuild the current working tree as one commit on `branch` and force-push
 * it, dropping the previous history — the case where a past agent committed
 * non-stop and the operator wants a clean re-publish. Files are preserved. */
async function freshHistoryPush(branch) {
  const tmp = 'tacit-fresh-start';
  note('building a fresh single-commit history…');
  await runCommand(`git checkout --orphan ${tmp}`);
  await runCommand('git add -A');
  const c = await runCommand('git commit -m "Initial commit"');
  if (!c.ok) { note('fresh commit failed — nothing to commit?', true); return; }
  await runCommand(`git branch -M ${branch}`);
  await runCommand(`git push -u origin ${branch} --force`);
}

/** Delete the GitHub repository backing the selected project (needs a token
 * with the `delete_repo` scope). Lets a project be re-published in one step. */
async function deleteRepo() {
  const full = ghFullName();
  if (!full) { note('this project has no GitHub remote to delete', true); return; }
  if (!confirm('Delete the GitHub repository ' + full + '?\nThis cannot be undone.')) return;
  const r = await api('/api/github/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo: full }),
  });
  if (!r.ok) { note(r.error || 'delete failed', true); return; }
  logLine(`$ gh repo delete ${full} --yes`);
  logLine(r.deleted ? 'deleted ' + full : full + ' did not exist');
  note('deleted ' + full);
  refreshGithub();
  refresh();
}

// ── top actions ────────────────────────────────────────────────────────────
async function remoteAndBranch() {
  const st = state.status || {};
  const remotes = state.remotes || [];
  const remote = remotes[0] ? remotes[0].name : 'origin';
  const branch = st.branch || 'main';
  return { remote, branch, hasUpstream: !!st.upstream, hasRemote: remotes.length > 0 };
}

/** Pull with rebase and autostash: keeps a linear history, and a dirty working
 * tree or a branch that moved on GitHub does not block the operator. */
async function pullRemote() {
  const { remote, branch, hasUpstream } = await remoteAndBranch();
  return runCommand(hasUpstream
    ? 'git pull --rebase --autostash'
    : `git pull --rebase --autostash ${remote} ${branch}`);
}

/** Push, self-healing: if the remote moved (e.g. a file edited on github.com)
 * the plain push is rejected, so integrate it with a rebase pull and retry once. */
async function pushRemote() {
  const { remote, branch, hasUpstream, hasRemote } = await remoteAndBranch();
  if (!hasRemote) { note('no remote configured — add one below first', true); return { ok: false }; }
  const push = () => runCommand(hasUpstream ? 'git push' : `git push -u ${remote} ${branch}`);
  let r = await push();
  if (!r.ok && /rejected|fetch first|non-fast-forward|behind/i.test((r.output || '') + ' ' + (r.error || ''))) {
    note('the remote has changes you do not have — pulling (rebase) then pushing…');
    const p = await pullRemote();
    if (!p.ok) { note('pull stopped (conflict?) — fix it in the console, then push again', true); return p; }
    r = await push();
  }
  return r;
}

// ── GitHub: the operator's account, reused from CMD, general to any project ─
let ghRepos = [];
function renderGhAccount(a) {
  const el = $('#gitGhAccount');
  if (!el) return;
  if (a && a.connected) {
    const who = a.name ? `${a.login} (${a.name})` : a.login;
    el.innerHTML = `<span class="git-gh-dot ok"></span>connected as <strong>${esc(who)}</strong>`
      + (a.source === 'credential-manager' ? ' <span class="git-gh-src">via CMD</span>' : ' <span class="git-gh-src">saved token</span>')
      + (a.avatar ? ` <img class="git-gh-av" src="${esc(a.avatar)}" alt="">` : '');
  } else {
    el.innerHTML = `<span class="git-gh-dot off"></span>not connected`
      + (a && a.error ? ` — <span class="git-gh-err">${esc(a.error)}</span>` : '');
  }
}
function renderGhList() {
  const dl = $('#gitGhRepoList');
  if (dl) dl.innerHTML = ghRepos.map(r => `<option value="${esc(r.full_name)}"></option>`).join('');
  const el = $('#gitGhList');
  if (!el) return;
  if (!ghRepos.length) { el.innerHTML = ''; return; }
  el.innerHTML = ghRepos.slice(0, 200).map(r =>
    `<div class="git-gh-repo"><span class="git-gh-repo-name">${esc(r.full_name)}`
    + `${r.private ? ' <span class="git-gh-priv">private</span>' : ''}</span>`
    + `<button class="ho-btn small" data-repo="${esc(r.full_name)}">Clone</button></div>`).join('');
  el.querySelectorAll('button[data-repo]').forEach(b =>
    b.addEventListener('click', () => cloneRepo(b.dataset.repo)));
}
async function loadGhRepos() {
  const r = await api('/api/github/repos');
  ghRepos = (r.ok && r.repos) || [];
  renderGhList();
}
async function refreshGithub() {
  const a = await api('/api/github/account');
  renderGhAccount(a);
  if (a && a.connected) loadGhRepos();
  else { ghRepos = []; renderGhList(); }
}
async function cloneRepo(repo) {
  repo = String(repo || ($('#gitGhRepo') && $('#gitGhRepo').value) || '').trim();
  if (!repo) { note('type owner/repo to clone', true); return; }
  const dir = ($('#gitGhDir') && $('#gitGhDir').value.trim()) || '';
  note('cloning ' + repo + '…');
  const r = await api('/api/github/clone', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo, dir }),
  });
  logLine('$ ' + (r.command || ('git clone ' + repo)));
  if (r.output) logLine(r.output);
  if (!r.ok) { note(r.error || 'clone failed', true); return; }
  wdOverride = r.target;
  if ($('#gitWorkdir')) $('#gitWorkdir').value = r.target;
  note('cloned → ' + r.target);
  refresh();
}
function wireGithub() {
  const conn = $('#gitGhConnect');
  if (conn) conn.addEventListener('click', async () => {
    const token = ($('#gitGhToken').value || '').trim();
    if (!token) { note('paste a token first', true); return; }
    const r = await api('/api/github/account', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    if (!r.ok) { note(r.error || 'invalid token', true); return; }
    $('#gitGhToken').value = '';
    note('token saved — connected as ' + r.login);
    refreshGithub();
  });
  const out = $('#gitGhSignout');
  if (out) out.addEventListener('click', async () => {
    await api('/api/github/account', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true }),
    });
    note('signed out — falls back to the credential manager');
    refreshGithub();
  });
  const cl = $('#gitGhClone');
  if (cl) cl.addEventListener('click', () => cloneRepo());
  const wdBtn = $('#gitWorkdirOpen');
  if (wdBtn) wdBtn.addEventListener('click', () => {
    wdOverride = ($('#gitWorkdir').value || '').trim();
    note(wdOverride ? ('opened ' + wdOverride) : 'using the session folder');
    refresh();
  });
  const wdInput = $('#gitWorkdir');
  if (wdInput) wdInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); wdOverride = wdInput.value.trim(); refresh(); }
  });
  const wdBrowse = $('#gitWorkdirBrowse');
  if (wdBrowse) wdBrowse.addEventListener('click', () => {
    window.Tacit.pickDir({
      key: 'workspace',
      start: (wdInput && wdInput.value.trim()) || workdir(),
      title: 'Choose workspace',
      onPick: (p) => {
        wdOverride = p;
        if (wdInput) wdInput.value = p;
        note('workspace → ' + p);
        refresh();
      },
    });
  });
  const ghDirBrowse = $('#gitGhDirBrowse');
  if (ghDirBrowse) ghDirBrowse.addEventListener('click', () => {
    window.Tacit.pickDir({
      key: 'clone',
      start: ($('#gitGhDir') && $('#gitGhDir').value.trim()) || '',
      title: 'Clone into folder',
      onPick: (p) => {
        if ($('#gitGhDir')) $('#gitGhDir').value = p;
        note('clone into ' + p);
      },
    });
  });
  const idSel = $('#gitIdSelect');
  if (idSel) idSel.addEventListener('change', toggleIdentityCustom);

  const idSave = $('#gitIdSave');
  if (idSave) idSave.addEventListener('click', async () => {
    let payload;
    if (!idSel || idSel.value === 'custom') {
      payload = { name: ($('#gitIdName').value || '').trim(),
                  email: ($('#gitIdEmail').value || '').trim() };
    } else {
      const opt = idOptions[parseInt(idSel.value, 10)];
      if (!opt) { note('nothing selected', true); return; }
      payload = { name: opt.ident.name, email: opt.ident.email };
    }
    const r = await api('/api/git/identity', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) { note(r.error || 'could not save the identity', true); return; }
    note('commits will be made as ' + r.identity.name);
    refreshIdentity();
  });

  const createBtn = $('#gitGhCreate');
  if (createBtn) createBtn.addEventListener('click', createRepo);
  const delGitBtn = $('#gitGhDelete');
  if (delGitBtn) delGitBtn.addEventListener('click', deleteRepo);
}

function wireActions() {
  $('#gitRefreshBtn').addEventListener('click', () => { note('refreshing…'); refresh(); });
  $('#gitFetchBtn').addEventListener('click', async () => { await runCommand('git fetch --all --prune'); refresh(); });
  $('#gitPullBtn').addEventListener('click', async () => { await pullRemote(); refresh(); });
  $('#gitPushBtn').addEventListener('click', async () => { await pushRemote(); refresh(); });
  const syncBtn = $('#gitSyncBtn');
  if (syncBtn) syncBtn.addEventListener('click', async () => {
    note('syncing — pulling the remote changes, then pushing yours…');
    const p = await pullRemote();
    if (!p.ok) { note('pull stopped — resolve it in the console, then Sync again', true); refresh(); return; }
    await pushRemote();
    refresh();
  });
  $('#gitStageAllBtn').addEventListener('click', async () => { await runCommand('git add -A'); refresh(); });
  $('#gitCommitBtn').addEventListener('click', async () => {
    const msg = $('#gitCommitMsg').value.trim();
    if (!msg) { note('write a commit message first', true); return; }
    await runCommand('git commit -m "' + msg.replace(/"/g, '\\"') + '"');
    $('#gitCommitMsg').value = '';
    refresh();
  });
  $('#gitRunBtn').addEventListener('click', async () => { await runCommand($('#gitCmd').value); refresh(); });
  $('#gitCmd').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runCommand($('#gitCmd').value).then(refresh); }
  });
  $('#gitClose').addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
}

// ── open / close ───────────────────────────────────────────────────────────
function open() {
  overlay.hidden = false;
  note('');
  $('#gitRepo').textContent = workdir() || '—';
  loadProjects();
  refresh();
  refreshGithub();
}
function close() { overlay.hidden = true; }

const openBtn = $('#gitBtn');
if (openBtn) openBtn.addEventListener('click', open);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !overlay.hidden) close();
});

wireActions();
wireGithub();
window.TacitGit = { open, close, refresh, run: runCommand };
})();
