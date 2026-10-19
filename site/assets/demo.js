/*
 * The interface mock: a Tacit turn, played out on the landing page.
 *
 * Nothing here talks to a server and nothing here is a screenshot. The rows are the
 * shapes the real transcript builds (a user turn, a thinking block, a tool card with
 * a head and a body, an assistant reply) and the content is a plausible turn about a
 * defect this harness actually had: a shell timeout that killed the wrapper and left
 * the command holding the pipe. So a visitor can see what using Tacit looks like,
 * including the parts a static page usually hides, that the tool cards open, that the
 * Assistant is a second conversation the agent cannot see, and that the cost is
 * counted while the turn runs rather than in a report afterwards.
 *
 * The sequence is data. `render()` walks it and appends; `play()` does the same with
 * delays between the steps. Reduced motion gets the finished transcript at once,
 * because a demo that will not animate should not animate at all.
 */

(function () {
  const root = document.getElementById('demo');
  if (!root) return;

  const transcript = root.querySelector('#demo-transcript');
  const rail = root.querySelector('#demo-assistant');
  const strip = root.querySelector('#demo-strip');
  const meterFill = root.querySelector('#demo-meter i');
  const meterText = root.querySelector('#demo-tokens');
  const status = root.querySelector('#demo-status');
  const playBtn = root.querySelector('#demo-play');

  const reduce = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Tokens billed by the end of the turn, and where each step sits on the way there.
  const WINDOW = 128000;

  const STEPS = [
    {
      kind: 'user',
      text: 'run_shell reports a timeout at 180s but the command is still running five '
          + 'minutes later. Fix it, then prove it.',
      tokens: 41208,
    },
    {
      kind: 'reason',
      text: 'Two things are wrong. Killing the `cmd /c` wrapper leaves its grandchildren '
          + 'alive, and they still hold the captured pipe, so the read blocks after the '
          + 'timeout has fired. The kill has to take the tree, and the drain after it has '
          + 'to be bounded.',
      tokens: 41890,
    },
    {
      kind: 'tool', name: 'read_file', ms: '0.4s',
      args: '{"path": "backend/agent.py", "offset": 309, "limit": 64}',
      body: '310\tdef t_run_shell(command: str, project: str | None = None,\n'
          + '311\t                timeout: int | None = None, session: str = "") -> str:\n'
          + '312\t    root = _root(project, session)\n'
          + '313\t    limit = timeout or config.SHELL_TIMEOUT\n'
          + '314\t    try:\n'
          + '315\t        proc = subprocess.Popen(command, shell=True, cwd=str(root),\n'
          + '316\t                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)\n'
          + '317\t        out, _ = proc.communicate(timeout=limit)\n'
          + '318\t    except subprocess.TimeoutExpired:\n'
          + '319\t        proc.kill()                      # the wrapper only\n'
          + '320\t        out, _ = proc.communicate()      # and this can block forever\n'
          + '\nlines 310-320 of 2469 shown; pass offset=373 for the next 2096',
      tokens: 44310,
    },
    {
      kind: 'tool', name: 'grep_files', ms: '0.2s',
      args: '{"pattern": "kill|killpg|taskkill", "path": "backend/proctools.py"}',
      body: 'backend/proctools.py:118:  """Kill a pid and everything it spawned."""\n'
          + 'backend/proctools.py:124:        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],\n'
          + 'backend/proctools.py:127:        os.killpg(os.getpgid(pid), signal.SIGKILL)\n'
          + '\n3 matches in 1 file',
      tokens: 45980,
    },
    {
      kind: 'tool', name: 'edit_file', ms: '0.1s',
      args: '{"path": "backend/agent.py", "find": "        proc.kill()  '
          + '# the wrapper only", "replace": "        proctools.kill_tree(proc.pid)"}',
      body: '- 319          proc.kill()                      # the wrapper only\n'
          + '+ 319          proctools.kill_tree(proc.pid)     # the wrapper and its children\n'
          + '- 320          out, _ = proc.communicate()      # and this can block forever\n'
          + '+ 320          out, _ = _drain_bounded(proc, DRAIN_S)\n'
          + '+ 321          note = f" (killed after {limit}s; {len(out)} chars captured first)"',
      tokens: 47402,
    },
    {
      kind: 'tool', name: 'run_shell', ms: '4.3s', ok: true,
      args: '{"command": ".venv\\\\Scripts\\\\python.exe -m unittest '
          + 'tests.test_shell_timeout", "timeout": 60}',
      body: 'exit code 0\n'
          + 'a command that outlives its timeout returned in 4.11s, not 183.4s\n'
          + '----------------------------------------------------------------------\n'
          + 'Ran 3 tests in 12.884s\n'
          + '\nOK',
      tokens: 50115,
    },
    {
      kind: 'notify', level: 'warn',
      text: 'A snapshot was taken before the first edit of this turn: 141 files, '
          + 'backend/agent.py changed since.',
      tokens: 50480,
    },
    {
      kind: 'text',
      text: 'Fixed. `t_run_shell` now kills the whole process tree through '
          + '`proctools.kill_tree`, which is `taskkill /T /F` on Windows and `killpg` '
          + 'elsewhere, and the read after the kill is bounded by `DRAIN_S` so a '
          + 'grandchild holding the pipe cannot stall the call. The result says how '
          + 'long it actually waited and how much it had already printed, so a '
          + 'timeout no longer reads as a hung no-op.\n\n'
          + '`tests/test_shell_timeout.py` covers it: a three-second timeout on a '
          + 'command that would run for a minute returns in about four seconds with '
          + 'exit 124 and no surviving child.',
      tokens: 52840,
      done: true,
    },
  ];

  // The Assistant's own conversation. It is not the agent's: the agent never sees it.
  const ASSISTANT = [
    { from: 'you', text: 'Is this the kind of change that needs a plan first?' },
    { from: 'it', text: 'No. One call site, one guard, and a test that already names '
        + 'the symptom. The risk is not the edit, it is that `kill_tree` on Windows '
        + 'goes through `taskkill`, which fails silently if the pid is already gone. '
        + 'Worth one look at the branch for a dead child.' },
    { from: 'you', text: 'Draft the prompt for that look.' },
    { from: 'it', text: 'After the tree kill, prove the child is gone: enumerate the '
        + 'pid and its descendants, assert none are alive, and report what the '
        + 'command had already written before it died. Keep it inside '
        + 'tests/test_shell_timeout.py.' },
  ];

  function el(cls, html) {
    const n = document.createElement('div');
    n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function setMeter(step) {
    const used = step.tokens || 0;
    meterFill.style.width = Math.min(100, (used / WINDOW) * 100).toFixed(1) + '%';
    meterText.textContent = used.toLocaleString() + ' / ' + WINDOW.toLocaleString();
  }

  function row(step) {
    if (step.kind === 'user') {
      const r = el('d-msg d-user');
      r.appendChild(el('d-who', 'You'));
      r.appendChild(el('d-body', step.text));
      return r;
    }
    if (step.kind === 'reason') {
      const r = el('d-reason');
      r.innerHTML = '<button class="d-reason-head" type="button">'
        + '<span class="d-caret"></span>Thought for 3s</button>'
        + '<div class="d-reason-body"></div>';
      r.querySelector('.d-reason-body').textContent = step.text;
      return r;
    }
    if (step.kind === 'notify') {
      return el('d-notify d-' + step.level, step.text);
    }
    if (step.kind === 'tool') {
      const card = el('d-card');
      const head = document.createElement('button');
      head.type = 'button';
      head.className = 'd-card-head';
      head.innerHTML = '<span class="d-tool-badge">' + step.name + '</span>'
        + '<span class="d-tool-args"></span>'
        + '<span class="d-tool-ms">' + step.ms + '</span>'
        + '<span class="d-tool-state">done</span>';
      head.querySelector('.d-tool-args').textContent = step.args;
      const body = document.createElement('pre');
      body.className = 'd-card-body';
      body.textContent = step.body;
      card.appendChild(head);
      card.appendChild(body);
      return card;
    }
    const r = el('d-msg d-agent');
    r.appendChild(el('d-who', 'Tacit'));
    const b = el('d-body');
    b.textContent = step.text;
    r.appendChild(b);
    return r;
  }

  // One delegated click handler, the same shape the real transcript uses: a card
  // does not own its expander, so a turn with forty cards costs one listener.
  transcript.addEventListener('click', (e) => {
    const head = e.target.closest('.d-card-head, .d-reason-head');
    if (!head) return;
    head.parentElement.classList.toggle('open');
  });

  function finish() {
    transcript.innerHTML = '';
    STEPS.forEach((s) => transcript.appendChild(row(s)));
    setMeter(STEPS[STEPS.length - 1]);
    strip.innerHTML = '';
    ['Read the kill path', 'Kill the tree, bound the drain',
     'Run the test that names the symptom', 'Report what it waited'].forEach((t, i) => {
      const li = el('d-strip-item' + (i === 3 ? '' : ' done'), t);
      strip.appendChild(li);
    });
    rail.innerHTML = '';
    ASSISTANT.forEach((m) => {
      const r = el('d-as-msg d-as-' + (m.from === 'you' ? 'you' : 'it'));
      r.appendChild(el('d-who', m.from === 'you' ? 'You' : 'Assistant'));
      r.appendChild(el('d-body', m.text));
      rail.appendChild(r);
    });
    rail.appendChild(el('d-as-cost',
      '~182 tokens of session context · you 20 · persona 162 · history 0 · tools 0'));
    status.textContent = 'idle';
    status.classList.remove('busy');
  }

  function play() {
    if (reduce) { finish(); return; }
    transcript.innerHTML = '';
    rail.innerHTML = '';
    strip.innerHTML = '';
    status.textContent = 'running';
    status.classList.add('busy');
    let i = 0;
    (function next() {
      if (i >= STEPS.length) {
        status.textContent = 'idle';
        status.classList.remove('busy');
        playBtn.textContent = 'Run it again';
        finish();
        return;
      }
      const step = STEPS[i];
      const node = row(step);
      node.classList.add('d-enter');
      transcript.appendChild(node);
      transcript.scrollTop = transcript.scrollHeight;
      setMeter(step);
      if (step.done) {
        strip.innerHTML = '';
        ['Read the kill path', 'Kill the tree, bound the drain',
         'Run the test that names the symptom', 'Report what it waited'].forEach((t, k) => {
          strip.appendChild(el('d-strip-item' + (k === 3 ? '' : ' done'), t));
        });
        ASSISTANT.forEach((m) => {
          const r = el('d-as-msg d-as-' + (m.from === 'you' ? 'you' : 'it'));
          r.appendChild(el('d-who', m.from === 'you' ? 'You' : 'Assistant'));
          r.appendChild(el('d-body', m.text));
          rail.appendChild(r);
        });
        rail.appendChild(el('d-as-cost',
          '~182 tokens of session context · you 20 · persona 162 · history 0 · tools 0'));
      }
      i += 1;
      setTimeout(next, step.kind === 'tool' ? 900 : 1250);
    })();
  }

  playBtn.addEventListener('click', play);

  // Show the finished turn if nobody scrolls to it, and play it once when they do.
  finish();
  if (!reduce && 'IntersectionObserver' in window) {
    let started = false;
    new IntersectionObserver((entries) => {
      if (started) return;
      started = entries.some((e) => e.isIntersecting && e.intersectionRatio > 0.35);
      if (started) play();
    }, { threshold: [0.35] }).observe(root);
  }
})();
