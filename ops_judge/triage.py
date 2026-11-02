"""Triage a real GitHub Actions failure end to end and print the decision trace.

  ops-judge triage https://github.com/OWNER/REPO/actions/runs/RUN_ID[/job/JOB_ID]

Steps: fetch the run and its failed jobs -> fetch logs and main-branch history -> build facts ->
cascade (rule -> decision model -> LLM -> human queue) -> planned action.

Decision model tier: set TYPESAFE_API_KEY for Jev, or --jev http://127.0.0.1:8094/v1/systemone for a
local llama-server (Kev, Laya, ...). Without either, that tier is shown as skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from judge_core import Cascade, HumanQueue, JevJudge, LLMJudge, RuleJudge, Store, load_policies
from judge_core.types import JudgeError, JudgeRequest, JudgeResult
from ops_judge.ci.actions import plan_action
from ops_judge.ci import context
from ops_judge.ci.extract import error_excerpt, failing_tests, stack_frames
from ops_judge.ci.facts import CIFailure, build_state, first_error_line
from ops_judge.ci.github import all_jobs, changed_files, changed_hunks, gh, job_history, main_runs, ts, workflow_runs
from ops_judge.ci.templates import TemplateIndex, mask

ROOT = Path(__file__).resolve().parents[1]
URL = re.compile(r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)(?:/job/(\d+))?")
MAX_LOG_LINES = 3000
TRANSIENT = ("flaky_test", "infrastructure")


class Skipped:
    """Placeholder tier that always declines, so the trace shows why it was skipped."""

    def __init__(self, name: str, reason: str):
        self.name, self.reason = name, reason

    def judge(self, req: JudgeRequest) -> JudgeResult:
        raise JudgeError(f"skipped: {self.reason}")


def log_summary(repo: str, job_id: int) -> dict | None:
    try:
        log = gh(f"repos/{repo}/actions/jobs/{job_id}/logs", raw=True)
    except RuntimeError:
        return None
    excerpt = error_excerpt(log)
    return {"tests": failing_tests(excerpt), "template": mask(first_error_line(excerpt, log))}


def history(repo: str, run: dict, job_name: str, days: int, max_runs: int) -> tuple[list[bool], int, TemplateIndex, list[dict]]:
    """This job's results on the default branch in the `days` before the run (never after it), plus the
    failing tests and error template of its recent default-branch failures."""
    branch = gh(f"repos/{repo}")["default_branch"]
    end = ts(run["created_at"])
    runs = main_runs(repo, run["workflow_id"], branch, end - timedelta(days=days), end, max_pages=1)[:max_runs]
    with ThreadPoolExecutor(8) as pool:
        rows = [r for chunk in pool.map(lambda r: job_history(repo, r, {job_name}), runs) for r in chunk]
    rows = [r for r in rows if ts(r["created_at"]) < end]
    idx = TemplateIndex(use_drain=False)
    fails = [r for r in rows if not r["first_ok"]][:10]
    with ThreadPoolExecutor(8) as pool:
        summaries = list(pool.map(lambda r: log_summary(repo, r["first_job_id"]), fails))
    main_failures = [r | s for r, s in zip(fails, summaries) if s]
    idx.seen |= {m["template"] for m in main_failures}
    return [r["first_ok"] for r in rows], sum(r["fixed_by_rerun"] for r in rows), idx, main_failures


def other_branch_failures(repo: str, run: dict, job_name: str, max_runs: int = 15) -> list[dict]:
    """First-attempt failures of the same job on other branches in the 3 days before this run."""
    end = ts(run["created_at"])
    runs = [r for r in workflow_runs(repo, run["workflow_id"], end - timedelta(days=3), end, max_pages=2)
            if r["head_branch"] != run["head_branch"] and (r["conclusion"] == "failure" or r["run_attempt"] > 1)][:max_runs]

    def one(r):
        try:
            jobs = sorted(all_jobs(repo, r["id"]), key=lambda j: j.get("run_attempt", 1))
        except RuntimeError:
            return None
        first = next((j for j in jobs if j["name"] == job_name), None)
        if not first or first.get("conclusion") != "failure":
            return None
        s = log_summary(repo, first["id"])
        return s and {"created_at": r["created_at"], "head_branch": r["head_branch"]} | s

    with ThreadPoolExecutor(8) as pool:
        return [x for x in pool.map(one, runs) if x]


def github_incidents() -> list[dict]:
    try:
        with urllib.request.urlopen("https://www.githubstatus.com/api/v2/incidents.json", timeout=10) as resp:
            return json.load(resp)["incidents"]
    except OSError:
        return []


def build_cascade(args, store: Store) -> Cascade:
    policies = load_policies(ROOT / "policies/ci.yaml")
    if args.jev:
        jev = JevJudge(url=args.jev)
    elif os.environ.get("TYPESAFE_API_KEY"):
        jev = JevJudge()
    else:
        jev = Skipped("jev", "no TYPESAFE_API_KEY and no --jev URL")
    llm = Skipped("llm", "--no-llm") if args.no_llm else LLMJudge()
    return Cascade(policies, {"rule": RuleJudge.from_policies(policies), "jev": jev, "llm": llm,
                              "human": HumanQueue(store)}, store)


def triage(args) -> list[dict]:
    m = URL.search(args.url)
    if not m:
        sys.exit("expected https://github.com/OWNER/REPO/actions/runs/RUN_ID[/job/JOB_ID]")
    repo, run_id, job_id = m.group(1), int(m.group(2)), m.group(3)
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    run = gh(f"repos/{repo}/actions/runs/{run_id}")

    def failed(path: str) -> list[dict]:
        return [j for j in gh(path)["jobs"]
                if j.get("conclusion") == "failure" and (job_id is None or str(j["id"]) == job_id)]

    jobs, outcome = failed(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"), None
    if not jobs and run.get("run_attempt", 1) > 1:
        # A later attempt passed: judge the first attempt as it was, then reveal what the rerun did.
        jobs = failed(f"repos/{repo}/actions/runs/{run_id}/attempts/1/jobs?per_page=100")
        outcome = f"attempt {run['run_attempt']} concluded {run['conclusion']}"
    if not jobs:
        sys.exit("no failed jobs in this run")
    head = (run.get("head_repository") or {}).get("full_name")
    is_fork = bool(head and head != repo)
    files = changed_files(repo, run)
    hunks = {} if args.no_context else changed_hunks(repo, run)
    incidents = [] if args.no_context else github_incidents()
    timings["fetch_run"] = time.perf_counter() - t0

    store = Store(ROOT / "data/triage.sqlite")
    cascade = build_cascade(args, store)
    results = []
    for job in jobs[:args.max_jobs]:
        t = time.perf_counter()
        log = "\n".join(gh(f"repos/{repo}/actions/jobs/{job['id']}/logs", raw=True).split("\n")[-MAX_LOG_LINES:])
        timings["fetch_log"] = time.perf_counter() - t
        t = time.perf_counter()
        main_hist, fixed, idx, main_fail = ([], 0, None, []) if args.no_history else history(repo, run, job["name"], args.days, args.max_runs)
        timings["history"] = time.perf_counter() - t
        t = time.perf_counter()
        state = build_state(CIFailure(log=log, changed_files=files, is_fork_pr=is_fork,
                                      main_history=main_hist, main_fixed_by_rerun=fixed), idx)
        state.pop("rerun_result")  # the decision is made before any rerun
        tests = failing_tests(state["error_excerpt"])
        state["failing_tests"] = tests[:5]
        if not args.no_history and tests:
            state |= context.test_history(tests, main_fail, run["created_at"], args.days)
        if not args.no_context:
            state |= context.same_error_elsewhere(mask(state["first_error_line"]), run["head_branch"],
                                                  other_branch_failures(repo, run, job["name"]), run["created_at"])
            state |= context.github_incident(run["created_at"], incidents)
            if hunks:
                state |= context.changed_lines_hit(stack_frames(state["error_excerpt"]), hunks)
        timings["preprocess"] = time.perf_counter() - t
        t = time.perf_counter()
        d = cascade.run(state, ["ci_failure_class"], context={"repo": repo, "run_id": run_id, "job": job["name"]})["ci_failure_class"]
        timings["judge"] = time.perf_counter() - t
        action = plan_action(d, is_fork_pr=is_fork, already_rerun=outcome is None and run.get("run_attempt", 1) > 1)
        results.append({"repo": repo, "run_id": run_id, "run_url": run["html_url"], "job": job["name"],
                        "job_url": job["html_url"], "is_fork_pr": is_fork, "log_lines": len(log.split("\n")),
                        "state": state, "decision": d.model_dump(), "action": vars(action), "timings_s": dict(timings),
                        "actual_outcome": outcome})
    return results


# --- terminal output ---
C = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def c(text: str, *styles: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return "".join(f"\x1b[{C[s]}m" for s in styles) + text + "\x1b[0m"


def bar(p: float, width: int = 20) -> str:
    n = round(p * width)
    return "█" * n + "·" * (width - n)


def show(r: dict) -> None:
    s, d = r["state"], r["decision"]
    print(c(f"\n▶ {r['repo']}  run {r['run_id']}  ·  {r['job']}", "bold"))
    print(c(f"  {r['job_url']}", "dim"))
    print(c("\n1. Facts (precomputed, ", "bold") + c(f"{r['log_lines']} log lines → {len(s['error_excerpt'].splitlines())}-line excerpt", "dim") + c(")", "bold"))
    print(f"  first error        {c(s['first_error_line'][:110] or '(none)', 'yellow')}")
    print(f"  touches PR files   {s['stack_overlaps_changed_files']}  {', '.join(s['overlapping_files'][:3])}")
    if "main_pass_rate" in s:
        print(f"  main history       pass rate {s['main_pass_rate']:.0%} over {s['main_runs_considered']} runs · "
              f"{s['main_failures']} failures, {s['main_failures_fixed_by_rerun']} fixed by rerun")
    if "error_seen_on_main_recently" in s:
        print(f"  error seen on main {s['error_seen_on_main_recently']}")
    if s.get("failing_tests"):
        print(f"  failing tests      {', '.join(s['failing_tests'][:3])[:100]}")
    if "same_test_failed_on_main" in s:
        print(f"  same test on main  failed {s['same_test_failed_on_main']}x, {s['same_test_fixed_by_rerun_on_main']} fixed by rerun")
    if "same_error_on_other_branches_3d" in s:
        print(f"  same error elsewhere {s['same_error_on_other_branches_3d']} other branches in the last 3 days")
    if "github_incident_active" in s:
        print(f"  GitHub incident    {s.get('github_incident', 'none')}")
    if "error_in_changed_lines" in s:
        print(f"  error in changed lines {s['error_in_changed_lines']}")
    print(f"  infra signals      {', '.join(s['infra_signals']) or '-'}")
    print(f"  env signals        {', '.join(s['env_signals'])[:100] or '-'}")
    print(f"  fork PR            {r['is_fork_pr']}")

    print(c("\n2. Cascade", "bold"))
    for t in d["tiers"]:
        name = f"  {t['judge']:<6}"
        if "error" in t:
            print(name, c(t["error"][:100], "dim"))
        elif t.get("answer") is None:
            print(name, c("no answer" + (f" → queued for review (#{t['queued_id']})" if t.get("queued_id") else ""), "dim"))
        else:
            a = t["answer"]
            top = max(a["probabilities"].values())
            verdict = c("accept", "green") if t["accepted"] else c("escalate", "yellow")
            print(f"{name} {a['value']:<15} {bar(top)} {top:.2f}  {verdict}")
            if t["judge"] not in ("rule", "human"):
                rest = sorted(a["probabilities"].items(), key=lambda kv: -kv[1])[1:3]
                print(c("         " + "   ".join(f"{k} {v:.2f}" for k, v in rest), "dim"))
    final = d["decision"] if d["status"] == "decided" else d["status"]
    p_tr = None
    for t in reversed(d["tiers"]):
        if t.get("answer") and t["answer"].get("probabilities"):
            p_tr = sum(t["answer"]["probabilities"].get(k, 0.0) for k in TRANSIENT)
            break
    print(c("\n3. Decision", "bold"))
    print(f"  {c(str(final), 'bold', 'cyan')}  by {d['decided_by'] or '-'}"
          + (f"  ·  P(rerun fixes it) {p_tr:.2f}" if p_tr is not None else "")
          + f"  ·  {d['latency_ms'] / 1000:.1f}s judging  ·  ${d['cost_usd']:.4f}")
    a = r["action"]
    print(f"  action: {c(a['kind'], 'bold', 'green' if a['kind'] == 'rerun' else 'yellow')} — {a['reason']}")
    if r["actual_outcome"]:
        print(c(f"  what actually happened: {r['actual_outcome']} (the decision above did not see this)", "cyan"))
    tm = r["timings_s"]
    print(c("  time: " + "  ".join(f"{k} {v:.1f}s" for k, v in tm.items()), "dim"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ops-judge triage", description=__doc__.split("\n\n")[0])
    ap.add_argument("url", help="GitHub Actions run (or job) URL")
    ap.add_argument("--jev", help="System One endpoint, e.g. http://127.0.0.1:8094/v1/systemone")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM tier")
    ap.add_argument("--no-history", action="store_true", help="skip main-branch history (faster)")
    ap.add_argument("--no-context", action="store_true", help="skip other-branch failures, GitHub incidents, changed lines")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-runs", type=int, default=30, help="main-branch runs to inspect for history")
    ap.add_argument("--max-jobs", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="print JSON instead of the trace")
    args = ap.parse_args(argv)
    results = triage(args)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            show(r)


if __name__ == "__main__":
    main()
