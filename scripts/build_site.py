"""Build the GitHub Pages site into `site/`.

Canonical documentation lives in `site/md/`. The build writes one HTML shell per page,
vendors the app's Markdown renderer, and refreshes the two repository pages from their
root sources. There is no second documentation directory and no frontend toolchain.

    python scripts/build_site.py

Navigation and landing cards come from the table in `site/md/index.md`.
"""

from __future__ import annotations

import json
import pathlib
import sys
import re
import shutil

# `demo_shell` lives beside this file, which is sys.path[0] however the build is started.
import demo_shell

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
MD_OUT = SITE / "md"
ASSETS = SITE / "assets"

REPO_PAGES = (("README.md", "readme"), ("CONTRIBUTING.md", "contributing"))

TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="nocturne">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<meta name="theme-color" content="#14171d">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='12' fill='%23111510'/%3E%3Cpath d='M14 17h36M32 17v31' stroke='%23d6c77a' stroke-width='7'/%3E%3C/svg%3E">
<link rel="stylesheet" href="assets/nocturne-tokens.css">
<link rel="stylesheet" href="assets/site.css">
</head>
<body class="{body_class}">
<a class="skip-link" href="#main">Skip to content</a>
<header class="masthead">
  <div class="masthead-inner">
    <a class="wordmark" href="index.html" aria-label="Tacit home"><span class="wordmark-mark" aria-hidden="true">T</span><span>Tacit</span></a>
    <span class="masthead-tag">{tagline}</span>
    <nav class="masthead-links" aria-label="Primary">
      <a href="index.html#evidence">Evidence</a>
      <a href="demo.html">Interface</a>
      <a href="install.html">Install</a>
    </nav>
    <a class="repository-link" href="{source_link}" rel="noopener noreferrer" target="_blank">GitHub <span aria-hidden="true">↗</span></a>
    <button class="nav-toggle" type="button" aria-controls="doc-nav" aria-expanded="false">Documentation</button>
  </div>
</header>

<div class="shell">
  <aside class="sidebar">
    <nav class="nav" id="doc-nav" aria-label="Documentation">
      {nav_docs}
      {nav_repo}
    </nav>
  </aside>

  <main class="page" id="main">
{hero}{article}  </main>
</div>

<footer class="footer">
  <div class="footer-inner">
    <a class="footer-brand" href="index.html">Tacit <span>{version}</span></a>
    <span>Local files. Chosen providers. No account or tracking.</span>
    <a href="https://github.com/{repo}" rel="noopener noreferrer" target="_blank">Source on GitHub ↗</a>
  </div>
</footer>

<script src="assets/markdown.js"></script>
<script src="assets/site.js"></script>
</body>
</html>
"""

ARTICLE = """    <div class="article-head"><span>Reference</span><span>Source-linked documentation</span></div>
    <article class="prose" id="page" data-md="{md_src}"></article>
"""

NAV_ITEM = '<li><a href="{href}"{current}>{label}</a></li>'
NAV_GROUP = '<div class="nav-group">\n      <p class="nav-label">{label}</p>\n      <ul>{items}</ul>\n    </div>'

CARD = ('<a class="card" href="{href}"><span class="card-key">{key}</span>'
        '<h2>{title}</h2><p>{description}</p><span class="card-go" aria-hidden="true">→</span></a>')

# The interface, embedded at full width. The frame is `demo.html`: the app's own markup
# and the app's own stylesheet, with a scripted turn running in it. Everything inside the
# frame is clickable, including the composer, so a reader who types gets an answer.
DEMO = """    <section class="demo-band" id="demo" aria-label="Tacit at work">
      <div class="demo-band-head">
        <div>
          <p class="eyebrow">The interface</p>
          <h2>The real app, running with no server</h2>
        </div>
        <p>This frame is generated from the app's own markup and stylesheet, driven by one
        scripted turn. It is not a drawing of the interface. Type in the composer and it
        answers. The full surface, including the Assistant, Delegation, terminal, Git and
        Prompts panels, is <code>demo.html</code> full screen.</p>
        <div class="demo-actions">
          <a href="demo.html" target="_blank" rel="noopener">Open full screen</a>
          <a href="interface.html">How the interface works</a>
        </div>
      </div>
      <div class="demo-frame-wrap">
        <iframe class="demo-frame" src="demo.html?view=basic" title="Tacit interface, running without a server" loading="lazy"></iframe>
      </div>
      <p class="demo-note">Nothing in the frame touches your machine: no backend, no WebSocket,
      no disk. The transcript is a script and the prompt texts are copied from the repository
      at build time.</p>
    </section>

"""

HERO = """    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Local-first coding agent</p>
        <h1>{headline}</h1>
        <p class="hero-lead">{lead}</p>
        <div class="hero-cta">
          <a class="btn btn-primary" href="install.html">Install</a>
          <a class="btn" href="demo.html">See it run</a>
          <a class="btn" href="readme.html">Read the README</a>
        </div>
      </div>
      <aside class="hero-panel" aria-label="Measured cost of a turn">
        <div class="panel-head"><span>Fixed cost per turn</span><span class="panel-tag">measured</span></div>
        <div class="panel-row"><span class="panel-key">Standing prompt</span><span class="panel-val">974 chars</span></div>
        <div class="panel-row"><span class="panel-key">Minimal profile</span><span class="panel-val">~768 tokens</span></div>
        <div class="panel-row"><span class="panel-key">Default profile</span><span class="panel-val">~2,039 tokens</span></div>
        <p class="panel-note">Re-derived from the code by a test on every run. Characters divided
        by four, the same heuristic the token meter uses. The preamble is one term you can see and
        set; the bill also depends on steps and transcript.</p>
        <a class="panel-link" href="cost-and-profiles.html">How the figure is produced →</a>
      </aside>
    </section>

    <section class="proof-strip" aria-label="Key facts">
      <div class="proof"><span class="proof-num">9/9</span><span class="proof-label">tasks solved, Linux default, OpenBench</span></div>
      <div class="proof"><span class="proof-num">1</span><span class="proof-label">process per turn, so Stop means stop</span></div>
      <div class="proof"><span class="proof-num">23</span><span class="proof-label">tools in the registry, gated by profile</span></div>
      <div class="proof"><span class="proof-num">0</span><span class="proof-label">accounts, telemetry or tracking</span></div>
    </section>

    <section class="principles" aria-label="What Tacit refuses to hide">
      <div class="principle">
        <span class="principle-key">01</span>
        <h3>Nothing optional is on</h3>
        <p>Memory, sandboxing and learning start off. A capability you did not enable costs no
        schema, no prompt text and no worker.</p>
      </div>
      <div class="principle">
        <span class="principle-key">02</span>
        <h3>It reports what it enforces</h3>
        <p>Where the platform cannot isolate a command, the result says so plainly. The sandbox
        only claims a guarantee it can actually keep.</p>
      </div>
      <div class="principle">
        <span class="principle-key">03</span>
        <h3>Nothing is learned without you</h3>
        <p>A proposal is a stored artifact you read and decide on. The analyzer registers no tool
        and adds no prompt text.</p>
      </div>
    </section>

{demo}    <section class="evidence" id="evidence" aria-label="Benchmark evidence">
      <div class="evidence-head">
        <p class="eyebrow">Evidence</p>
        <h2>One model, three tasks, both operating systems</h2>
        <p>GLM 5.2 on the fp8 endpoint, run by OpenBench's own runner with its checkers untouched.
        Three trials per task, one build. Tokens are uncached input plus output, cache reads
        excluded. These rows show the plumbing did not break on these tasks; they do not rank
        harnesses on hard work.</p>
      </div>
      <div class="evidence-grid">
        <div class="evidence-card">
          <span class="evidence-os">Linux</span>
          <div class="evidence-row"><span>Default</span><strong>9/9</strong><em>156,466 tok/solve</em></div>
          <div class="evidence-row"><span>Minimal</span><strong>9/9</strong><em>133,881 tok/solve</em></div>
        </div>
        <div class="evidence-card">
          <span class="evidence-os">Windows</span>
          <div class="evidence-row"><span>Default</span><strong>6/6</strong><em>106,522 tok/solve</em></div>
          <div class="evidence-row"><span>Minimal</span><strong>5/6</strong><em>82,841 tok/solve</em></div>
        </div>
      </div>
      <p class="evidence-note">Schemelike is graded on Linux only: its checker reads
      <code>os.O_NONBLOCK</code>, which Windows Python does not provide.
      <a href="testing-and-benchmarks.html">Methodology and per-task cells →</a></p>
    </section>

    <p class="eyebrow section-label">Documentation</p>
    <div class="card-grid">
{cards}
    </div>

"""

ROW_RE = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+\.md)\)\s*\|\s*([^|]+)\|")
H1_RE = re.compile(r"^#\s+(.+)$", re.M)


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def index_rows() -> list[tuple[str, str, str]]:
    """(title, file, description) for every page, in canonical navigation order."""
    rows = []
    for line in read(MD_OUT / "index.md").splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            rows.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
    if not rows:
        raise SystemExit("site/md/index.md no longer holds the page table this build reads")
    return rows


def title_of(p: pathlib.Path, fallback: str) -> str:
    m = H1_RE.search(read(p))
    return (m.group(1).strip() if m else fallback)


def esc_attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def slug_of(file: str) -> str:
    return file.rsplit("/", 1)[-1].removesuffix(".md")


THEME_BLOCK = re.compile(r"const THEMES = \[.*?^\];", re.S | re.M)


def theme_literal() -> str:
    """The app's own theme list, copied out of `static/app.js`.

    The mock's Appearance tab has to offer the same themes in the same order with the
    same hints, and a second list written here would drift the first time a theme is
    added to the app.
    """
    source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    match = THEME_BLOCK.search(source)
    if not match:
        raise SystemExit("static/app.js no longer declares `const THEMES = [...]`")
    return match.group(0).split("=", 1)[1].strip().rstrip(";")


# (group id, entry name, how much of it to show). The live panel lists all 89 entries;
# the mock shows enough of them that the claim on the landing page is checkable, and
# says so when a text is cut.
WANT_PROMPTS = [
    ("standing", "BASE", 1500),
    ("standing", "CHAT", 700),
    ("tools", "read", 600),
    ("tools", "edit", 600),
    ("tools", "bash", 700),
    ("guidance", "", 700),
    ("compaction", "", 700),
    ("assistant", "", 700),
    ("plan", "", 700),
]


def prompt_groups() -> list:
    """Real prompt text, read from the constants the sending code reads."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from backend.prompts_catalog import catalog
        cat = catalog(project=str(ROOT))
    except Exception as exc:  # noqa: BLE001 - a docs build must not fail on the backend
        print("  prompts unavailable: %s" % exc)
        return []
    by_id = {g["id"]: g for g in cat.get("groups", [])}
    out: list = []
    for gid, want, cap in WANT_PROMPTS:
        group = by_id.get(gid)
        if not group:
            continue
        for item in group["items"]:
            if want and item["name"] != want:
                continue
            text = item["text"]
            if len(text) > cap:
                cut = text.rfind("\n", 0, cap)
                text = text[:cut if cut > 200 else cap].rstrip() + "\n…"
            out.append({"id": gid, "label": group["label"],
                        "entries": [{"name": item["name"], "text": text}]})
            break
    live = (cat.get("live") or {}).get("blocks") or []
    if live:
        out.insert(0, {"id": "live", "label": "This session",
                       "entries": [{"name": b["name"], "text": b["text"][:1200]}
                                   for b in live[:2]]})
    return out


# The page the mock runs on is the app shell itself, patched by `demo_shell.py`. It is
# filled with tokens rather than str.format because the theme list and the prompt texts
# injected below are full of braces.
def demo_page(here: str) -> str:
    page = demo_shell.shell()
    themes = "window.TACIT_THEMES = " + theme_literal() + ";"
    prompts = json.dumps(prompt_groups(), ensure_ascii=False)
    return (page.replace("@@VERSION@@", here)
                .replace("@@THEMES@@", themes)
                .replace("@@PROMPTS@@", prompts))


def main() -> None:
    repo = "SebSilent/Tacit"
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").is_file() else ""
    tagline = "The coding agent where nothing is hidden"

    pages = index_rows()
    # The canonical index supplies navigation; it is not repeated in the sidebar.
    nav_items = [(title, slug_of(file) + ".html", description)
                 for title, file, description in pages if slug_of(file) != "index"]

    MD_OUT.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(ROOT / "static" / "markdown.js", ASSETS / "markdown.js")
    # The interface mock runs on the app's own stylesheet rather than an imitation of
    # it, so the site carries a byte copy of `static/style.css`. Nothing in
    # `site/assets/demo.css` restyles a surface the app already draws.
    shutil.copyfile(ROOT / "static" / "style.css", ASSETS / "app.css")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")

    # Root pages remain canonical on the repository side. Their Pages copies adapt
    # asset paths because the site directory is the web root on the gh-pages branch.
    for name, _slug in REPO_PAGES:
        source = ROOT / name
        if not source.is_file():
            raise SystemExit(f"{source} is missing; the site build needs it")
        text = read(source).replace("](site/", "](")
        (MD_OUT / name).write_text(text, encoding="utf-8")

    doc_files = ["index.md"] + [p[1].rsplit("/", 1)[-1] for p in pages]
    sources = [(MD_OUT / name, "md/" + name, name.removesuffix(".md") + ".html",
                title_of(MD_OUT / name, name)) for name in doc_files]
    sources += [(MD_OUT / name, "md/" + name, slug + ".html", title_of(MD_OUT / name, name))
                for name, slug in REPO_PAGES]

    for path, _out_rel, _href, _title in sources:
        if not path.is_file():
            raise SystemExit(f"{path} is missing; the site build needs it")

    def nav_html(active_href: str) -> tuple[str, str]:
        docs = "".join(NAV_ITEM.format(href=href, label=title,
                                       current=' aria-current="page"' if href == active_href else "")
                       for title, href, _ in nav_items)
        docs_html = NAV_GROUP.format(label="Documentation", items=docs) if docs else ""
        repo_items = "".join(NAV_ITEM.format(href=f"{slug}.html", label=title_of(ROOT / name, name),
                                             current=' aria-current="page"' if f"{slug}.html" == active_href else "")
                             for name, slug in REPO_PAGES)
        repo_html = NAV_GROUP.format(label="Repository", items=repo_items)
        return docs_html, repo_html

    for path, out_rel, href, title in sources:
        if href == "index.html":
            continue
        docs_html, repo_html = nav_html(href)
        out = SITE / href
        out.write_text(TEMPLATE.format(
            title=esc_attr(f"{title} · Tacit"),
            description=esc_attr(f"{title}. Documentation for Tacit, a local coding agent."),
            tagline=esc_attr(tagline),
            nav_docs=docs_html,
            nav_repo=repo_html,
            article=ARTICLE.format(md_src=out_rel),
            hero="",
            body_class="doc",
            version=esc_attr(version),
            repo=repo,
            source_link=f"https://github.com/{repo}",
        ), encoding="utf-8")

    # The landing page: hero, proof strip, demo, evidence, then the doc cards.
    cards = "\n".join(CARD.format(href=href, title=esc_attr(title), key=href.replace(".html", ""),
                                   description=esc_attr(desc))
                      for title, href, desc in nav_items)
    docs_html, repo_html = nav_html("index.html")
    hero = HERO.format(headline=esc_attr(tagline),
                       lead=esc_attr("A coding agent you run yourself. One Python program, plain "
                                     "web pages, no database. Every decision lands in a file you "
                                     "can read, and the cost of a turn is measured before you "
                                     "send it."),
                       demo=DEMO,
                       cards=cards)
    (SITE / "index.html").write_text(TEMPLATE.format(
        title="Tacit · The coding agent where nothing is hidden",
        description="Tacit is a local-first coding agent. Measured prompt cost, killable turns, "
                    "honest sandbox reporting, and documentation that cites the code.",
        tagline=esc_attr(tagline),
        nav_docs=docs_html,
        nav_repo=repo_html,
        article="",
        hero=hero,
        body_class="home",
        version=esc_attr(version),
        repo=repo,
        source_link=f"https://github.com/{repo}",
    ), encoding="utf-8")

    # The mock is a page of its own so it can be the app full screen, and the frame in
    # the landing page is that same file rather than a second copy of anything.
    (SITE / "demo.html").write_text(demo_page(version), encoding="utf-8")

    written = sorted(p.name for p in SITE.glob("*.html"))
    print(f"{len(written)} pages, {len(list(MD_OUT.glob('*.md')))} markdown sources")
    print("  " + " ".join(written))
    broken = verify()
    if broken:
        print("broken links:")
        for line in broken:
            print("  " + line)
        raise SystemExit(1)
    print("links ok")


HREF_RE = re.compile(r'href="([^"]+\.html)(?:#[^"]*)?"')
MD_LINK_RE = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")


def verify() -> list[str]:
    """Every link the site can follow has to land on a page that was written.

    The renderer turns `[Install](install.md)` into `install.html` in the browser, so a
    typo in `site/md/index.md` or a renamed file would only show up as a 404 after a
    push. Checked here instead, where a build can fail.
    """
    pages = {p.name for p in SITE.glob("*.html")}
    bad: list[str] = []
    for p in sorted(SITE.glob("*.html")):
        for href in HREF_RE.findall(read(p)):
            if href not in pages:
                bad.append(f"{p.name}: {href}")
    for p in sorted(MD_OUT.glob("*.md")):
        for link in MD_LINK_RE.findall(read(p)):
            seg = link.rsplit("/", 1)[-1].removesuffix(".md").lower()
            target = (seg or "index") + ".html"
            if target not in pages:
                bad.append(f"md/{p.name}: ({link}) -> {target}")
    return sorted(set(bad))


if __name__ == "__main__":
    main()
