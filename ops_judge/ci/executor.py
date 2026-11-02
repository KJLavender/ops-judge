"""Carry out a planned action against GitHub through the `gh` CLI.

Every action is idempotent: PR comments carry a hidden marker per (run, job), so a second triage of the
same failure edits nothing, and a run is rerun at most once (only from attempt 1).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass, field

from .actions import Action
from .github import gh

MARKER = "<!-- ops-judge:{run_id}:{job} -->"
TRANSIENT = ("flaky_test", "infrastructure")


@dataclass
class Context:
    repo: str
    run_id: int
    run_attempt: int
    job: str
    job_url: str
    pr_number: int | None
    decision: dict
    state: dict
    actual_outcome: str | None = None


@dataclass
class Outcome:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"done": self.done, "skipped": self.skipped}


def pr_number(repo: str, run: dict) -> int | None:
    """The run's PR in this repo (`pull_requests` may also list PRs of other repos sharing the commit)."""
    prs = [p for p in run.get("pull_requests") or [] if p["base"]["repo"]["url"].endswith(f"/repos/{repo}")]
    return prs[0]["number"] if prs else None


def comment_body(ctx: Context, action: Action) -> str:
    d, s = ctx.decision, ctx.state
    verdict = d["decision"] if d["status"] == "decided" else "not sure"
    lines = [MARKER.format(run_id=ctx.run_id, job=ctx.job),
             f"**ops-judge** looked at the failed job [`{ctx.job}`]({ctx.job_url}): **{verdict}**"
             + (f" (confidence {d['confidence']:.2f}, decided by {d['decided_by']})" if d["status"] == "decided" else ""),
             ""]
    if s.get("first_error_line"):
        lines += ["First error:", "```", s["first_error_line"][:300], "```"]
    if s.get("overlapping_files"):
        lines.append("The stack trace touches files changed in this PR: " + ", ".join(f"`{f}`" for f in s["overlapping_files"][:5]))
    if "main_pass_rate" in s:
        lines.append(f"This job passed {s['main_pass_rate']:.0%} of {s['main_runs_considered']} recent runs on the default branch.")
    lines += ["", {
        "rerun": "Rerunning the failed jobs once.",
        "comment_pr": f"Next step: {action.reason}.",
        "notify_platform": "Looks like an environment / infrastructure problem; the platform team has been told.",
        "needs_review": "Not confident enough to act automatically. A person should take a look.",
    }.get(action.kind, action.reason)]
    lines.append("\n<sub>Automated triage. Reply here if this call was wrong; it helps tune the thresholds.</sub>")
    return "\n".join(lines)


def already_commented(repo: str, pr: int, marker: str) -> bool:
    try:
        comments = gh(f"repos/{repo}/issues/{pr}/comments?per_page=100")
    except RuntimeError:
        return False
    return any(marker in (c.get("body") or "") for c in comments)


def run_cmd(cmd: list[str], dry_run: bool) -> None:
    if dry_run:
        return
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300])


def notify(ctx: Context, text: str, dry_run: bool) -> str:
    """Slack-compatible webhook if OPSJUDGE_NOTIFY_WEBHOOK is set, else the GitHub Actions job summary."""
    hook = os.environ.get("OPSJUDGE_NOTIFY_WEBHOOK")
    if hook:
        if not dry_run:
            req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        return "notified webhook"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        if not dry_run:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(text + "\n\n")
        return "wrote job summary"
    return "no notify target (set OPSJUDGE_NOTIFY_WEBHOOK)"


def execute(ctx: Context, action: Action, dry_run: bool = False) -> Outcome:
    out = Outcome()
    tag = "[dry-run] " if dry_run else ""
    if ctx.actual_outcome:
        out.skipped.append(f"run already has a later attempt ({ctx.actual_outcome}); nothing to do")
        return out

    if action.kind == "rerun":
        if ctx.run_attempt > 1:
            out.skipped.append("rerun: already rerun once")
        else:
            run_cmd(["gh", "run", "rerun", str(ctx.run_id), "--failed", "-R", ctx.repo], dry_run)
            out.done.append(f"{tag}rerun failed jobs of run {ctx.run_id}")

    if action.kind == "notify_platform" or action.also_notify:
        text = f"ops-judge: {ctx.repo} job `{ctx.job}` failed with an environment/infrastructure problem: " \
               f"{ctx.state.get('first_error_line', '')[:200]} ({ctx.job_url})"
        out.done.append(tag + notify(ctx, text, dry_run))

    # Every outcome is explained on the PR (once), so the author knows what happened and why.
    if ctx.pr_number is None:
        out.skipped.append("comment: run is not attached to a pull request in this repo")
    else:
        marker = MARKER.format(run_id=ctx.run_id, job=ctx.job)
        if already_commented(ctx.repo, ctx.pr_number, marker):
            out.skipped.append(f"comment: PR #{ctx.pr_number} already has the ops-judge note for this job")
        else:
            body = comment_body(ctx, action)
            run_cmd(["gh", "pr", "comment", str(ctx.pr_number), "-R", ctx.repo, "--body", body], dry_run)
            out.done.append(f"{tag}commented on PR #{ctx.pr_number}")
    return out


def followup_after_rerun(repo: str, run: dict, job: str, job_url: str, dry_run: bool = False) -> Outcome:
    """Called when attempt 2+ of a run fails: a job ops-judge called flaky that fails again is a real bug."""
    out = Outcome()
    pr = pr_number(repo, run)
    if pr is None:
        return out
    marker = MARKER.format(run_id=run["id"], job=job)
    followup = MARKER.format(run_id=run["id"], job=job + ":followup")
    if already_commented(repo, pr, marker) and not already_commented(repo, pr, followup):
        body = f"{followup}\n**ops-judge**: [`{job}`]({job_url}) failed again after the rerun, so this is probably " \
               f"not a flake. Treating it as a code problem."
        run_cmd(["gh", "pr", "comment", str(pr), "-R", repo, "--body", body], dry_run)
        out.done.append(f"{'[dry-run] ' if dry_run else ''}follow-up comment on PR #{pr}")
    return out
