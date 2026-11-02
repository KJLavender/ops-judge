"""GitHub Actions access through the authenticated `gh` CLI (no token handling of our own)."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime


def gh(path: str, raw: bool = False):
    # Raw job logs contain ANSI color codes; gh refuses to print them without this flag.
    cmd = ["gh", "api", path] + (["--allow-escape-sequences"] if raw else [])
    for _ in range(3):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            return proc.stdout if raw else json.loads(proc.stdout)
        if "rate limit" not in proc.stderr.lower():
            break
        # Out of API budget: wait for the reset instead of silently dropping data.
        wait_for_rate_limit()
    raise RuntimeError(proc.stderr.strip()[:200])


def wait_for_rate_limit() -> None:
    try:
        out = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, text=True, encoding="utf-8")
        reset = json.loads(out.stdout)["resources"]["core"]["reset"]
    except (ValueError, KeyError):
        reset = time.time() + 300
    time.sleep(max(5, reset - time.time() + 5))


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def changed_files(repo: str, run: dict) -> list[str]:
    # `pull_requests` sometimes lists PRs from other repos that share the head commit; keep only ours.
    prs = [p for p in run.get("pull_requests") or [] if p["base"]["repo"]["url"].endswith(f"/repos/{repo}")]
    try:
        if prs:
            files = gh(f"repos/{repo}/pulls/{prs[0]['number']}/files?per_page=100")
        else:
            files = gh(f"repos/{repo}/commits/{run['head_sha']}")["files"]
        return [f["filename"] for f in files]
    except (RuntimeError, KeyError):
        return []


def all_jobs(repo: str, run_id: int, attempts: str = "all") -> list[dict]:
    """Jobs of a run; attempts="all" includes earlier attempts, "latest" only the last one."""
    jobs, page = [], 1
    while True:
        batch = gh(f"repos/{repo}/actions/runs/{run_id}/jobs?filter={attempts}&per_page=100&page={page}")["jobs"]
        jobs += batch
        if len(batch) < 100:
            return jobs
        page += 1


def main_runs(repo: str, workflow_id: int, branch: str, start: datetime, end: datetime, max_pages: int = 5) -> list[dict]:
    created = f"{start:%Y-%m-%dT%H:%M:%SZ}..{end:%Y-%m-%dT%H:%M:%SZ}"
    runs, page = [], 1
    while page <= max_pages:
        batch = gh(f"repos/{repo}/actions/workflows/{workflow_id}/runs?branch={branch}&status=completed"
                   f"&created={created}&per_page=100&page={page}")["workflow_runs"]
        runs += batch
        if len(batch) < 100:
            break
        page += 1
    return runs


def job_history(repo: str, run: dict, wanted: set[str]) -> list[dict]:
    """One row per wanted job name in a main-branch run: first-attempt result and whether a rerun fixed it."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for j in all_jobs(repo, run["id"]):
        if j["name"] in wanted:
            by_name[j["name"]].append(j)
    rows = []
    for name, js in by_name.items():
        js.sort(key=lambda j: j.get("run_attempt", 1))
        first = js[0]
        if first.get("conclusion") not in ("success", "failure"):
            continue  # skipped / cancelled: says nothing about the job
        rows.append({"repo": repo, "workflow_id": run["workflow_id"], "job_name": name, "run_id": run["id"],
                     "created_at": run["created_at"], "first_ok": first["conclusion"] == "success",
                     "fixed_by_rerun": first["conclusion"] == "failure" and any(j.get("conclusion") == "success" for j in js[1:]),
                     "first_job_id": first["id"]})
    return rows


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def parse_hunks(patch: str) -> list[tuple[int, int]]:
    """New-file line ranges touched by a unified diff."""
    out = []
    for m in HUNK.finditer(patch or ""):
        start, length = int(m.group(1)), int(m.group(2) or 1)
        out.append((start, start + max(length, 1) - 1))
    return out


def changed_hunks(repo: str, run: dict) -> dict[str, list[tuple[int, int]]]:
    prs = [p for p in run.get("pull_requests") or [] if p["base"]["repo"]["url"].endswith(f"/repos/{repo}")]
    try:
        if prs:
            files = gh(f"repos/{repo}/pulls/{prs[0]['number']}/files?per_page=100")
        else:
            files = gh(f"repos/{repo}/commits/{run['head_sha']}")["files"]
        return {f["filename"]: parse_hunks(f.get("patch", "")) for f in files}
    except (RuntimeError, KeyError):
        return {}


def workflow_runs(repo: str, workflow_id: int, start: datetime, end: datetime, max_pages: int = 10) -> list[dict]:
    """Completed runs of a workflow on any branch in [start, end]."""
    created = f"{start:%Y-%m-%dT%H:%M:%SZ}..{end:%Y-%m-%dT%H:%M:%SZ}"
    runs, page = [], 1
    while page <= max_pages:
        batch = gh(f"repos/{repo}/actions/workflows/{workflow_id}/runs?status=completed&created={created}"
                   f"&per_page=100&page={page}")["workflow_runs"]
        runs += batch
        if len(batch) < 100:
            break
        page += 1
    return runs
