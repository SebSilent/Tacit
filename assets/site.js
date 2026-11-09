/*
 * Renders a markdown file into the page with the app's own renderer, vendored at
 * assets/markdown.js. The markdown is the single source: the site does not keep a
 * second copy of the prose that could drift from the repository.
 */

(function () {
  const page = document.getElementById('page');
  if (!page) return;

  const src = page.getAttribute('data-md');

  // A link to a .md file is a link to a page of this site. The files are siblings
  // here, so only the last segment of the path matters.
  // The last segment only, lowercased: the pages are siblings here, and GitHub
  // Pages serves from a case-sensitive filesystem, so `CONTRIBUTING.md` has to land
  // on `contributing.html` the same way `configuration.md` does.
  function htmlHref(href) {
    const clean = href.replace(/\.md$/i, '');
    const seg = clean.split('/').pop();
    return (seg === '' ? 'index' : seg).toLowerCase() + '.html';
  }

  function decorate(root) {
    root.querySelectorAll('a[href]').forEach((a) => {
      const href = a.getAttribute('href') || '';
      if (/^https?:/i.test(href)) {
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        return;
      }
      if (href.startsWith('#')) return;
      if (/\.md$/i.test(href.split('#')[0])) {
        const frag = href.includes('#') ? '#' + href.split('#')[1] : '';
        a.setAttribute('href', htmlHref(href.split('#')[0]) + frag);
      }
    });

    root.querySelectorAll('h2, h3').forEach((h) => {
      const text = h.textContent || '';
      const id = text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      if (id) h.id = id;
    });

    root.querySelectorAll('.copy-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const block = btn.closest('.code-block');
        const code = block && block.querySelector('pre');
        if (!code) return;
        const done = () => {
          btn.textContent = 'Copied';
          btn.classList.add('is-done');
          setTimeout(() => {
            btn.textContent = 'Copy';
            btn.classList.remove('is-done');
          }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(code.textContent || '').then(done, () => {});
        }
      });
    });
  }

  function fail(message) {
    page.innerHTML = '<p class="state-error">' + message + '</p>';
  }

  fetch(src)
    .then((r) => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then((text) => {
      page.innerHTML = window.TacitMarkdown.md(text);
      decorate(page);
      document.title = (document.querySelector('#page h1') || {}).textContent
        || document.title;
    })
    .catch(() => fail('This page could not read its markdown.'));
})();
