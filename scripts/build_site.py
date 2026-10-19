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

import pathlib
import re
import shutil

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
<script src="assets/demo.js"></script>
</body>
</html>
"""

NAV_ITEM = '<li><a href="{href}"{current}>{label}</a></li>'
NAV_GROUP = '<div class="nav-group">\n      <p class="nav-label">{label}</p>\n      <ul>{items}</ul>\n    </div>'

CARD = ('<a class="card" href="{href}"><span class="card-key">{key}</span>'
        '<h2>{title}</h2><p>{description}</p></a>')

# The interface mock. `assets/demo.js` fills the three empty containers and bails out
# on every page that has no #demo, so this markup is the only thing that decides where
# the demo appears. It is a shell: no sentence of it is a claim about a product, and
# the text inside it is a turn about a defect this harness really had.
DEMO = """    <section class="demo" id="demo" aria-label="Tacit at work">
      <div class="demo-bar">
        <span class="demo-title">Tacit</span>
        <span class="demo-chip">glm-5.2 · fp8</span>
        <span class="demo-chip">default profile · 20 tools</span>
        <span class="demo-status" id="demo-status">idle</span>
        <span class="demo-meter" id="demo-meter" aria-hidden="true"><i></i></span>
        <span class="demo-tokens" id="demo-tokens">0 / 128,000</span>
        <button class="demo-play" id="demo-play" type="button">Run the turn</button>
      </div>
      <div class="demo-grid">
        <aside class="demo-sessions">
          <p class="demo-rail-label">Sessions</p>
          <div class="d-sess"><span class="d-dot"></span>audit ledger rotation</div>
          <div class="d-sess on"><span class="d-dot working"></span>shell timeout leaves orphans</div>
          <div class="d-sess"><span class="d-dot"></span>compaction tail budget</div>
          <div class="d-sess"><span class="d-dot"></span>mcp lazy activation</div>
        </aside>
        <div class="demo-main">
          <div class="demo-transcript" id="demo-transcript"></div>
          <div class="demo-strip" id="demo-strip"></div>
          <div class="demo-composer">message Tacit</div>
        </div>
        <aside class="demo-assistant" id="demo-assistant"></aside>
      </div>
    </section>
    <p class="demo-caption">A turn drawn with the interface's own parts, and no server behind
    it. The tool cards open, the thinking block collapses, the task strip sits outside the
    transcript where a compaction cannot summarise it away, and the panel on the right is the
    Assistant: a second conversation the agent is never told exists. The counter in the bar is
    the context window filling as the turn spends it.</p>

"""

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
            hero="",
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
