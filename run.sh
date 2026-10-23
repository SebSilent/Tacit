#!/usr/bin/env sh
# Tacit: start the web host.
set -e
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENVPY="$PWD/.venv/bin/python"
# Tacit runs on its own interpreter or not at all. Falling back to whatever
# python3 happens to be first on PATH means importing another tool's packages,
# and possibly a version of them Tacit cannot use.
if [ ! -x "$VENVPY" ]; then
    cat >&2 <<'MSG'

Tacit needs its own Python environment before it can start.

  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt

Or run install.sh, which does both and writes a launcher.
MSG
    exit 1
fi
export TACIT_PORT="${TACIT_PORT:-8550}"
export TACIT_HOME="${TACIT_HOME:-$HOME/.tacit}"
echo "[tacit] http://localhost:$TACIT_PORT   state: $TACIT_HOME"
# Stop what is provably Tacit's and wait for the port before starting anything.
# The Restart button runs this same script, so there is one launch path. The step
# is Python because proving a process is ours means reading its command line, and
# doing that in sh means a platform branch per statement.
"$VENVPY" -m backend.launcher prelaunch || {
    echo "[tacit] not started: a Tacit server still holds the port." >&2
    echo "[tacit] nothing was launched; see restart.log in the Tacit state folder." >&2
    exit 1
}
# The marker token is what lets a later stop step prove this process is Tacit's;
# it sits where the interpreter ignores it, so a server started here looks like
# one started by the Restart button's watcher.
exec "$VENVPY" -m backend.main tacit-server "$@"
