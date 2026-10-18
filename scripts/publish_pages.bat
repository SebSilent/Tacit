@echo off
rem Publish site/ to GitHub Pages.
rem
rem Run after scripts/build_site.py, from the repo root:
rem     scripts\publish_pages.bat
rem
rem Pages reads the gh-pages branch, not master. It has to: this repository's Pages
rem API accepts only / or /docs as the source path, /docs is the markdown folder, and
rem the built site lives in site/, so the site cannot be served from master directly.
rem
rem The branch holds nothing but the contents of site/ at the root, which is what
rem Pages wants. It is built with commit-tree from the site tree of HEAD, so it never
rem carries backend code and never merges with master. A plain copy would put the
rem files under site/ on gh-pages too, and every URL would need /site/ in it.
setlocal enabledelayedexpansion
cd /d "%~dp0.."

for /f "delims=" %%a in ('git rev-parse HEAD:site') do set TREE=%%a
for /f "delims=" %%b in ('git commit-tree !TREE! -m "Pages site"') do set COMMIT=%%b
if not defined COMMIT (
    echo could not build a commit from HEAD:site. Run scripts\build_site.py first.
    exit /b 1
)
echo publishing tree !TREE! as commit !COMMIT!
git branch -f gh-pages !COMMIT!
git push -f origin gh-pages
endlocal
