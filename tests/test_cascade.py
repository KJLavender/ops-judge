from pathlib import Path

import pytest

from conftest import FakeJudge
from judge_core.calibration import PlattCalibrator
from judge_core.cascade import Cascade
from judge_core.judges.human import HumanQueue
from judge_core.judges.rule import RuleJudge
from judge_core.policy import load_policies
from judge_core.store import Store
from judge_core.types import Answer

ROOT = Path(__file__).resolve().parents[1]
CI = load_policies(ROOT / "policies/ci.yaml")
ALERTS = load_policies(ROOT / "policies/alerts.yaml")
OPTS = ["code_bug", "flaky_test", "environment", "infrastructure", "unknown"]


def choice(top: str, p: float) -> Answer:
    rest = (1 - p) / (len(OPTS) - 1)
    return Answer.from_probs("choice", {o: (p if o == top else rest) for o in OPTS})


def ci_cascade(jev_ans=None, llm_ans=None, jev_fail=False, store=None):
    store = store or Store()
    judges = {"rule": RuleJudge.from_policies(CI),
              "jev": FakeJudge("jev", {"ci_failure_class": jev_ans} if jev_ans else {}, fail=jev_fail),
              "llm": FakeJudge("llm", {"ci_failure_class": llm_ans} if llm_ans else {}),
              "human": HumanQueue(store)}
    return Cascade(CI, judges, store), judges, store


STATE = {"first_error_line": "AssertionError", "error_excerpt": "E   AssertionError: 1 != 2"}


def test_rule_hit_short_circuits():
    c, judges, store = ci_cascade(jev_ans=choice("code_bug", 0.99))
    d = c.run({"error_excerpt": "OSError: No space left on device"})["ci_failure_class"]
    assert (d.status, d.decision, d.decided_by) == ("decided", "infrastructure", "rule")
    assert judges["jev"].requests == []
    assert store.decisions()[0]["path"] == ["rule"]


def test_confident_jev_decides():
    c, judges, _ = ci_cascade(jev_ans=choice("code_bug", 0.9), llm_ans=choice("flaky_test", 0.99))
    d = c.run(STATE)["ci_failure_class"]
    assert (d.decision, d.decided_by, d.path) == ("code_bug", "jev", ["rule", "jev"])
    assert judges["llm"].requests == []


def test_flaky_needs_stricter_threshold():
    c, _, _ = ci_cascade(jev_ans=choice("flaky_test", 0.8), llm_ans=choice("flaky_test", 0.9))
    d = c.run(STATE)["ci_failure_class"]
    assert d.decided_by == "llm"  # 0.8 passes the global 0.7 but not flaky's 0.85


def test_unknown_always_escalates_and_lands_in_human_queue():
    c, _, store = ci_cascade(jev_ans=choice("unknown", 0.99), llm_ans=choice("code_bug", 0.5))
    d = c.run(STATE)["ci_failure_class"]
    assert d.status == "queued" and d.path == ["rule", "jev", "llm", "human"]
    assert len(store.pending("ci_failure_class")) == 1


def test_backend_failure_moves_to_next_tier():
    c, _, store = ci_cascade(jev_fail=True, llm_ans=choice("environment", 0.95))
    d = c.run(STATE)["ci_failure_class"]
    assert d.decided_by == "llm" and "error" in d.tiers[1]
    assert store.decisions()[0]["decided_by"] == "llm"


def test_human_label_is_reused():
    store = Store()
    c, _, _ = ci_cascade(jev_ans=choice("code_bug", 0.4), store=store)
    assert c.run(STATE)["ci_failure_class"].status == "queued"
    store.label(store.pending()[0]["id"], "environment")
    d = c.run(STATE)["ci_failure_class"]
    assert (d.decision, d.decided_by) == ("environment", "human")
    assert len(store.export_labels()) == 1


def alert_cascade(jev, llm):
    return Cascade(ALERTS, {"jev": jev, "llm": llm})


def test_alert_questions_batched_into_one_request():
    sev = Answer.from_probs("score", {"0": 0.1, "1": 0.1, "2": 0.6, "3": 0.1, "4": 0.1})
    jev = FakeJudge("jev", {"alert_page": Answer.noul(0.9), "alert_severity": sev})
    out = alert_cascade(jev, FakeJudge("llm")).run({"alert": "HighErrorRate"})
    assert len(jev.requests) == 1 and len(jev.requests[0].questions) == 2
    assert out["alert_page"].decision is True
    assert out["alert_severity"].decision == 2


@pytest.mark.parametrize("jev_p,llm,expect_status", [
    (0.3, FakeJudge("llm", {"alert_page": Answer.noul(0.4)}), "fallback"),  # gray all the way -> page
    (0.3, FakeJudge("llm", fail=True), "fallback"),                          # llm down -> page
])
def test_gating_fails_closed(jev_p, llm, expect_status):
    jev = FakeJudge("jev", {"alert_page": Answer.noul(jev_p)})
    d = alert_cascade(jev, llm).run({"alert": "x"}, ["alert_page"])["alert_page"]
    assert (d.status, d.decision) == (expect_status, True)


def test_gating_both_backends_down_still_pages():
    d = alert_cascade(FakeJudge("jev", fail=True), FakeJudge("llm", fail=True)).run({"a": 1}, ["alert_page"])["alert_page"]
    assert d.decision is True


def test_calibration_changes_the_verdict():
    # Jev's noul is under-confident: raw 0.5 sits in the gray zone, calibrated it clears 0.6.
    jev = FakeJudge("jev", {"alert_page": Answer.noul(0.5)})
    llm = FakeJudge("llm", {"alert_page": Answer.noul(0.3)})
    c = Cascade(ALERTS, {"jev": jev, "llm": llm}, calibrators={("alert_page", "jev"): PlattCalibrator(a=2.0, b=1.0)})
    d = c.run({"a": 1}, ["alert_page"])["alert_page"]
    assert d.decided_by == "jev" and d.tiers[0]["calibrated"]
    assert d.tiers[0]["raw"]["value"] == 0.5 and d.tiers[0]["answer"]["value"] > 0.6


def test_missing_judge_rejected():
    with pytest.raises(ValueError):
        Cascade(CI, {"jev": FakeJudge("jev")})
