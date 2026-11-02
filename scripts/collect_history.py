"""Collect main-branch history for the jobs in data/reruns.jsonl.

For every (repo, workflow, job name) that failed, fetch that workflow's runs on the default branch and
record, per job and per run: first-attempt conclusion, whether a later attempt fixed it, and (for a
capped number of first-attempt failures) the error template of the failing log.

build_dataset.py later keeps only history from BEFORE each failure, so nothing leaks from the future.

Usage:
  python scripts/collect_history.py [--src data/reruns.jsonl] [--out data/history.jsonl] [--days 14]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ops_judge.ci.github import gh, job_history, main_runs, ts  # noqa: E402
from ops_judge.ci.facts import first_error_line  # noqa: E402
from ops_judge.ci.extract import error_excerpt  # noqa: E402
from ops_judge.ci.templates import mask  # noqa: E402

LOGS_PER_JOB = 3  # main-branch failure logs fetched per job name (for error templates)


def safe_history(repo: str, run: dict, wanted: set[str]) -> list[dict]:
    try:
        return job_history(repo, run, wanted)
    except RuntimeError as e:
        print(f"  {repo} run {run['id']}: {e}", file=sys.stderr)
        return []


def add_templates(rows: list[dict]) -> None:
    fails: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if not r["first_ok"]:
            fails[(r["repo"], r["workflow_id"], r["job_name"])].append(r)

    def fetch(r: dict) -> None:
        try:
            log = gh(f"repos/{r['repo']}/actions/jobs/{r['first_job_id']}/logs", raw=True)
            r["error_template"] = mask(first_error_line(error_excerpt(log), log))
        except RuntimeError:
            pass

    todo = [r for group in fails.values() for r in sorted(group, key=lambda x: x["created_at"], reverse=True)[:LOGS_PER_JOB]]
    print(f"fetching {len(todo)} main-branch failure logs ...", file=sys.stderr)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(fetch, todo))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data/reruns.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "data/history.jsonl"))
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    failures = [json.loads(line) for line in Path(args.src).read_text(encoding="utf-8").splitlines() if line]
    out = Path(args.out)
    # Incremental: workflows already in the output are skipped; run metadata comes from collect_context's cache.
    existing = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x] if out.exists() else []
    done = {(r["repo"], r["workflow_id"]) for r in existing}
    meta_path = ROOT / "data/run_meta.jsonl"
    meta = ({m["run_id"]: m for m in map(json.loads, meta_path.read_text(encoding="utf-8").splitlines())}
            if meta_path.exists() else {})

    # Group failures by workflow; the run endpoint gives workflow_id, the repo gives the default branch.
    branches = {repo: gh(f"repos/{repo}")["default_branch"] for repo in {f["repo"] for f in failures}}
    groups: dict[tuple, dict] = {}
    for f in failures:
        run = meta.get(f["run_id"]) or gh(f"repos/{f['repo']}/actions/runs/{f['run_id']}")
        key = (f["repo"], run["workflow_id"])
        if key in done:
            continue
        g = groups.setdefault(key, {"jobs": set(), "start": ts(run["created_at"]), "end": ts(run["created_at"])})
        g["jobs"].add(f["job_name"])
        g["start"] = min(g["start"], ts(run["created_at"]))
        g["end"] = max(g["end"], ts(run["created_at"]))

    rows: list[dict] = []
    for (repo, wid), g in groups.items():
        runs = main_runs(repo, wid, branches[repo], g["start"] - timedelta(days=args.days), g["end"])
        print(f"{repo} workflow {wid}: {len(runs)} main runs, {len(g['jobs'])} job names", file=sys.stderr)
        with ThreadPoolExecutor(8) as pool:
            for chunk in pool.map(lambda r: safe_history(repo, r, g["jobs"]), runs):
                rows += chunk
    add_templates(rows)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in existing + rows), encoding="utf-8")
    print(f"added {len(rows)} history rows ({len(existing) + len(rows)} total) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
