import pytest

from ops_judge.ci import executor
from ops_judge.ci.actions import Action
from ops_judge.ci.executor import Context, execute, followup_after_rerun, pr_number


@pytest.fixture
def gh_calls(monkeypatch):
    calls, comments = [], []
    monkeypatch.setattr(executor, "run_cmd", lambda cmd, dry_run: None if dry_run else calls.append(cmd))
    monkeypatch.setattr(executor, "gh", lambda path, raw=False: [{"id": i, "body": b} for i, b in enumerate(comments)])
    monkeypatch.delenv("OPSJUDGE_NOTIFY_WEBHOOK", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return calls, comments


def ctx(**kw):
    base = dict(repo="o/r", run_id=7, run_attempt=1, job="test", job_url="https://x/job/1", pr_number=3,
                decision={"status": "decided", "decision": "flaky_test", "confidence": 0.9, "decided_by": "llm"},
                state={"first_error_line": "AssertionError: 1 != 2", "overlapping_files": ["src/a.py"],
                       "main_pass_rate": 0.9, "main_runs_considered": 20})
    return Context(**(base | kw))


def test_rerun_then_comment_once(gh_calls):
    calls, comments = gh_calls
    out = execute(ctx(), Action("rerun", "flaky"))
    assert calls[0] == ["gh", "run", "rerun", "7", "--failed", "-R", "o/r"]
    assert calls[1][:4] == ["gh", "pr", "comment", "3"] and "<!-- ops-judge:7:test -->" in calls[1][-1]
    assert "`src/a.py`" in calls[1][-1] and len(out.done) == 2
    comments.append(calls[1][-1])  # the comment now exists on the PR
    out2 = execute(ctx(), Action("rerun", "flaky"))
    assert len(calls) == 3 and any("already has this note" in s for s in out2.skipped)


def test_better_informed_triage_updates_the_note(gh_calls):
    calls, comments = gh_calls
    unsure = {"status": "queued", "decision": None, "confidence": None, "decided_by": None}
    execute(ctx(decision=unsure), Action("needs_review", "?"))
    comments.append(calls[-1][-1])
    out = execute(ctx(), Action("rerun", "flaky"))
    assert calls[-1][:4] == ["gh", "api", "-X", "PATCH"] and "issues/comments/0" in calls[-1][4]
    assert "**flaky_test**" in calls[-1][-1] and "updated the note" in out.done[-1]


def test_never_reruns_a_rerun_and_dry_run_does_nothing(gh_calls):
    calls, _ = gh_calls
    out = execute(ctx(run_attempt=2), Action("rerun", "flaky"))
    assert "rerun: already rerun once" in out.skipped and all(c[1] != "run" for c in calls)
    calls.clear()
    out = execute(ctx(), Action("rerun", "flaky"), dry_run=True)
    assert calls == [] and all(x.startswith("[dry-run]") for x in out.done)


def test_infrastructure_reruns_and_notifies_job_summary(gh_calls, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = execute(ctx(), Action("rerun", "infra", also_notify=True))
    assert "wrote job summary" in out.done and "environment/infrastructure" in summary.read_text()


def test_no_pr_and_later_attempt_are_skipped(gh_calls):
    calls, _ = gh_calls
    assert any("not attached to a pull request" in s for s in execute(ctx(pr_number=None), Action("needs_review", "?")).skipped)
    out = execute(ctx(actual_outcome="attempt 2 concluded success"), Action("rerun", "flaky"))
    assert calls == [] and out.done == []


def test_followup_only_after_our_own_note(gh_calls):
    calls, comments = gh_calls
    run = {"id": 7, "pull_requests": [{"number": 3, "base": {"repo": {"url": "https://api.github.com/repos/o/r"}}}]}
    assert followup_after_rerun("o/r", run, "test", "u").done == []      # we never commented: stay quiet
    comments.append("<!-- ops-judge:7:test --> **code_bug**")
    assert followup_after_rerun("o/r", run, "test", "u").done == []      # we never called it a flake
    comments[0] = "<!-- ops-judge:7:test --> **flaky_test**"
    assert len(followup_after_rerun("o/r", run, "test", "u").done) == 1
    assert "failed again after the rerun" in calls[-1][-1]


def test_pr_number_ignores_other_repos():
    run = {"pull_requests": [{"number": 9, "base": {"repo": {"url": "https://api.github.com/repos/other/x"}}},
                             {"number": 3, "base": {"repo": {"url": "https://api.github.com/repos/o/r"}}}]}
    assert pr_number("o/r", run) == 3


def test_comment_without_history_does_not_crash(gh_calls):
    calls, _ = gh_calls
    execute(ctx(state={"first_error_line": "boom", "main_pass_rate": None, "main_runs_considered": 0}), Action("needs_review", "?"))
    assert "boom" in calls[-1][-1] and "recent runs" not in calls[-1][-1]
