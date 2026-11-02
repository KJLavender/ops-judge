"""All follow-up experiments on the rerun dataset, from cached predictions (no model calls here).

  1. per-model metrics, on all cases and on cases without label noise
  2. calibration: cross-validated isotonic fit -> ECE before/after; "answer only when sure" curve
  3. cost-based threshold: rerun vs send-to-human, minimising expected engineer minutes
  5. agreement: decision model + LLM must agree, otherwise human
  per-repo breakdown

Writes data/experiments.json (read by build_site.py) and prints a readable summary.

Usage:
  python scripts/experiments.py [--models llm,kev,...] [--rerun-cost 1 --human-cost 15 --delay-cost 5]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_reruns import TRANSIENT, auroc, dedupe  # noqa: E402
from judge_core.calibration import IsotonicCalibrator, ece  # noqa: E402

OUT = ROOT / "examples/out"
FOLDS = 5


def p_transient(a: dict) -> float:
    if a["kind"] == "noul":  # split-question runs store P(transient) directly
        return float(a["value"])
    return sum(a["probabilities"].get(k, 0.0) for k in TRANSIENT)


def load(name: str) -> dict[str, float]:
    path = OUT / f"preds_reruns_{name}.jsonl"
    if not path.exists():
        return {}
    preds = map(json.loads, path.read_text(encoding="utf-8").splitlines())
    return {p["id"]: p_transient(p["answer"]) for p in preds if p.get("answer")}


def cv_calibrate(s: np.ndarray, y: np.ndarray, seed: int = 0) -> np.ndarray:
    """Out-of-fold isotonic calibration: each case is calibrated by a fit that never saw its label."""
    idx = np.random.default_rng(seed).permutation(len(y))
    out = np.zeros(len(y))
    for fold in np.array_split(idx, FOLDS):
        train = np.setdiff1d(idx, fold)
        cal = IsotonicCalibrator().fit(s[train], y[train])
        out[fold] = [cal(x) for x in s[fold]]
    return out


def metrics(s: np.ndarray, y: np.ndarray) -> dict:
    return {"n": int(len(y)), "accuracy": float(np.mean((s >= 0.5) == y)), "auroc": auroc(s, y), "ece": ece(s, y, 5)}


def selective(s: np.ndarray, y: np.ndarray) -> list[dict]:
    """Answer only when max(p, 1-p) >= t: coverage vs accuracy on the answered part."""
    conf = np.maximum(s, 1 - s)
    rows = []
    for t in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95):
        m = conf >= t
        rows.append({"threshold": t, "answered": int(m.sum()), "coverage": float(m.mean()),
                     "accuracy": float(np.mean((s[m] >= 0.5) == y[m])) if m.any() else None})
    return rows


def cost_policy(s: np.ndarray, y: np.ndarray, c_rerun: float, c_human: float, c_delay: float) -> dict:
    """Two actions per failure: auto-rerun or send to a human.
    rerun:  c_rerun, and if the rerun does not help, a human still has to look (+ c_human + c_delay)
    human:  c_human (wasted when a rerun would have fixed it)
    Expected cost says: rerun iff P(transient) > (c_rerun + c_delay) / (c_human + c_delay)."""
    threshold = (c_rerun + c_delay) / (c_human + c_delay)

    def total(rerun: np.ndarray) -> float:
        return float(np.sum(np.where(rerun, c_rerun + (1 - y) * (c_human + c_delay), c_human)))

    return {
        "threshold": threshold,
        "always_human": total(np.zeros_like(y, bool)),
        "always_rerun": total(np.ones_like(y, bool)),
        "model": total(s > threshold),
        "oracle": total(y.astype(bool)),
        "reruns": int((s > threshold).sum()),
        "persistent_rerun": int(((s > threshold) & (y == 0)).sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/ci_eval.jsonl"))
    ap.add_argument("--models", default="llm,llm-nocontext,llm-split,kev,kev-split")
    ap.add_argument("--rerun-cost", type=float, default=1.0, help="engineer-minutes equivalent of an automatic rerun")
    ap.add_argument("--human-cost", type=float, default=15.0, help="minutes for a human to investigate a failure")
    ap.add_argument("--delay-cost", type=float, default=5.0, help="extra minutes lost when a useless rerun delays the human")
    args = ap.parse_args()

    rows = dedupe([json.loads(x) for x in Path(args.data).read_text(encoding="utf-8").splitlines() if x])
    gold = {r["id"]: int(r["label"] == "flaky_test") for r in rows}
    clean = {r["id"] for r in rows if not r.get("label_noise")}
    repo = {r["id"]: r["repo"] for r in rows}
    report: dict = {"n": len(rows), "n_transient": sum(gold.values()), "n_clean": len(clean),
                    "repos": sorted(set(repo.values())), "models": {}}
    report["costs"] = {"rerun": args.rerun_cost, "human": args.human_cost, "delay": args.delay_cost}
    report["majority"] = {"all": max(np.mean(list(gold.values())), 1 - np.mean(list(gold.values()))),
                          "clean": max(np.mean([gold[i] for i in clean]), 1 - np.mean([gold[i] for i in clean]))}

    scores: dict[str, dict[str, float]] = {}
    for name in args.models.split(","):
        p = load(name)
        ids = [r["id"] for r in rows if r["id"] in p]
        if len(ids) < len(rows) * 0.9:
            continue  # missing or partial run: skip rather than report on a different subset
        s, y = np.array([p[i] for i in ids]), np.array([gold[i] for i in ids], float)
        cal = cv_calibrate(s, y)
        scores[name] = dict(zip(ids, cal))
        m_clean = [k for k, i in enumerate(ids) if i in clean]
        by_repo = defaultdict(list)
        for k, i in enumerate(ids):
            by_repo[repo[i]].append(k)
        report["models"][name] = {
            "raw": metrics(s, y), "calibrated": metrics(cal, y),
            "clean": metrics(cal[m_clean], y[m_clean]),
            "selective": selective(cal, y),
            "cost": cost_policy(cal, y, args.rerun_cost, args.human_cost, args.delay_cost),
            "cost_sensitivity": {str(h): cost_policy(cal, y, args.rerun_cost, h, args.delay_cost) for h in (5, 15, 30, 60)},
            "by_repo": {r: {"n": len(ks), "majority": float(max(y[ks].mean(), 1 - y[ks].mean())),
                            "accuracy": float(np.mean((cal[ks] >= 0.5) == y[ks]))} for r, ks in sorted(by_repo.items())},
        }

    # 5. agreement: a decision model and the LLM both confident and on the same side -> decide; else human
    pairs = {}
    for dm in ("kev-split", "kev"):
        if dm in scores and "llm" in scores:
            ids = [i for i in scores["llm"] if i in scores[dm]]
            a, b = np.array([scores[dm][i] for i in ids]), np.array([scores["llm"][i] for i in ids])
            y = np.array([gold[i] for i in ids], float)
            for t in (0.5, 0.6, 0.7):
                agree = ((a >= t) & (b >= t)) | ((a <= 1 - t) & (b <= 1 - t))
                pairs[f"{dm}+llm@{t}"] = {"decided": int(agree.sum()), "coverage": float(agree.mean()),
                                          "accuracy": float(np.mean((b[agree] >= 0.5) == y[agree])) if agree.any() else None}
    report["agreement"] = pairs
    (ROOT / "data/experiments.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    print(f"cases {report['n']} (transient {report['n_transient']}), without label noise {report['n_clean']}; "
          f"majority {report['majority']['all']:.3f} / clean {report['majority']['clean']:.3f}")
    for name, m in report["models"].items():
        r, c, cl, cost = m["raw"], m["calibrated"], m["clean"], m["cost"]
        print(f"{name:13} acc {r['accuracy']:.3f}->{c['accuracy']:.3f}  auroc {r['auroc']:.3f}  "
              f"ece {r['ece']:.3f}->{c['ece']:.3f}  clean acc {cl['accuracy']:.3f}  "
              f"cost {cost['model']:.0f} (human {cost['always_human']:.0f}, rerun {cost['always_rerun']:.0f}, oracle {cost['oracle']:.0f})")
    for k, v in pairs.items():
        print(f"agreement {k}: decides {v['decided']} ({v['coverage']:.0%}), accuracy {v['accuracy']}")


if __name__ == "__main__":
    main()
