"""Evaluate ci_failure_class on real rerun-labeled failures (build_dataset.py output).

Gold is binary: did a rerun fix it (transient) or not (persistent)? Judges output the 5-way class, so
the score is P(transient) = P(flaky_test) + P(infrastructure). Rows are deduplicated by (run, error
template) so one matrix job failing 50 times does not dominate.

Outputs examples/out/reruns_{sweep,calibration}.png and reruns_report.json.

Usage:
  python scripts/eval_reruns.py [--judges llm] [--data data/ci_eval.jsonl]
  python scripts/eval_reruns.py --judges llm,kev@http://127.0.0.1:8094/v1/systemone,laya@http://127.0.0.1:8092/v1/systemone
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from judge_core import JevJudge, LLMJudge, RuleJudge, load_policies  # noqa: E402
from judge_core.evaluate import Prediction, calibration, plot_reliability, plot_sweep, sweep_thresholds  # noqa: E402
from judge_core.types import Answer, JudgeRequest, JudgeResult, Question  # noqa: E402
from ops_judge.ci.templates import mask  # noqa: E402
from toy_eval import OUT, cached_preds  # noqa: E402

TRANSIENT = ("flaky_test", "infrastructure")


def dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        key = (r["id"].split("#")[1], mask(r["state"]["first_error_line"]))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def p_transient(a: Answer) -> float:
    if a.kind == "noul":  # split-question runs answer P(transient) directly
        return float(a.value)
    return sum(a.probabilities.get(k, 0.0) for k in TRANSIENT)  # type: ignore[union-attr]


# Two plain yes/no questions instead of one 5-way choice: small decision models handle these better.
SPLIT_QUESTIONS = [
    Question(key="environment_caused", kind="noul",
             instructions="Was this CI failure caused by the environment it ran in (network, package registry, "
                          "runner or machine problems, cloud capacity) rather than by the code being tested?"),
    Question(key="intermittent", kind="noul",
             instructions="Does the evidence suggest this failure is intermittent, so the same code would likely "
                          "pass if the job simply ran again?"),
]


class SplitJudge:
    """Asks SPLIT_QUESTIONS in one request and answers the original question with
    P(transient) = max(P(environment), P(intermittent)) as a noul."""

    def __init__(self, inner):
        self.inner, self.name = inner, f"{inner.name}-split"

    def judge(self, req: JudgeRequest) -> JudgeResult:
        res = self.inner.judge(JudgeRequest(state=req.state, questions=SPLIT_QUESTIONS))
        p = max(float(res.answers[q.key].value) for q in SPLIT_QUESTIONS)
        return res.model_copy(update={"answers": {req.questions[0].key: Answer.noul(p)},
                                      "raw": {k: float(a.value) for k, a in res.answers.items()}})


def auroc(scores: np.ndarray, labels: np.ndarray) -> float:
    pos, neg = scores[labels == 1], scores[labels == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    greater = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(greater / (len(pos) * len(neg)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/ci_eval.jsonl"))
    ap.add_argument("--judges", default="jev,llm" if os.environ.get("TYPESAFE_API_KEY") else "llm")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    raw = [json.loads(line) for line in Path(args.data).read_text(encoding="utf-8").splitlines() if line]
    rows = dedupe(raw)
    gold = {r["id"]: int(r["label"] == "flaky_test") for r in rows}  # 1 = transient (rerun fixed it)
    policy = load_policies(ROOT / "policies/ci.yaml")[0]
    question = policy.to_question()
    print(f"{len(raw)} rows -> {len(rows)} after dedupe; transient={sum(gold.values())}", file=sys.stderr)

    # Rule tier first: whatever it decides never reaches the model.
    rule = RuleJudge.from_policies([policy])
    rule_hits = {r["id"]: rule.judge(JudgeRequest(state=r["state"], questions=[question])).answers.get(policy.id)
                 for r in rows}
    rule_hits = {k: v for k, v in rule_hits.items() if v is not None}

    factories = {"jev": JevJudge, "llm": LLMJudge}
    report: dict = {"n_raw": len(raw), "n": len(rows), "n_transient": sum(gold.values()),
                    "rule": {"decided": len(rule_hits),
                             "accuracy": float(np.mean([gold[k] == int(str(v.value) in TRANSIENT) for k, v in rule_hits.items()]))
                             if rule_hits else None}}
    sweeps, cals = {}, {}
    for spec in args.judges.split(","):
        # "kev@http://127.0.0.1:8094/v1/systemone" = a local llama-server decision model on the Jev API
        name, _, url = spec.partition("@")
        base = name.removesuffix("-split")
        judge = JevJudge(url=url) if url else factories.get(base.split("-")[0], JevJudge)()  # cached runs need no live backend
        if name.endswith("-split"):
            judge = SplitJudge(judge)
        preds = cached_preds(f"reruns_{name}", judge, rows, question, args.workers)
        ok = [p for p in preds if p.answer is not None]
        score = np.array([p_transient(p.answer) for p in ok])  # type: ignore[arg-type]
        y = np.array([gold[p.id] for p in ok])
        binary = [Prediction(id=p.id, answer=Answer.noul(s), latency_ms=p.latency_ms, cost_usd=p.cost_usd)
                  for p, s in zip(ok, score)]
        bgold = {k: bool(v) for k, v in gold.items()}
        sweeps[name] = sweep_thresholds(binary, bgold)
        cals[name] = calibration(binary, bgold, n_bins=5)
        confusion: dict[str, Counter] = defaultdict(Counter)
        for p in ok:
            confusion["transient" if gold[p.id] else "persistent"][str(p.answer.value)] += 1  # type: ignore[union-attr]
        # The costly mistake for a rerun bot: a persistent failure confidently called transient.
        risky = [p.id for p, s in zip(ok, score) if not gold[p.id] and s >= 0.85]
        report[name] = {
            "n": len(ok), "failures": len(preds) - len(ok),
            "accuracy@0.5": float(np.mean((score >= 0.5) == y)),
            "auroc": auroc(score, y), "ece": cals[name]["ece"],
            "predicted_class_by_gold": {k: dict(v) for k, v in confusion.items()},
            "persistent_called_transient@0.85": len(risky), "risky_ids": risky[:10],
            "latency_p50_ms": float(np.percentile([p.latency_ms for p in ok], 50)),
            "cost_per_1k_usd": float(np.mean([p.cost_usd for p in ok]) * 1000),
        }

    plot_sweep(sweeps, str(OUT / "reruns_sweep.png"), f"transient vs persistent (real reruns, n={len(rows)})")
    plot_reliability(cals, str(OUT / "reruns_calibration.png"), "P(transient) calibration (real reruns)")
    (OUT / "reruns_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
