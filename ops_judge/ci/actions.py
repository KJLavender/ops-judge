"""Map a ci_failure_class decision to what the bot does next."""

from __future__ import annotations

from dataclasses import dataclass

from judge_core.cascade import Decision


@dataclass
class Action:
    kind: str     # rerun | comment_pr | notify_platform | needs_review | none
    reason: str
    also_notify: bool = False  # tell the platform team as well (infrastructure that a rerun may fix)


def plan_action(d: Decision, is_fork_pr: bool, already_rerun: bool) -> Action:
    if d.status != "decided":
        return Action("needs_review", f"undecided ({d.status}); waiting for a human")
    cls = d.decision
    if cls == "flaky_test":
        if is_fork_pr:
            return Action("comment_pr", "looks flaky, but fork PRs are never auto-rerun (log content is author-controlled)")
        if already_rerun:
            return Action("comment_pr", "already rerun once; not rerunning again")
        return Action("rerun", f"flaky_test with confidence {d.confidence:.2f}")
    if cls == "code_bug":
        return Action("comment_pr", "code_bug: posting key error lines and overlapping files")
    if cls == "infrastructure" and not is_fork_pr and not already_rerun:
        # Runner loss, registry outages, spot reclaims: usually gone on the next attempt (see the rerun data).
        return Action("rerun", "infrastructure: rerun and tell the platform team", also_notify=True)
    if cls in ("environment", "infrastructure"):
        return Action("notify_platform", f"{cls}: notify the platform team, do not ping the PR author")
    return Action("needs_review", f"unexpected class {cls!r}")


def reclassify_after_rerun(d: Decision, rerun_passed: bool) -> Decision:
    """A 'flaky' failure that fails again on rerun is treated as a real bug."""
    if d.decision == "flaky_test" and not rerun_passed:
        return d.model_copy(update={"decision": "code_bug", "decided_by": "rerun_failed"})
    return d
