#!/usr/bin/env sh
# Tacit — start the web host.
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
exec "$VENVPY" -m backend.main "$@"
