"""Run the 20-item toy CI dataset through each backend, then the full YAML cascade.

Outputs (examples/out/):
  preds_<judge>.jsonl   cached predictions (delete to re-run a backend)
  sweep.png             coverage vs error rate
  calibration.png       reliability curves + ECE
  compare.json          accuracy / latency / cost / error correlation
  audit.sqlite          audit log + human queue from the cascade run

Usage:
  python scripts/toy_eval.py                 # llm (claude CLI); jev too if TYPESAFE_API_KEY is set
  python scripts/toy_eval.py --judges jev,llm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from judge_core import Cascade, HumanQueue, JevJudge, LLMJudge, RuleJudge, Store, load_policies  # noqa: E402
from judge_core.evaluate import (Prediction, calibration, compare, plot_reliability, plot_sweep,  # noqa: E402
                                 run_judge, sweep_thresholds)
from judge_core.types import JudgeError, JudgeRequest, JudgeResult  # noqa: E402

OUT = ROOT / "examples/out"


class ReplayJudge:
    """Serves cached predictions so the cascade run does not call the backend again."""

    def __init__(self, name: str, preds: list[Prediction], dataset: list[dict]):
        self.name = name
        by_id = {p.id: p for p in preds}
        self.by_hash = {JudgeRequest(state=x["state"], questions=[]).input_hash(): by_id.get(x["id"]) for x in dataset}

    def judge(self, req: JudgeRequest) -> JudgeResult:
        p = self.by_hash.get(req.input_hash())
        if p is None or p.answer is None:
            raise JudgeError(f"{self.name}: no cached prediction")
        return JudgeResult(judge=self.name, latency_ms=p.latency_ms, cost_usd=p.cost_usd,
                           answers={q.key: p.answer for q in req.questions})


def cached_preds(name: str, judge, dataset: list[dict], question, workers: int) -> list[Prediction]:
    """Judge only what the cache lacks. An entry is reused when its id matches and, if it recorded one,
    its input hash matches too: changed facts mean a fresh judgment, never a stale answer."""
    path = OUT / f"preds_{name}.jsonl"
    cache = ({p.id: p for p in map(Prediction.model_validate_json, path.read_text(encoding="utf-8").splitlines())}
             if path.exists() else {})

    def fresh(x: dict) -> bool:
        p = cache.get(x["id"])
        return bool(p and p.answer and (p.input_hash is None
                                        or p.input_hash == JudgeRequest(state=x["state"], questions=[]).input_hash()))

    todo = [x for x in dataset if not fresh(x)]
    if todo:
        print(f"running {name} on {len(todo)} of {len(dataset)} items ...", file=sys.stderr)
        with ThreadPoolExecutor(workers) as pool:
            for p in (p for chunk in pool.map(lambda x: run_judge(judge, [x], question), todo) for p in chunk):
                cache[p.id] = p
        path.write_text("\n".join(p.model_dump_json() for p in cache.values()), encoding="utf-8")
    return [cache[x["id"]] for x in dataset]


def main() -> None:
    ap = argparse.ArgumentParser()
    default = "jev,llm" if os.environ.get("TYPESAFE_API_KEY") else "llm"
    ap.add_argument("--judges", default=default)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    dataset = [json.loads(line) for line in (ROOT / "examples/toy_ci.jsonl").read_text(encoding="utf-8").splitlines()]
    golds = {x["id"]: x["label"] for x in dataset}
    policies = load_policies(ROOT / "policies/ci.yaml")
    question = policies[0].to_question()

    factories = {"jev": JevJudge, "llm": LLMJudge}
    results = {n: cached_preds(n, factories[n](), dataset, question, args.workers) for n in args.judges.split(",")}

    plot_sweep({n: sweep_thresholds(p, golds) for n, p in results.items()}, str(OUT / "sweep.png"),
               "ci_failure_class: coverage vs error (toy, n=20)")
    cals = {n: calibration(p, golds, n_bins=5) for n, p in results.items()}
    plot_reliability(cals, str(OUT / "calibration.png"), "ci_failure_class calibration (toy, n=20)")
    report = compare(results, golds)
    (OUT / "compare.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Full cascade from YAML: rule -> jev -> llm -> human, with audit log.
    db = OUT / "audit.sqlite"
    db.unlink(missing_ok=True)
    store = Store(db)
    judges = {"rule": RuleJudge.from_policies(policies), "human": HumanQueue(store),
              "jev": ReplayJudge("jev", results["jev"], dataset) if "jev" in results else JevJudge(),
              "llm": ReplayJudge("llm", results["llm"], dataset) if "llm" in results else LLMJudge()}
    cascade = Cascade(policies, judges, store)
    decisions = [cascade.run(x["state"])["ci_failure_class"] for x in dataset]
    correct = sum(d.status == "decided" and d.decision == x["label"] for d, x in zip(decisions, dataset))

    print(json.dumps(report["backends"], indent=2))
    print("pairs:", json.dumps(report["pairs"]))
    print("ECE:", {n: round(c["ece"], 3) for n, c in cals.items()})
    print("cascade decided_by:", dict(Counter(d.decided_by or d.status for d in decisions)))
    print(f"cascade correct on decided: {correct}/{sum(d.status == 'decided' for d in decisions)}; "
          f"queued for human: {len(store.pending())}; audit rows: {len(store.decisions())}")


if __name__ == "__main__":
    main()
