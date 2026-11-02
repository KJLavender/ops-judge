"""Collect the raw evidence behind the context facts (ops_judge/ci/context.py). Incremental: reruns
skip anything already in the output files.

  data/run_meta.jsonl        per failed run: workflow, branch, time, changed line ranges (hunks)
  data/main_failures.jsonl   default-branch first-attempt failures of the same jobs: failing tests, error template
  data/other_failures.jsonl  other branches' first-attempt failures of the same jobs (window: N days before)
  data/github_incidents.json githubstatus.com incident history (last 50)

Usage:
  python scripts/collect_context.py [--days-other 3] [--max-other-runs 200]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops_judge.ci.extract import error_excerpt, failing_tests, infra_signals  # noqa: E402
from ops_judge.ci.facts import first_error_line  # noqa: E402
from ops_judge.ci.github import all_jobs, changed_hunks, gh, ts, workflow_runs  # noqa: E402
from ops_judge.ci.templates import mask  # noqa: E402

DATA = ROOT / "data"
LOGS_PER_JOB_OTHER = 30


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x] if path.exists() else []


def append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def log_summary(repo: str, job_id: int) -> dict | None:
    """Failing tests + error template of one job log, or None if the log is gone."""
    try:
        log = gh(f"repos/{repo}/actions/jobs/{job_id}/logs", raw=True)
    except RuntimeError:
        return None
    excerpt = error_excerpt(log)
    return {"tests": failing_tests(excerpt), "template": mask(first_error_line(excerpt, log)),
            "infra_signals": infra_signals(excerpt)}


def pmap(fn, items, workers: int = 8) -> list:
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(fn, items))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-other", type=float, default=3)
    ap.add_argument("--max-other-runs", type=int, default=200)
    args = ap.parse_args()
    failures = read_jsonl(DATA / "reruns.jsonl")
    history = read_jsonl(DATA / "history.jsonl")

    # 1. per-run metadata + changed line ranges
    meta_path = DATA / "run_meta.jsonl"
    have = {m["run_id"] for m in read_jsonl(meta_path)}
    todo = sorted({(f["repo"], f["run_id"]) for f in failures if f["run_id"] not in have})
    print(f"run meta: {len(todo)} runs", file=sys.stderr)

    def meta(item):
        repo, run_id = item
        try:
            run = gh(f"repos/{repo}/actions/runs/{run_id}")
        except RuntimeError:
            return None
        return {"repo": repo, "run_id": run_id, "workflow_id": run["workflow_id"], "head_branch": run["head_branch"],
                "created_at": run["created_at"], "hunks": changed_hunks(repo, run)}

    append_jsonl(meta_path, [m for m in pmap(meta, todo) if m])
    metas = {m["run_id"]: m for m in read_jsonl(meta_path)}

    # 2. default-branch failures of the same jobs: which tests failed, which error
    mf_path = DATA / "main_failures.jsonl"
    have = {(m["repo"], m["run_id"], m["job_name"]) for m in read_jsonl(mf_path)}
    wanted = {(f["repo"], f["job_name"]) for f in failures}
    todo = [h for h in history if not h["first_ok"] and (h["repo"], h["job_name"]) in wanted
            and (h["repo"], h["run_id"], h["job_name"]) not in have]
    print(f"main failures: {len(todo)} logs", file=sys.stderr)

    def main_fail(h):
        s = log_summary(h["repo"], h["first_job_id"])
        return s and {k: h[k] for k in ("repo", "job_name", "run_id", "created_at", "fixed_by_rerun")} | s

    append_jsonl(mf_path, [m for m in pmap(main_fail, todo) if m])

    # 3. other branches' failures of the same jobs, in the days before each failure
    of_path = DATA / "other_failures.jsonl"
    seen_runs = {(o["repo"], o["run_id"]) for o in read_jsonl(of_path)}
    groups: dict[tuple, dict] = {}
    for f in failures:
        m = metas.get(f["run_id"])
        if not m:
            continue
        g = groups.setdefault((f["repo"], m["workflow_id"]), {"jobs": set(), "start": ts(m["created_at"]), "end": ts(m["created_at"])})
        g["jobs"].add(f["job_name"])
        g["start"], g["end"] = min(g["start"], ts(m["created_at"])), max(g["end"], ts(m["created_at"]))
    branches = {repo: gh(f"repos/{repo}")["default_branch"] for repo, _ in groups}
    for (repo, wid), g in groups.items():
        try:
            runs = workflow_runs(repo, wid, g["start"] - timedelta(days=args.days_other), g["end"])
        except RuntimeError as e:
            print(f"  {repo} {wid}: {e}", file=sys.stderr)
            continue
        runs = [r for r in runs if r["head_branch"] != branches[repo] and (r["conclusion"] == "failure" or r["run_attempt"] > 1)
                and (repo, r["id"]) not in seen_runs]
        runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)[:args.max_other_runs]
        print(f"{repo} workflow {wid}: {len(runs)} other-branch failed runs", file=sys.stderr)

        def first_attempt_failures(run):
            try:
                jobs = all_jobs(repo, run["id"])
            except RuntimeError:
                return []
            firsts: dict[str, dict] = {}
            for j in sorted(jobs, key=lambda j: j.get("run_attempt", 1)):
                firsts.setdefault(j["name"], j)
            return [(run, j) for name, j in firsts.items() if name in g["jobs"] and j.get("conclusion") == "failure"]

        pairs = [p for chunk in pmap(first_attempt_failures, runs) for p in chunk]
        per_job: dict[str, int] = defaultdict(int)
        capped = []
        for run, j in pairs:  # newest first; cap logs per job name
            if per_job[j["name"]] < LOGS_PER_JOB_OTHER:
                per_job[j["name"]] += 1
                capped.append((run, j))

        def other(pair):
            run, j = pair
            s = log_summary(repo, j["id"])
            return s and {"repo": repo, "workflow_id": wid, "job_name": j["name"], "run_id": run["id"],
                          "head_branch": run["head_branch"], "created_at": run["created_at"]} | s

        append_jsonl(of_path, [o for o in pmap(other, capped) if o])
        # Remember runs with no matching failed job too, so a rerun of this script skips them.
        append_jsonl(of_path, [{"repo": repo, "run_id": r["id"], "workflow_id": wid, "job_name": None, "head_branch": r["head_branch"],
                                "created_at": r["created_at"], "template": None, "tests": []}
                               for r in runs if r["id"] not in {p[0]["id"] for p in capped}])

    # 4. last attempt of every "persistent" failure: if it died of the same infrastructure accident
    #    (spot reclaim, lost runner), the label says "rerun did not help" only by bad luck -> label noise.
    la_path = DATA / "last_attempts.jsonl"
    have = {x["id"] for x in read_jsonl(la_path)}
    todo = [f for f in failures if f["label"] == "persistent_failure" and f["id"] not in have]
    print(f"last attempts: {len(todo)}", file=sys.stderr)

    def last_attempt(f):
        n = len(f["attempts"])
        try:
            jobs = gh(f"repos/{f['repo']}/actions/runs/{f['run_id']}/attempts/{n}/jobs?per_page=100")["jobs"]
        except RuntimeError:
            return None
        job = next((j for j in jobs if j["name"] == f["job_name"]), None)
        if not job:
            return None
        return {"id": f["id"], "attempt": n, "conclusion": job.get("conclusion")} | (log_summary(f["repo"], job["id"]) or {})

    append_jsonl(la_path, [x for x in pmap(last_attempt, todo) if x])

    # 5. GitHub status incidents
    with urllib.request.urlopen("https://www.githubstatus.com/api/v2/incidents.json", timeout=30) as resp:
        incidents = json.load(resp)["incidents"]
    (DATA / "github_incidents.json").write_text(json.dumps(
        [{k: i.get(k) for k in ("name", "created_at", "resolved_at", "impact")} for i in incidents], ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"incidents: {len(incidents)}", file=sys.stderr)


if __name__ == "__main__":
    main()
