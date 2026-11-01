"""Collect free flaky labels from GitHub Actions rerun history.

Same commit, attempt 1 failed, a later attempt succeeded  -> label "flaky"
Every attempt failed                                      -> label "persistent_failure" (refine by hand later)

Usage:
  python scripts/collect_reruns.py owner/repo [owner/repo ...] --pages 5 --out data/reruns.jsonl
Needs an authenticated `gh` CLI. Logs older than the repo's retention (default 90 days) are gone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_judge.ci.github import changed_files, gh  # noqa: E402

MAX_LOG_LINES = 3000


def failed_logs(repo: str, run_id: int) -> list[dict]:
    jobs = gh(f"repos/{repo}/actions/runs/{run_id}/attempts/1/jobs?per_page=100")["jobs"]
    out = []
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        try:
            log = gh(f"repos/{repo}/actions/jobs/{job['id']}/logs", raw=True)
        except RuntimeError as e:
            print(f"  job {job['id']}: log unavailable ({e})", file=sys.stderr)
            continue
        out.append({"job_name": job["name"], "log": "\n".join(log.split("\n")[-MAX_LOG_LINES:])})
    return out


def collect(repo: str, pages: int, skip: set[str]):
    for page in range(1, pages + 1):
        try:
            runs = gh(f"repos/{repo}/actions/runs?per_page=100&page={page}")["workflow_runs"]
        except RuntimeError as e:
            print(f"  page {page}: {e}", file=sys.stderr)
            return
        if not runs:
            return
        for run in runs:
            n = run.get("run_attempt", 1)
            if n < 2 or f"{repo}#{run['id']}" in skip:
                continue
            try:
                attempts = [gh(f"repos/{repo}/actions/runs/{run['id']}/attempts/{i}")["conclusion"] for i in range(1, n + 1)]
                if attempts[0] != "failure":
                    continue
                if "success" in attempts[1:]:
                    label = "flaky"
                elif all(a == "failure" for a in attempts):
                    label = "persistent_failure"
                else:
                    continue  # cancelled / mixed: ambiguous
                jobs = failed_logs(repo, run["id"])
            except RuntimeError as e:
                print(f"  run {run['id']}: {e}", file=sys.stderr)
                continue
            head = (run.get("head_repository") or {}).get("full_name")
            files = changed_files(repo, run) if jobs else []
            for job in jobs:
                yield {
                    "id": f"{repo}#{run['id']}#{job['job_name']}",
                    "repo": repo, "run_id": run["id"], "head_sha": run["head_sha"], "event": run["event"],
                    "workflow": run.get("name"), "created_at": run.get("created_at"),
                    "label": label, "attempts": attempts, "is_fork_pr": bool(head and head != repo),
                    "changed_files": files, **job,
                }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--out", default="data/reruns.jsonl")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Resume: skip runs already collected by a previous invocation.
    skip = set()
    if out.exists():
        skip = {"#".join(json.loads(line)["id"].split("#", 2)[:2])
                for line in out.read_text(encoding="utf-8").splitlines() if line}
    n = 0
    with out.open("a", encoding="utf-8") as fh:
        for repo in args.repos:
            print(f"{repo} ...", file=sys.stderr)
            for row in collect(repo, args.pages, skip):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                n += 1
    print(f"wrote {n} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
