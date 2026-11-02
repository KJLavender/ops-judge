from pathlib import Path

from conftest import FakeJudge
from judge_core.cascade import Cascade, Decision
from judge_core.judges.rule import RuleJudge
from judge_core.policy import load_policies
from judge_core.store import Store
from judge_core.types import Answer
from ops_judge.alerts.facts import AlertHistory, build_state
from ops_judge.alerts.route import ongoing, route

ROOT = Path(__file__).resolve().parents[1]
POLICIES = load_policies(ROOT / "policies/alerts.yaml")


class FakeProm:
    def __init__(self, values: dict[str, float | None]):
        self.values = values

    def value(self, expr: str):
        return next((v for k, v in self.values.items() if k in expr), None)


ALERT = {"labels": {"alertname": "ShopHighErrorRate", "namespace": "lab", "service": "shop", "severity": "critical"},
         "annotations": {"summary": "errors"}}
HEALTHY = {'code=~"5.."': 0.31, "histogram_quantile": 0.05, "replicas_available": 2, "spec_replicas": 2,
           "kube_replicaset_created": 1000.0}


def test_facts_are_precomputed_answers():
    h = AlertHistory()
    h.add("ShopHighErrorRate", "shop", 1000 + 60)
    firing = [ALERT | {"state": "firing"},
              {"labels": {"alertname": "ShopSlowResponses", "namespace": "lab"}, "state": "firing"},
              {"labels": {"alertname": "Other", "namespace": "elsewhere"}, "state": "firing"}]
    s = build_state(ALERT, FakeProm(HEALTHY), h, now=1000 + 600, firing=firing)
    assert s["user_request_error_ratio_1m"] == 0.31 and s["service_fully_down"] is False
    assert s["other_alerts_firing_now"] == ["ShopSlowResponses"]
    assert s["same_alert_fired_in_last_hour"] == 1 and s["minutes_since_last_deploy"] == 10.0


def test_fully_down_pages_by_rule_without_any_model():
    s = build_state(ALERT, FakeProm(HEALTHY | {"replicas_available": 0}), AlertHistory(), now=2000)
    llm = FakeJudge("llm", {"alert_page": Answer.noul(0.0)})
    out = Cascade(POLICIES, {"rule": RuleJudge.from_policies(POLICIES), "jev": FakeJudge("jev", fail=True), "llm": llm},
                  Store()).run(s)
    assert (out["alert_page"].decision, out["alert_page"].decided_by) == (True, "rule")


def test_no_model_available_fails_closed():
    s = build_state(ALERT, FakeProm(HEALTHY), AlertHistory(), now=2000)
    out = Cascade(POLICIES, {"rule": RuleJudge.from_policies(POLICIES), "jev": FakeJudge("jev", fail=True),
                             "llm": FakeJudge("llm", fail=True)}, Store()).run(s)
    assert out["alert_page"].decision is True and out["alert_page"].status == "fallback"


def d(value, status="decided"):
    return Decision(policy_id="alert_page", status=status, decision=value, decided_by="llm")


def test_route_pages_once_per_incident_not_per_time_window():
    paged = {"ShopHighErrorRate": "shop"}
    # The paged alert is still firing: same incident, don't wake anyone twice.
    assert route(d(True), None, ongoing(paged, "shop", ["ShopHighErrorRate"])).kind == "mute"
    # It has resolved: a new alert minutes later is a new incident and must page.
    assert route(d(True), None, ongoing(paged, "shop", [])).kind == "page"
    # Another service's incident doesn't mute this one.
    assert route(d(True), None, ongoing(paged, "batch", ["ShopHighErrorRate"])).kind == "page"
    assert route(d(False), None, None).kind == "digest"
