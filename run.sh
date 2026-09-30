#!/usr/bin/env sh
# Tacit — start the web host.
set -e
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export TACIT_PORT="${TACIT_PORT:-8550}"
export TACIT_HOME="${TACIT_HOME:-$HOME/.tacit}"
echo "[tacit] http://localhost:$TACIT_PORT   state: $TACIT_HOME"
exec python3 -m backend.main
