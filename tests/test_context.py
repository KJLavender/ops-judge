from ops_judge.ci import context
from ops_judge.ci.extract import failing_tests, stack_frames
from ops_judge.ci.github import parse_hunks

WHEN = "2026-10-20T12:00:00Z"


def test_failing_tests_across_runners():
    log = "\n".join([
        "FAILED tests/test_a.py::test_x - assert 1 == 2",
        "FAIL: test_sampling (test.test_profiling.TestSample.test_sampling)",
        " FAIL  kit-server-dev  src/core/sync/index.spec.js > reports errors",
        "  1) [chromium] › tests/basics.spec.ts:12:5 › shows toast ─────",
        "test parser::tests::bad ... FAILED",
        "--- FAIL: TestFoo (0.01s)",
    ])
    assert failing_tests(log) == ["tests/test_a.py::test_x", "test.test_profiling.TestSample.test_sampling",
                                  "src/core/sync/index.spec.js > reports errors", "tests/basics.spec.ts › shows toast",
                                  "parser::tests::bad", "TestFoo"]


def test_frames_and_hunks():
    assert stack_frames('File "/w/app/src/a.py", line 42, in f\n  at x (src/ui/Cart.tsx:12:5)') == \
        [("/w/app/src/a.py", 42), ("src/ui/Cart.tsx", 12)]
    assert parse_hunks("@@ -1,3 +10,5 @@ x\n@@ -40 +44 @@") == [(10, 14), (44, 44)]


def test_test_history_only_counts_the_past():
    rows = [
        {"created_at": "2026-10-19T00:00:00Z", "tests": ["t1"], "fixed_by_rerun": True},
        {"created_at": "2026-10-18T00:00:00Z", "tests": ["t2"], "fixed_by_rerun": False},
        {"created_at": "2026-10-21T00:00:00Z", "tests": ["t1"], "fixed_by_rerun": True},  # future: ignored
        {"created_at": "2026-09-01T00:00:00Z", "tests": ["t1"], "fixed_by_rerun": True},  # too old: ignored
    ]
    assert context.test_history(["t1"], rows, WHEN) == {"same_test_failed_on_main": 1, "same_test_fixed_by_rerun_on_main": 1}


def test_same_error_elsewhere_counts_other_branches_in_window():
    rows = [
        {"created_at": "2026-10-19T00:00:00Z", "template": "E", "head_branch": "a"},
        {"created_at": "2026-10-19T01:00:00Z", "template": "E", "head_branch": "a"},   # same branch twice: one
        {"created_at": "2026-10-19T02:00:00Z", "template": "E", "head_branch": "mine"},  # own branch: ignored
        {"created_at": "2026-10-19T03:00:00Z", "template": "X", "head_branch": "b"},   # other error
        {"created_at": "2026-10-20T13:00:00Z", "template": "E", "head_branch": "c"},   # after: ignored
        {"created_at": "2026-10-18T13:00:00Z", "template": "E", "head_branch": "d"},
    ]
    assert context.same_error_elsewhere("E", "mine", rows, WHEN) == {"same_error_on_other_branches_3d": 2}
    assert context.same_error_elsewhere("", "mine", rows, WHEN) == {"same_error_on_other_branches_3d": 0}


def test_github_incident_window():
    inc = [{"name": "Incident with Actions", "created_at": "2026-10-20T11:00:00Z", "resolved_at": "2026-10-20T13:00:00Z"},
           {"name": "Later", "created_at": "2026-10-20T12:30:00Z", "resolved_at": None}]
    assert context.github_incident(WHEN, inc) == {"github_incident_active": True, "github_incident": "Incident with Actions"}
    assert context.github_incident("2026-10-20T10:00:00Z", inc) == {"github_incident_active": False}
    copilot = [{"name": "Disruption with some Copilot services", "created_at": "2026-10-20T11:00:00Z", "resolved_at": None}]
    assert context.github_incident(WHEN, copilot) == {"github_incident_active": False}


def test_error_in_changed_lines():
    hunks = {"src/a.py": [(40, 45)]}
    assert context.changed_lines_hit([("/w/app/src/a.py", 42)], hunks) == {"error_in_changed_lines": True}
    assert context.changed_lines_hit([("/w/app/src/a.py", 90)], hunks) == {"error_in_changed_lines": False}
    assert context.changed_lines_hit([("/w/app/src/b.py", 42)], hunks) == {"error_in_changed_lines": False}
