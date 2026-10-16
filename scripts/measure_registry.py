"""Measure what the registry actually costs the browser at boot.

`/api/sessions/registry` is the boot payload: one response holding every session,
and the frontend used to wait for all of it before painting anything. Counting
messages is not enough to know whether that is small, because one message can
carry a 48,000-character `read_file` result — so this counts bytes, which is the
unit the wire and the parser actually pay.

    python scripts/measure_registry.py [TACIT_HOME]

With no argument it measures the real home (~/.tacit). Point it at a throwaway
home to measure a synthetic case. Read-only: it never writes to the store.

Kept in the repo on purpose. The first version of this lived in %TEMP% and was
deleted with it, which is why the registry's byte cost was argued about from
memory for several sessions instead of measured.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    home = sys.argv[1] if len(sys.argv) > 1 else None
    if home:
        os.environ["TACIT_HOME"] = str(Path(home).resolve())
    # imported after TACIT_HOME is set: config.HOME is read from the environment
    # at import time, so importing earlier would measure the wrong home
    from backend import config, store

    reg = store.registry()
    sessions = reg["sessions"]
    blob = json.dumps(reg, ensure_ascii=False)
    print(f"sessions dir: {config.SESSIONS_DIR}")
    print(f"sessions:  {len(sessions)}")
    print(f"registry:  {len(blob):,} bytes ({len(blob) / 1024:.1f} KB)")

    rows = sorted(((json.dumps(s, ensure_ascii=False), s) for s in sessions),
                  reverse=True, key=lambda x: len(x[0]))
    print("\nlargest session rows:")
    for text, s in rows[:5]:
        msgs = s.get("messages") or []
        worst = max((len(json.dumps(m, ensure_ascii=False)) for m in msgs),
                    default=0)
        print(f"  {len(text):>8,} B  {s.get('messageCount', 0):>3} msgs"
              f"  tail={len(msgs):>2}  worst msg={worst:,} B  {s.get('title', '')[:40]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
