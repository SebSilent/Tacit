"""The evaluation matrix runner (P0B.3): tasks, checkers, manifests, reports.

One task = one isolated run: a fresh TACIT_HOME, a prepared workdir, one
headless Tacit turn, one checker, one manifest. The runner never grades the
harness on the model's mistakes: a run whose plumbing failed (error, timeout,
crash) is an INFRASTRUCTURE failure and is counted separately from a task the
agent ran cleanly but did not solve.

Two modes:

- ``--offline``: the conformance run. Tasks with an ``offline_script`` drive
  the reusable FakeProvider through a real headless subprocess, proving the
  whole pipeline (setup, run, checker, manifest, report) without a provider
  key. This is a pipeline proof, not a capability measurement.
- ``--live --model REF``: the measurement run against a configured endpoint.
  The operator's models.json and .env are copied into each task's throwaway
  home; nothing else is shared between tasks.

Tuning and held-out splits are aggregated separately and never merged: a task
used to tune prompts must not also carry a release claim. Competitor arms run
the SAME tasks and checkers through their own harnesses and contribute rows
with a different ``arm``; the report format reserves the field.

Usage:
    python scripts/eval_matrix.py --offline --out eval-artifacts
    python scripts/eval_matrix.py --live --model provider/id --out eval-artifacts
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CATALOG = ROOT / "scripts" / "eval_tasks.json"


def load_tasks(path: Path | None = None) -> list[dict]:
    data = json.loads((path or CATALOG).read_text(encoding="utf-8"))
    return list(data.get("tasks") or [])


def _prepare_workdir(work: Path, task: dict) -> None:
    work.mkdir(parents=True, exist_ok=True)
    for rel, content in ((task.get("setup") or {}).get("files") or {}).items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _check(task: dict, work: Path) -> tuple[bool, str]:
    """The checker's verdict on the workdir, with its output for the record."""
    checker = task.get("checker") or {}
    kind = checker.get("type")
    try:
        if kind == "file_exists":
            ok = (work / checker["path"]).is_file()
            return ok, "" if ok else f"{checker['path']} is missing"
        if kind in ("file_equals", "file_contains"):
            target = work / checker["path"]
            if not target.is_file():
                return False, f"{checker['path']} is missing"
            text = target.read_text(encoding="utf-8", errors="replace")
            want = checker.get("content") or ""
            if kind == "file_equals":
                return text == want, "" if text == want else "content differs"
            return want in text, "" if want in text else "content not found"
        if kind == "command":
            env = dict(os.environ)
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
            r = subprocess.run(checker["command"], cwd=str(work), env=env, shell=True,
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=120)
            out = ((r.stdout or "") + (r.stderr or "")).strip()[-800:]
            return r.returncode == 0, out
    except Exception as exc:  # noqa: BLE001
        return False, f"checker error: {exc}"
    return False, f"unknown checker type {kind!r}"


def _write_offline_registry(home: Path, url: str) -> None:
    (home / "models.json").write_text(json.dumps({
        "default": "p/m",
        "providers": {"p": {"baseUrl": url, "apiKey": "k",
                            "models": [{"id": "m", "contextWindow": 100000}]}},
    }), encoding="utf-8")


def _copy_live_credentials(home: Path) -> None:
    source = Path(os.environ.get("TACIT_HOME") or (Path.home() / ".tacit"))
    for name in ("models.json", ".env"):
        src = source / name
        if src.is_file():
            shutil.copyfile(src, home / name)


def run_task(task: dict, *, mode: str, out_dir: Path, model: str = "",
             profile: str | None = None) -> dict:
    """One isolated run of one task. Returns its report row."""
    from tests.fake_provider import FakeProvider

    task_dir = out_dir / task["id"]
    home = task_dir / "home"
    work = task_dir / "workdir"
    home.mkdir(parents=True, exist_ok=True)
    _prepare_workdir(work, task)
    budget = task.get("budget") or {}
    timeout_s = float(budget.get("timeout_s") or 300)
    fp = None
    if mode == "offline":
        script = task.get("offline_script")
        if not script:
            return {"task": task["id"], "split": task.get("split"), "skipped": True,
                    "reason": "no offline script (live-only task)"}
        fp = FakeProvider(script)
        _write_offline_registry(home, fp.url)
        ref = "p/m"
    else:
        _copy_live_credentials(home)
        ref = model
    row_path = task_dir / "row.json"
    cmd = [sys.executable, "-m", "backend.headless",
           "--instruction", task["instruction"], "--workdir", str(work),
           "--model", ref, "--timeout", str(timeout_s),
           "--max-steps", str(budget.get("max_steps") or 12),
           "--json-out", str(row_path), "--quiet"]
    if profile:
        cmd += ["--profile", profile]
    env = dict(os.environ)
    env["TACIT_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("TACIT_MANIFEST", None)
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout_s + 90)
        crash = proc.returncode not in (0, 1)
        stderr_tail = (proc.stderr or "")[-800:]
    except subprocess.TimeoutExpired:
        crash, stderr_tail = True, "runner timeout: the headless process outlived its budget"
    finally:
        if fp is not None:
            fp.stop()
    result = {}
    try:
        result = json.loads(row_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    # Infrastructure failure: the harness's own plumbing decided the outcome.
    # A clean run that the checker rejects is a task failure, not an infra one.
    infra = crash or not result or not result.get("ok")
    solved = False
    checker_out = ""
    if not infra:
        solved, checker_out = _check(task, work)
    manifest_src = result.get("manifest") or ""
    manifest_dst = ""
    if manifest_src and Path(manifest_src).is_file():
        manifest_dst = str(task_dir / "manifest.json")
        shutil.copyfile(manifest_src, manifest_dst)
    row = {
        "task": task["id"],
        "split": task.get("split") or "tuning",
        "class": task.get("class") or "",
        "arm": "tacit",
        "mode": mode,
        "model": result.get("model") or ref,
        "ok": bool(result.get("ok")),
        "solved": solved,
        "infra_failure": infra,
        "error": result.get("error") or (stderr_tail if crash else None),
        "tokens": int(result.get("tokens") or 0),
        "wall_s": result.get("wall_time_s") or round(time.time() - started, 3),
        "steps": int(result.get("steps") or 0),
        "tool_calls": int(result.get("tool_calls") or 0),
        "checker_output": checker_out,
        "manifest": manifest_dst,
        "workdir": str(work),
    }
    (task_dir / "report_row.json").write_text(json.dumps(row, indent=1, ensure_ascii=False),
                                              encoding="utf-8")
    return row


def _aggregate(rows: list[dict]) -> dict:
    ran = [r for r in rows if not r.get("skipped")]
    solved = [r for r in ran if r.get("solved")]
    infra = [r for r in ran if r.get("infra_failure")]
    task_failed = [r for r in ran if not r.get("infra_failure") and not r.get("solved")]
    tokens_per_solve = (round(sum(r["tokens"] for r in solved) / len(solved), 1)
                        if solved else None)
    wall_per_solve = (round(sum(r["wall_s"] for r in solved) / len(solved), 2)
                      if solved else None)
    return {"tasks": len(ran), "solved": len(solved),
            "infra_failures": len(infra), "task_failures": len(task_failed),
            "tokens_per_solve": tokens_per_solve, "wall_per_solve": wall_per_solve,
            "infra_task_ids": [r["task"] for r in infra]}


def run_matrix(tasks: list[dict], *, mode: str, out_dir: Path, model: str = "",
               profile: str | None = None) -> dict:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for task in tasks:
        rows.append(run_task(task, mode=mode, out_dir=run_dir, model=model, profile=profile))
    splits = {}
    for split in sorted({r.get("split") or "tuning" for r in rows}):
        splits[split] = _aggregate([r for r in rows if (r.get("split") or "tuning") == split])
    conformance = []
    for task in tasks:
        if task.get("expect_infra_failure"):
            row = next((r for r in rows if r["task"] == task["id"]), None)
            if row and not row.get("skipped"):
                conformance.append({"task": task["id"],
                                    "classified_as_infra": bool(row.get("infra_failure"))})
    from backend.autoupdater import local_version
    report = {
        "run_id": run_id, "mode": mode, "model": model, "arm": "tacit",
        "tacit_version": local_version(), "started": round(time.time(), 3),
        "splits": splits, "conformance": conformance, "rows": rows,
        "rule": "tuning and held_out are reported separately; a task tuned "
                "against must never carry a release claim. Infrastructure "
                "failures are counted apart from task failures.",
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                         encoding="utf-8")
    (run_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    lines = [f"# Evaluation run {report['run_id']}", "",
             f"Mode: {report['mode']}  Model: {report['model'] or '(offline fake)'}  "
             f"Tacit {report['tacit_version']}", "",
             "Infrastructure failures are plumbing failures, counted apart from task "
             "failures. Splits are never merged.", ""]
    for split, agg in report["splits"].items():
        lines += [f"## {split}", "",
                  f"- tasks: {agg['tasks']}",
                  f"- solved: {agg['solved']}",
                  f"- infrastructure failures: {agg['infra_failures']}"
                  + (f" ({', '.join(agg['infra_task_ids'])})" if agg["infra_task_ids"] else ""),
                  f"- task failures: {agg['task_failures']}",
                  f"- uncached tokens per solve: {agg['tokens_per_solve']}",
                  f"- wall seconds per solve: {agg['wall_per_solve']}", ""]
    lines += ["## Rows", "",
              "| task | split | class | solved | infra | tokens | wall s |",
              "|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        if r.get("skipped"):
            continue
        lines.append(f"| {r['task']} | {r['split']} | {r['class']} | "
                     f"{'yes' if r['solved'] else 'no'} | "
                     f"{'yes' if r['infra_failure'] else 'no'} | {r['tokens']} | {r['wall_s']} |")
    lines += ["", "Raw artifacts: each task's workdir, result row and manifest live "
              "beside this report, inspectable as-is.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the Tacit evaluation matrix.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true",
                      help="conformance run against the local fake provider")
    mode.add_argument("--live", action="store_true",
                      help="measurement run against a configured endpoint")
    ap.add_argument("--model", default="", help="model pin for --live (provider/id)")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--out", default=str(ROOT / "eval-artifacts"))
    ap.add_argument("--task", action="append", default=None, help="run only these task ids")
    ap.add_argument("--split", default=None, help="run only this split")
    args = ap.parse_args(argv)
    if args.live and not args.model:
        ap.error("--live requires --model provider/id")
    tasks = load_tasks(Path(args.catalog) if args.catalog else None)
    if args.task:
        tasks = [t for t in tasks if t["id"] in args.task]
    if args.split:
        tasks = [t for t in tasks if t.get("split") == args.split]
    if not tasks:
        print("no tasks match the filters", file=sys.stderr)
        return 2
    report = run_matrix(tasks, mode="offline" if args.offline else "live",
                        out_dir=Path(args.out), model=args.model, profile=args.profile)
    print(json.dumps({"run_id": report["run_id"], "splits": report["splits"],
                      "conformance": report["conformance"]}, indent=1))
    print(f"artifacts: {Path(args.out) / report['run_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())