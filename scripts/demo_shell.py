"""The interface mock is the app's own shell, edited, not a drawing of it.

`static/index.html` is the source of truth, and `build_site.py` generates `site/demo.html`
from it at build time, so the mock cannot fall behind the interface the site advertises:
the Git overlay, new settings rows and renamed ids arrive in the mock the same commit they
arrive in the app. What makes it a mock rather than the app is the list below: a seeded
session list, four asks that match the scripted turn, a Prompts tab, and a control strip
in place of the eight scripts that would otherwise try to reach a server that is not there.

Each entry is the app's text and the demo's text, always whole lines. The build stops if
an anchor does not appear exactly once, so an edit to `static/index.html` fails here rather
than shipping a page that quietly lost a panel.
"""

import pathlib
import re

# Cache-busting queries are the app's business. The site's assets are copied per build,
# and stripping them here keeps every anchor below from changing on each version bump.
VERSION_QUERY = re.compile(r"\?v=\d+")

HEAD = """\
<meta name="theme-color" id="meta-theme" content="#14171d">
<meta name="robots" content="noindex">
<title>Tacit interface (mock)</title>
<link rel="stylesheet" href="assets/app.css">
<link rel="stylesheet" href="assets/demo.css">
"""

SESSIONS = """\
    <nav class="session-list" id="sessionList">
      <div class="session-item active" data-key="shell-timeout">
        <span class="s-dot" title="working"></span>
        <svg class="s-icon" viewBox="0 0 24 24" fill="none"><path d="M5 4h14v11H8l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
        <span class="s-title">shell timeout leaves orphans</span>
        <button class="s-stop" title="Stop this session's turn"><svg viewBox="0 0 24 24" fill="none"><rect x="7" y="7" width="10" height="10" rx="1.5" stroke="currentColor" stroke-width="1.8"/></svg></button>
        <button class="s-del" title="Delete"><svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
      </div>
      <div class="session-item" data-key="audit-rotation">
        <span class="s-dot" title="idle"></span>
        <svg class="s-icon" viewBox="0 0 24 24" fill="none"><path d="M5 4h14v11H8l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
        <span class="s-title">audit ledger rotation</span>
        <button class="s-del" title="Delete"><svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
      </div>
      <div class="session-item" data-key="prompt-size">
        <span class="s-dot" title="idle"></span>
        <svg class="s-icon" viewBox="0 0 24 24" fill="none"><path d="M5 4h14v11H8l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
        <span class="s-title">why is the prompt so big</span>
        <button class="s-del" title="Delete"><svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
      </div>
    </nav>
"""

CHIPS = """\
          <button class="chip" data-s="The shell tool kills a timed-out command but the children keep running. Find it and fix it.">Fix a bug</button>
          <button class="chip" data-s="The audit ledger never rotates. Add size-based rotation without losing the current file.">Add rotation</button>
          <button class="chip" data-s="How much of the fixed prompt is the tool list, and can I read it anywhere?">Read the prompt</button>
"""

CHROME = """\
<div class="demo-chrome">
  <span class="demo-chrome-label">Interface mock, no server</span>
  <button class="demo-chrome-btn" id="demoPause" type="button">Pause</button>
  <button class="demo-chrome-btn" id="demoSpeed" type="button">1x</button>
  <button class="demo-chrome-btn" id="demoReplay" type="button">Replay</button>
  <a class="demo-chrome-link" href="index.html" target="_top">Docs</a>
</div>

<script>
window.TACIT_VERSION = "@@VERSION@@";
@@THEMES@@
window.TACIT_PROMPTS = @@PROMPTS@@;
</script>
<script src="assets/markdown.js"></script>
<script src="assets/demo.js"></script>
</body>
"""

APP_SCRIPTS = """\
<script src="/static/transport.js"></script>
<script src="/static/markdown.js"></script>
<script src="/static/app.js"></script>
<script src="/static/git.js"></script>
<script src="/static/harness.js"></script>
<script src="/static/platform.js"></script>
<script src="/static/assistant.js"></script>
<script src="/static/delegation.js"></script>
</body>
"""

PATCHES = [
    # The mock boots on a real palette instead of waiting for a stored preference.
    ('<html lang="en">\n',
     '<html lang="en" data-theme="nocturne">\n'),
    # The app's own favicon and viewport comment stay: they are part of what is shown.
    ('<title>Tacit</title>\n<link rel="stylesheet" href="/static/style.css">\n',
     HEAD),
    ('    <nav class="session-list" id="sessionList"></nav>\n',
     SESSIONS),
    ('      <button data-mode="chat" class="active">Chat</button>\n'
     '      <button data-mode="agent">Agent</button>\n',
     '      <button data-mode="chat">Chat</button>\n'
     '      <button data-mode="agent" class="active">Agent</button>\n'),
    ('          <button class="chip" data-s="Explain what this folder contains and what each file does.">Explore a folder</button>\n'
     '          <button class="chip" data-s="Write a python script that renames all files in the current directory to kebab-case.">Write a script</button>\n'
     '          <button class="chip" data-s="Create a snake game in a single html file in this folder.">Build a game</button>\n',
     CHIPS),
    # Settings gains one tab the app does not have, and the panel it points at.
    ('          <button class="ho-tab" data-tab="appearance">Appearance</button>\n'
     '        </nav>\n',
     '          <button class="ho-tab" data-tab="appearance">Appearance</button>\n'
     '          <button class="ho-tab" data-tab="prompts">Prompts</button>\n'
     '        </nav>\n'),
    ('        <section class="ho-panel" id="panel-appearance" hidden></section>\n',
     '        <section class="ho-panel" id="panel-appearance" hidden></section>\n'
     '        <section class="ho-panel" id="panel-prompts" hidden></section>\n'),
    (APP_SCRIPTS, CHROME),
]


STRAY_INDENT = re.compile(r"^[ \t]+(<script src=)", re.MULTILINE)


def build_shell(app_html: str) -> str:
    """Apply the demo edits to the app shell, insisting each anchor is still there."""
    out = STRAY_INDENT.sub(r"\1", VERSION_QUERY.sub("", app_html))
    for old, new in PATCHES:
        hits = out.count(old)
        if hits != 1:
            raise SystemExit(
                "demo patch found %d matches, expected 1, for: %.70s" % (hits, old)
            )
        out = out.replace(old, new)
    return out


def shell() -> str:
    """The patched app shell, read from the repository at build time."""
    root = pathlib.Path(__file__).resolve().parent.parent
    return build_shell((root / "static" / "index.html").read_text(encoding="utf-8"))
