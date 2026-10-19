"""Build the GitHub Pages site into `site/`.

The prose stays in the repository. This reads `README.md`, `CONTRIBUTING.md` and `docs/*.md`,
copies them into `site/md/`, and writes one HTML shell per page that renders its markdown with
the app's own renderer (`static/markdown.js`, vendored into `site/assets/`). So the site has no
second copy of any sentence, and no build toolchain beyond this file.

    python scripts/build_site.py

The order of the navigation and the landing cards comes from the table in `docs/index.md`, which
is the same list a reader of the repository sees first. Nothing here duplicates it.
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
<link rel="stylesheet" href="assets/nocturne-tokens.css">
<link rel="stylesheet" href="assets/site.css">
</head>
<body>
<header class="masthead">
  <div class="masthead-inner">
    <a class="wordmark" href="index.html">Tacit</a>
    <span class="masthead-tag">{tagline}</span>
    <nav class="masthead-links">
      <a href="demo.html">Interface</a>
      <a href="readme.html">README</a>
      <a href="contributing.html">Contributing</a>
      <a href="{source_link}" rel="noopener noreferrer" target="_blank">Repository</a>
    </nav>
  </div>
</header>

<div class="shell">
  <nav class="nav" aria-label="Documentation">
    {nav_docs}
    {nav_repo}
  </nav>

  <main class="page">
{hero}    <article class="prose" id="page" data-md="{md_src}"></article>
  </main>
</div>

<footer class="footer">
  <div class="footer-inner">
    <span>Tacit {version}</span>
    <span>Everything stays on your machine. No accounts, no telemetry, no tracking.</span>
    <a href="https://github.com/{repo}" rel="noopener noreferrer" target="_blank">github.com/{repo}</a>
  </div>
</footer>

<script src="assets/markdown.js"></script>
<script src="assets/site.js"></script>
</body>
</html>
"""

NAV_ITEM = '<li><a href="{href}"{current}>{label}</a></li>'
NAV_GROUP = '<div class="nav-group">\n      <p class="nav-label">{label}</p>\n      <ul>{items}</ul>\n    </div>'

CARD = ('<a class="card" href="{href}"><span class="card-key">{key}</span>'
        '<h2>{title}</h2><p>{description}</p></a>')

# The interface, embedded at full width. The frame is `demo.html`: the app's own markup
# and the app's own stylesheet, with a scripted turn running in it. Everything inside the
# frame is clickable, including the composer, so a reader who types gets an answer.
DEMO = """    <section class="demo-band" id="demo" aria-label="Tacit at work">
      <div class="demo-band-head">
        <div>
          <p class="eyebrow">The interface</p>
          <h2>The whole app, with no server behind it</h2>
        </div>
        <p>The frame below loads <code>demo.html?view=basic</code>, which the build
        generates out of <code>static/index.html</code> and <code>static/style.css</code>
        rather than drawing: the sidebar, the top bar, the transcript, the tool cards and
        the meter are the real parts, driven by one scripted turn with a pointer that
        moves to the composer, types the ask and presses send. Every documentation page
        embeds a still view of its own choosing. The whole thing, with the Assistant asked
        several questions per session, the Delegation panel, the terminal, the Git panel and
        the Prompts tab, is <code>demo.html</code> full screen.</p>
        <div class="demo-actions">
          <a href="demo.html" target="_blank" rel="noopener">Open full screen</a>
          <a href="interface.html">How the interface works</a>
        </div>
      </div>
      <div class="demo-frame-wrap">
        <iframe class="demo-frame" src="demo.html?view=basic" title="Tacit interface, running without a server"></iframe>
      </div>
      <p class="demo-note">Nothing in the frame touches your machine: there is no backend,
      no WebSocket and no disk. The transcript is a script, the theme list and the prompt
      texts are copied out of the repository at build time, and the handlers are written
      against the app's markup rather than being the app's own, because the app's scripts
      have no server here to talk to.</p>
    </section>

"""

# Each documentation page shows one part of the interface, chosen for what that page is
# about, in a frame small enough to read as a card. These are still frames of the real
# panels, not a tour: the pointer does not walk the tabs here. The running mock, with the
# pointer, the Assistant and the Delegation panel, is `demo.html` itself, which is what the
# landing page embeds and what "Open full screen" leads to.
FRAGMENTS = {
    "install.html": ("empty", "What the page shows before a workspace is picked: the empty "
        "state and the four asks under it."),
    "interface.html": ("transcript", "One transcript row: the thinking block collapsed, the "
        "answer, and the meter the turn moved."),
    "settings.html": ("settings", "The General tab, the half people skip: profiles, "
        "permissions, the fallback picker, density, sounds."),
    "configuration.html": ("budget", "The turn budget as the server reports it, with the "
        "bounds each field clamps to."),
    "tools.html": ("tools", "Four tool cards in flight, one of them failed, each with its "
        "own time and size."),
    "architecture.html": ("plan", "The plan strip and the task list a phase leaves behind."),
    "cost-and-profiles.html": ("meter", "The context meter climbing into the compaction zone, "
        "then coming back down."),
    "isolation-and-audit.html": ("git", "The Git panel: the branch and its tracking, the "
        "changed files, the console underneath."),
    "memory-and-learning.html": ("skills", "Skills and the tools they ride on, in the Tools tab."),
    "mcp-and-plugins.html": ("mcp", "The four MCP helper rows, gated until a server is "
        "configured, and what the gate says about itself."),
    "testing-and-benchmarks.html": ("testrun", "A test run that fails first and passes "
        "second, because that is the order it usually happens in."),
    "data-and-privacy.html": ("snapshots", "The snapshots one turn took, and what restore "
        "does and does not touch."),
    "readme.html": ("transcript", "The thing the README describes, in the frame it runs in."),
    "contributing.html": ("testrun", "The suite a change has to pass, shown failing once "
        "first."),
}

FRAGMENT = """    <section class="demo-frag" aria-label="{label}">
      <div class="demo-frag-head">
        <p class="eyebrow">In the interface</p>
        <p>{caption}</p>
        <a href="demo.html?view={view}" target="_blank" rel="noopener">Open this view</a>
      </div>
      <iframe class="demo-frag-frame" src="demo.html?view={view}" title="{label}"></iframe>
    </section>

"""


def fragment_html(href: str) -> str:
    entry = FRAGMENTS.get(href)
    if not entry:
        return ""
    view, caption = entry
    label = "Tacit interface, the %s view, running without a server" % view
    return FRAGMENT.format(view=view, caption=esc_attr(caption), label=esc_attr(label))


HERO = """    <section class="hero">
      <p class="eyebrow">A coding agent you run yourself</p>
      <h1>{headline}</h1>
      <p>{lead}</p>
      <div class="hero-rule"></div>
      <p>One Python program serving plain web pages. No build step, no frontend toolchain, no
      database to install. The fixed cost per turn is the smallest in its class, nothing optional
      is on until you turn it on, and the sandbox reports what it actually enforces.</p>
    </section>

{demo}    <p class="eyebrow">Start here</p>
    <div class="card-grid">
{cards}
    </div>

"""

ROW_RE = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+\.md)\)\s*\|\s*([^|]+)\|")
H1_RE = re.compile(r"^#\s+(.+)$", re.M)


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def index_rows() -> list[tuple[str, str, str]]:
    """(title, file, description) for every page, in the order docs/index.md lists them."""
    rows = []
    for line in read(ROOT / "docs" / "index.md").splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            rows.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
    if not rows:
        raise SystemExit("docs/index.md no longer holds the page table this build reads")
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
    # docs/index.md itself is the landing page, so it is not repeated in the nav.
    nav_items = [(title, slug_of(file) + ".html", description)
                 for title, file, description in pages if slug_of(file) != "index"]

    if MD_OUT.exists():
        shutil.rmtree(MD_OUT)
    MD_OUT.mkdir(parents=True)
    ASSETS.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(ROOT / "static" / "markdown.js", ASSETS / "markdown.js")
    # The interface mock runs on the app's own stylesheet rather than an imitation of
    # it, so the site carries a byte copy of `static/style.css`. Nothing in
    # `site/assets/demo.css` restyles a surface the app already draws.
    shutil.copyfile(ROOT / "static" / "style.css", ASSETS / "app.css")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")

    # docs/index.md is the landing page's prose, and it is not in its own table.
    doc_files = ["index.md"] + [p[1].rsplit("/", 1)[-1] for p in pages]
    sources = [(ROOT / "docs" / name, "md/" + name, name.removesuffix(".md") + ".html",
                title_of(ROOT / "docs" / name, name)) for name in doc_files]
    # The repository pages get the href they are linked as, lowercased: GitHub Pages
    # serves from a case-sensitive filesystem, and `README.md` is not `readme.html`.
    sources += [(ROOT / name, "md/" + name, slug + ".html", title_of(ROOT / name, name))
                for name, slug in REPO_PAGES]

    for path, out_rel, _href, _title in sources:
        if not path.is_file():
            raise SystemExit(f"{path} is missing; the site build needs it")
        shutil.copyfile(path, SITE / out_rel)

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
            md_src=out_rel,
            hero=fragment_html(href),
            version=esc_attr(version),
            repo=repo,
            source_link=f"https://github.com/{repo}",
        ), encoding="utf-8")

    # The landing page: hero and cards, then the index's own prose.
    cards = "\n".join(CARD.format(href=href, title=esc_attr(title), key=href.replace(".html", ".md"),
                                   description=esc_attr(desc))
                      for title, href, desc in nav_items)
    docs_html, repo_html = nav_html("index.html")
    # The hero's headline is the tagline the masthead already carries, not a second
    # sentence written for the site: the build's rule is that it holds no prose the
    # repository does not. The page's own H1 comes from docs/index.md below.
    hero = HERO.format(headline=esc_attr(tagline),
                       lead=esc_attr("How Tacit works, where each setting lives, and which line of "
                                     "code decides it. Every citation is a file and a line you can "
                                     "open."),
                       demo=DEMO,
                       cards=cards)
    (SITE / "index.html").write_text(TEMPLATE.format(
        title="Tacit · Documentation",
        description="Documentation for Tacit, a coding agent you run yourself: architecture, "
                    "configuration, tools, isolation, memory and cost.",
        tagline=esc_attr(tagline),
        nav_docs=docs_html,
        nav_repo=repo_html,
        md_src="md/index.md",
        hero=hero,
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
    typo in `docs/index.md` or a renamed file would only show up as a 404 after a
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
