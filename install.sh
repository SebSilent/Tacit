#!/usr/bin/env bash
# Tacit bootstrap installer (macOS, Linux, WSL).
#
#   curl -fsSL https://raw.githubusercontent.com/Silent/tacit/main/install.sh | bash
#
# Clones the source, builds a private virtualenv, and writes a `tacit` launcher.
# Safe to re-run: it updates the checkout instead of replacing it.
set -eu

REPO_URL="${TACIT_REPO_URL:-https://github.com/Silent/tacit.git}"
BRANCH="${TACIT_BRANCH:-main}"
USER_HOME="${HOME:-$USERPROFILE}"
TACIT_HOME="${TACIT_HOME:-$USER_HOME/.tacit}"
APP_DIR="${TACIT_APP_DIR:-$USER_HOME/.local/share/tacit}"
BIN_DIR="${TACIT_BIN_DIR:-$USER_HOME/.local/bin}"
WANT_BROWSER=true

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) APP_DIR="$2"; shift 2 ;;
        --tacit-home) TACIT_HOME="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --bin-dir) BIN_DIR="$2"; shift 2 ;;
        --no-browser|--skip-browser) WANT_BROWSER=false; shift ;;
        -h|--help)
            cat <<'USAGE'
Usage: install.sh [options]

  --dir PATH         where to put the source checkout
  --tacit-home PATH  where Tacit keeps your data       (default ~/.tacit)
  --bin-dir PATH     where to write the tacit launcher (default ~/.local/bin)
  --branch NAME      git branch to install             (default main)
  --no-browser       skip the optional Playwright install
  --help             this text

Environment: TACIT_REPO_URL, TACIT_BRANCH, TACIT_HOME, TACIT_APP_DIR, TACIT_BIN_DIR
USAGE
            exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_GRN=$'\033[0;32m'; C_YEL=$'\033[0;33m'; C_RED=$'\033[0;31m'
    C_CYA=$'\033[0;36m'; C_BLD=$'\033[1m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_GRN=""; C_YEL=""; C_RED=""; C_CYA=""; C_BLD=""; C_DIM=""; C_OFF=""
fi
say()  { printf '%s->%s %s\n' "$C_CYA" "$C_OFF" "$1"; }
ok()   { printf '%s v%s %s\n' "$C_GRN" "$C_OFF" "$1"; }
warn() { printf '%s !%s %s\n' "$C_YEL" "$C_OFF" "$1" >&2; }
die()  { printf '%s x%s %s\n' "$C_RED" "$C_OFF" "$1" >&2; exit 1; }

printf '\n%s Tacit — the Silent Harness%s\n\n' "$C_BLD" "$C_OFF"

case "$(uname -s 2>/dev/null || echo unknown)" in
    Linux*|Darwin*) : ;;
    *) die "unsupported platform: $(uname -s). On Windows run install.ps1 instead." ;;
esac

# ── prerequisites ─────────────────────────────────────────────────────────
for tool in git curl; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required. Install it and re-run."
done

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done
[ -n "$PY" ] || die "Python 3.10 or newer is required. Install it and re-run."
ok "prerequisites present ($($PY --version 2>&1))"

# ── source checkout ───────────────────────────────────────────────────────
if [ -f "$APP_DIR/backend/main.py" ] && [ ! -d "$APP_DIR/.git" ]; then
    warn "$APP_DIR already holds the source but is not a checkout - using it as-is"
elif [ -d "$APP_DIR/.git" ]; then
    say "updating $APP_DIR"
    git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1 || die "could not fetch $BRANCH"
    git -C "$APP_DIR" checkout -q "$BRANCH" 2>/dev/null || true
    git -C "$APP_DIR" reset -q --hard "origin/$BRANCH" 2>/dev/null || true
else
    say "downloading into $APP_DIR"
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR" >/dev/null 2>&1 \
        || die "could not clone $REPO_URL (branch $BRANCH)"
fi
[ -f "$APP_DIR/backend/main.py" ] || die "$APP_DIR does not look like Tacit"
ok "source ready"

# ── virtualenv ────────────────────────────────────────────────────────────
VENV="$APP_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    say "creating a virtual environment"
    "$PY" -m venv "$VENV" || die "could not create the virtualenv (is python3-venv installed?)"
fi
say "installing Python dependencies"
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt" \
    || die "pip install failed"
ok "dependencies installed"

# ── optional browser tool ─────────────────────────────────────────────────
if [ "$WANT_BROWSER" = true ] && command -v npm >/dev/null 2>&1; then
    say "installing the browser driver (Playwright, ~130 MB — skip with --no-browser)"
    if (cd "$APP_DIR" && npm install --silent --no-audit --no-fund >/dev/null 2>&1 \
        && npx --yes playwright install chromium >/dev/null 2>&1); then
        ok "browser automation available"
    else
        warn "browser install failed — everything else works; run 'npm install && npx playwright install chromium' in $APP_DIR later"
    fi
elif [ "$WANT_BROWSER" = true ]; then
    warn "node/npm not found — skipping the browser tool (everything else works)"
fi

# ── launcher ──────────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/tacit"
cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env sh
TACIT_HOME="\${TACIT_HOME:-$TACIT_HOME}"
export TACIT_HOME
cd "$APP_DIR" || exit 1
exec "$VENV/bin/python" -m backend.main "\$@"
LAUNCH
chmod +x "$LAUNCHER"
ok "launcher written to $LAUNCHER"

PORT="${TACIT_PORT:-8550}"
printf '\n%sdone.%s\n\n' "$C_BLD" "$C_OFF"
printf '  start:  %s\n' "$LAUNCHER"
printf '  or:     cd %s && %s/bin/python -m backend.main\n' "$APP_DIR" "$VENV"
printf '  open:   http://localhost:%s\n' "$PORT"
printf '  state:  %s\n\n' "$TACIT_HOME"
case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *) printf '%s%s is not on your PATH.%s Add this to your shell profile:\n' "$C_YEL" "$BIN_DIR" "$C_OFF"
       printf '  export PATH="%s:$PATH"\n\n' "$BIN_DIR" ;;
esac
printf 'Then add a provider in Settings > Providers.\n\n'
