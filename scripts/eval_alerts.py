"""Evaluate alert routing on the alert lab recordings (scripts/alert_lab.py -> data/alerts.jsonl).

Ground truth comes from the injected fault: were users affected? An *incident* is one (round, scenario) with
users affected; it is missed when none of its alerts paged. The hard requirement is zero missed incidents;
after that, fewer pages is better.

Methods
  page_all       every alert pages (no routing at all)
  critical_only  page iff severity=critical (the usual Alertmanager route)
  ops-judge      rule -> LLM cascade on the precomputed facts, fail closed, then one page per incident

Usage:
  python scripts/eval_alerts.py [--no-llm]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from judge_core import Cascade, LLMJudge, RuleJudge, Store, load_policies  # noqa: E402
from judge_core.evaluate import rule_of_three  # noqa: E402
from ops_judge.alerts.route import ongoing, route  # noqa: E402
from ops_judge.alerts.watch import _Skipped  # noqa: E402

CACHE = ROOT / "examples/out/alert_decisions.jsonl"


def judge_all(rows: list[dict], use_llm: bool) -> dict[str, dict]:
    """Cascade decisions per alert, cached by alert id (each alert is one LLM call)."""
    cache = {d["id"]: d for d in map(json.loads, CACHE.read_text(encoding="utf-8").splitlines())} if CACHE.exists() else {}
    policies = load_policies(ROOT / "policies/alerts.yaml")
    llm = LLMJudge() if use_llm else _Skipped("llm", "--no-llm")
    cascade = Cascade(policies, {"rule": RuleJudge.from_policies(policies), "jev": _Skipped("jev", "not used"), "llm": llm}, Store())
    tag = "llm" if use_llm else "rule-only"
    for r in rows:
        key = f"{tag}:{r['id']}"
        if key not in cache:
            out = cascade.run(r["state"])
            cache[key] = {"id": key, "page": out["alert_page"].model_dump(), "severity": out["alert_severity"].model_dump()}
            print(f"  {r['id']:<40} page={cache[key]['page']['decision']} by {cache[key]['page']['decided_by']}", file=sys.stderr)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text("\n".join(json.dumps(d) for d in cache.values()), encoding="utf-8")
    return {r["id"]: cache[f"{tag}:{r['id']}"] for r in rows}


def score(rows: list[dict], paged: dict[str, bool]) -> dict:
    incidents = defaultdict(bool)
    for r in rows:
        if r["users_affected"]:
            incidents[(r["round"], r["scenario"])] |= paged[r["id"]]
    impact_scenarios = {(r["round"], r["scenario"]) for r in rows if r["users_affected"]}
    missed = [f"r{k[0]}-{k[1]}" for k in sorted(impact_scenarios) if not incidents[k]]
    pages = sum(paged.values())
    false_pages = sum(paged[r["id"]] for r in rows if not r["users_affected"])
    correct = sum(paged[r["id"]] == r["users_affected"] for r in rows)
    return {"alerts": len(rows), "pages": pages, "false_pages": false_pages, "incidents": len(impact_scenarios),
            "missed_incidents": missed, "missed_upper95": rule_of_three(len(impact_scenarios)) if not missed else None,
            "per_alert_accuracy": correct / len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/alerts.jsonl"))
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    rows = sorted((json.loads(x) for x in Path(args.data).read_text(encoding="utf-8").splitlines() if x), key=lambda r: r["t"])

    results = {
        "page_all": score(rows, {r["id"]: True for r in rows}),
        "critical_only": score(rows, {r["id"]: r["labels"].get("severity") == "critical" for r in rows}),
    }
    decisions = judge_all(rows, use_llm=not args.no_llm)
    judged = {r["id"]: decisions[r["id"]]["page"]["decision"] is True for r in rows}
    results["ops-judge (judge only)"] = score(rows, judged)

    # Full routing: an alert is muted only while another alert on the same service that already paged is still
    # firing (known at decision time from other_alerts_firing_now).
    from judge_core.cascade import Decision
    paged: dict[str, str] = {}
    routed, routes = {}, {}
    for r in rows:
        d, s = decisions[r["id"]], r["state"]
        rt = route(Decision(**d["page"]), Decision(**d["severity"]), ongoing(paged, s["service"], s["other_alerts_firing_now"]))
        if rt.kind == "page":
            paged[s["alert"]] = s["service"]
        routed[r["id"]], routes[r["id"]] = rt.kind == "page", rt.kind
    results["ops-judge (with dedupe)"] = score(rows, routed)

    report = {"methods": results, "alerts": [
        {"id": r["id"], "scenario": r["scenario"], "users_affected": r["users_affected"], "alert": r["state"]["alert"],
         "severity": r["labels"].get("severity"), "facts": r["state"], "page_decision": decisions[r["id"]]["page"],
         "route": routes[r["id"]]} for r in rows]}
    (ROOT / "data/alerts_eval.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} alerts from {len({(r['round'], r['scenario']) for r in rows})} scenario runs")
    for name, m in results.items():
        print(f"{name:26} pages {m['pages']:3}/{m['alerts']}  false pages {m['false_pages']:3}  "
              f"missed incidents {len(m['missed_incidents'])}/{m['incidents']} {m['missed_incidents'] or ''}  "
              f"per-alert acc {m['per_alert_accuracy']:.0%}")


if __name__ == "__main__":
    main()
