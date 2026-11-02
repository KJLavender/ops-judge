from judge_core.cascade import Decision
from ops_judge.ci import extract
from ops_judge.ci.actions import plan_action, reclassify_after_rerun
from ops_judge.ci.facts import CIFailure, build_state, overlap
from ops_judge.ci.templates import TemplateIndex

PYTEST_LOG = "\n".join(
    [f"2026-10-29T01:{i // 60:02d}:{i % 60:02d}.1234567Z \x1b[32mcollecting line {i}\x1b[0m" for i in range(200)] + [
        "2026-10-29T01:03:00.0000000Z Traceback (most recent call last):",
        '2026-10-29T01:03:00.0000000Z   File "/home/runner/work/app/app/src/billing/invoice.py", line 42, in total',
        '2026-10-29T01:03:00.0000000Z   File "/opt/hostedtoolcache/Python/3.11/lib/site-packages/x.py", line 1, in y',
        "2026-10-29T01:03:00.0000000Z AssertionError: expected 100, got 99",
        "2026-10-29T01:03:01.0000000Z ##[error]Process completed with exit code 1.",
    ])


def test_clean_strips_ansi_and_timestamps():
    out = extract.clean(PYTEST_LOG)
    assert "\x1b[" not in out and "2026-10-29T" not in out


def test_excerpt_is_short_and_keeps_the_error():
    ex = extract.error_excerpt(PYTEST_LOG)
    assert "AssertionError: expected 100, got 99" in ex
    assert len(ex.split("\n")) <= 120
    assert "collecting line 0\n" not in ex


def test_stack_files_skip_library_frames():
    files = extract.stack_files(extract.clean(PYTEST_LOG))
    assert files == ["/home/runner/work/app/app/src/billing/invoice.py"]
    assert extract.stack_files("at render (src/components/Cart.tsx:12:5)") == ["src/components/Cart.tsx"]


def test_overlap_matches_on_path_suffix():
    assert overlap(["/home/runner/work/app/app/src/billing/invoice.py"], ["src/billing/invoice.py", "README.md"]) == ["src/billing/invoice.py"]
    assert overlap(["src/a.py"], ["src/b.py"]) == []


def test_infra_and_env_signals():
    text = "npm ERR! code EAI_AGAIN\ngetaddrinfo EAI_AGAIN registry.npmjs.org\nError: connect ETIMEDOUT"
    assert set(extract.infra_signals(text)) >= {"dns", "timeout", "package_download"}
    assert extract.env_signals("KeyError: 'DATABASE_URL'")


def test_state_has_precomputed_facts():
    idx = TemplateIndex(use_drain=False)
    idx.add("AssertionError: expected 7, got 8")
    f = CIFailure(log=PYTEST_LOG, changed_files=["src/billing/invoice.py"], test_name="test_total",
                  main_history=[True] * 18 + [False] * 2, rerun_passed=None)
    s = build_state(f, idx)
    assert s["stack_overlaps_changed_files"] is True
    assert s["overlapping_files"] == ["src/billing/invoice.py"]
    assert s["main_pass_rate"] == 0.9 and s["main_runs_considered"] == 20
    assert s["rerun_result"] == "not rerun"
    assert s["error_seen_on_main_recently"] is True  # numbers masked -> same template
    assert s["first_error_line"] == "AssertionError: expected 100, got 99"


def test_template_index_with_drain():
    idx = TemplateIndex()
    for i in range(5):
        idx.add(f"Connection to db-{i}.internal timed out after {i * 100} ms")
    assert idx.contains("Connection to db-9.internal timed out after 900 ms")


def d(cls, status="decided", conf=0.9):
    return Decision(policy_id="ci_failure_class", status=status, decision=cls, decided_by="jev", confidence=conf)


def test_actions():
    assert plan_action(d("flaky_test"), is_fork_pr=False, already_rerun=False).kind == "rerun"
    assert plan_action(d("flaky_test"), is_fork_pr=True, already_rerun=False).kind == "comment_pr"
    assert plan_action(d("flaky_test"), is_fork_pr=False, already_rerun=True).kind == "comment_pr"
    infra = plan_action(d("infrastructure"), False, False)
    assert (infra.kind, infra.also_notify) == ("rerun", True)
    assert plan_action(d("infrastructure"), False, already_rerun=True).kind == "notify_platform"
    assert plan_action(d("environment"), False, False).kind == "notify_platform"
    assert plan_action(d(None, status="queued"), False, False).kind == "needs_review"
    assert reclassify_after_rerun(d("flaky_test"), rerun_passed=False).decision == "code_bug"
    assert reclassify_after_rerun(d("flaky_test"), rerun_passed=True).decision == "flaky_test"


# Regressions found on real logs (sveltejs/kit, apache/airflow, python/cpython, astral-sh/ruff).
CPYTHON_LIKE = "\n".join(
    ["clang -c foo.c"] * 50 + [
        "FAIL: test_sampling (test.test_profiling.TestSample.test_sampling)",
        "Traceback (most recent call last):",
        "AssertionError: ':' not found in '<unreadable frame>'",
        "##[error]AssertionError: ':' not found in '<unreadable frame>'",
        "Result: FAILURE",
        "##[error]Process completed with exit code 2.",
        "Post job cleanup.",
    ] + ["==17211==ERROR: AddressSanitizer: FPE on unknown address"] * 200)


def test_post_job_cleanup_is_cut_and_annotation_wins():
    ex = extract.error_excerpt(CPYTHON_LIKE)
    assert "AddressSanitizer" not in ex and "AssertionError" in ex
    s = build_state(CIFailure(log=CPYTHON_LIKE, changed_files=[]))
    assert s["first_error_line"] == "AssertionError: ':' not found in '<unreadable frame>'"


def test_signal_false_positives():
    assert extract.infra_signals("Input 'sticky_wait_timeout': 15m0s\nplugins: timeouts-1.2.1") == []
    assert extract.infra_signals("##[error]Process completed with exit code 137.") == ["sigkill"]
    assert "spot_interruption" in extract.infra_signals("##[error]AWS interrupted the EC2 Spot instance running this job.")
    assert "timeout" in extract.infra_signals("caused by: operation timed out")


def test_pytest_E_line_beats_echoed_source():
    log = "\n".join([
        ">           raise ConnectionError(",
        "                \"(Caused by NameResolutionError: Failed to resolve 'registry.example.org' \"",
        "E           ConnectionError: HTTPSConnectionPool(host='registry.example.org', port=443): Max retries exceeded",
        "tests/test_registry.py:14: ConnectionError",
        "##[error]Process completed with exit code 1.",
    ])
    s = build_state(CIFailure(log=log, changed_files=[]))
    assert s["first_error_line"].startswith("ConnectionError: HTTPSConnectionPool")
