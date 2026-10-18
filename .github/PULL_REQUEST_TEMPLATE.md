## What changed

One paragraph. What the code does now that it did not do before, and why.

## How it was verified

The command you ran and what it printed. A sentence like "tested manually" is not
verification, and the reviewer cannot tell what you actually exercised.

- [ ] `python -m unittest discover -s tests -t .` passes
- [ ] `python scripts/check_prose.py` passes
- [ ] New behavior has a test that fails without it

## Citations and docs

If you changed a `path:line` citation, you opened the file and read the line. If you
added a setting, you added the line that reads it, in `backend/`, and the row that
documents it. If you removed a surface, you removed its tests, its route and its docs.

## What this costs

For a new capability: what it costs someone who never turns it on. The answer should be
nothing. If it is not nothing, say what it is.

## Benchmark rows, if any

Every row carries:

- the checker's output, verbatim
- the byte counts and sha256 digests of what it graded
- the per-call audit ledger, so the claimed solve stays a transcript somebody else can
  re-run

State the hardware, the runtime and version, the context length, the sampling settings,
the number of runs, and the pass rule. A number without those is not a result.
