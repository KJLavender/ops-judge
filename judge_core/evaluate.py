"""Evaluation: threshold sweep, calibration, backend comparison (incl. error correlation), learning curve."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
from pydantic import BaseModel

from . import calibration as cal
from .types import Answer, Judge, JudgeError, JudgeRequest, Question


class Prediction(BaseModel):
    id: str
    answer: Answer | None  # None = backend failed
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None
    input_hash: str | None = None  # hash of the state judged; a cache entry is only valid for the same input


def predicted_value(a: Answer) -> Any:
    return float(a.value) >= 0.5 if a.kind == "noul" else a.value


def is_correct(a: Answer | None, gold: Any) -> bool:
    if a is None:
        return False
    v = predicted_value(a)
    return v == gold if a.kind != "score" else int(v) == int(gold)


def run_judge(judge: Judge, dataset: list[dict], question: Question) -> list[Prediction]:
    """dataset items: {"id", "state", "label"}"""
    preds = []
    for item in dataset:
        req = JudgeRequest(state=item["state"], questions=[question])
        try:
            r = judge.judge(req)
            preds.append(Prediction(id=item["id"], answer=r.answers.get(question.key), latency_ms=r.latency_ms,
                                    cost_usd=r.cost_usd, input_hash=req.input_hash()))
        except JudgeError as e:
            preds.append(Prediction(id=item["id"], answer=None, error=str(e)[:300], input_hash=req.input_hash()))
    return preds


def _scored(preds: list[Prediction], golds: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    ok = [p for p in preds if p.answer is not None]
    conf = np.array([p.answer.top_prob for p in ok])  # type: ignore[union-attr]
    correct = np.array([is_correct(p.answer, golds[p.id]) for p in ok], dtype=float)
    return conf, correct


def sweep_thresholds(preds: list[Prediction], golds: dict[str, Any], thresholds=None) -> list[dict]:
    """For each confidence threshold: share the judge decides alone (coverage) vs error rate on that share."""
    conf, correct = _scored(preds, golds)
    thresholds = np.linspace(0.5, 1.0, 51) if thresholds is None else thresholds
    rows = []
    for t in thresholds:
        m = conf >= t
        rows.append({"threshold": float(t), "coverage": float(m.mean()) if len(m) else 0.0,
                     "error_rate": float(1 - correct[m].mean()) if m.any() else 0.0, "n": int(m.sum())})
    return rows


def _binary(preds: list[Prediction], golds: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """noul: (P(true), gold); choice/score: (top prob, top was correct)."""
    ok = [p for p in preds if p.answer is not None]
    if ok and ok[0].answer.kind == "noul":  # type: ignore[union-attr]
        return (np.array([float(p.answer.value) for p in ok]),  # type: ignore[union-attr]
                np.array([float(bool(golds[p.id])) for p in ok]))
    return _scored(preds, golds)


def calibration(preds: list[Prediction], golds: dict[str, Any], n_bins: int = 10) -> dict:
    p, t = _binary(preds, golds)
    return {"ece": cal.ece(p, t, n_bins), "bins": cal.reliability(p, t, n_bins), "n": len(p)}


def fit_calibrator(preds: list[Prediction], golds: dict[str, Any], method: str = "isotonic") -> cal.Calibrator:
    p, t = _binary(preds, golds)
    c = cal.IsotonicCalibrator() if method == "isotonic" else cal.PlattCalibrator()
    return c.fit(p, t)


def rule_of_three(n: int) -> float:
    """95% upper bound on the error rate when 0 errors were observed in n trials."""
    return 3.0 / n if n else 1.0


def compare(results: dict[str, list[Prediction]], golds: dict[str, Any]) -> dict:
    """Accuracy, latency p50/p95, cost per 1k for each backend, plus pairwise error correlation
    and the share of items both backends got wrong (if high, cascading one into the other buys little)."""
    table, errors = {}, {}
    for name, preds in results.items():
        lat = np.array([p.latency_ms for p in preds if p.answer is not None]) if preds else np.array([0.0])
        wrong = np.array([not is_correct(p.answer, golds[p.id]) for p in preds], dtype=float)
        errors[name] = {p.id: w for p, w in zip(preds, wrong)}
        n_wrong = int(wrong.sum())
        table[name] = {
            "n": len(preds), "accuracy": float(1 - wrong.mean()) if len(wrong) else 0.0,
            "errors": n_wrong, "error_upper95": rule_of_three(len(preds)) if n_wrong == 0 else None,
            "failures": sum(p.answer is None for p in preds),
            "latency_p50_ms": float(np.percentile(lat, 50)) if len(lat) else 0.0,
            "latency_p95_ms": float(np.percentile(lat, 95)) if len(lat) else 0.0,
            "cost_per_1k_usd": float(np.mean([p.cost_usd for p in preds]) * 1000) if preds else 0.0,
        }
    pairs = {}
    names = list(results)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ids = sorted(set(errors[a]) & set(errors[b]))
            ea, eb = np.array([errors[a][k] for k in ids]), np.array([errors[b][k] for k in ids])
            corr = float(np.corrcoef(ea, eb)[0, 1]) if ea.std() > 0 and eb.std() > 0 else None
            pairs[f"{a}~{b}"] = {
                "error_corr": corr,
                "both_wrong": float((ea * eb).mean()) if ids else 0.0,
                # of the items `a` got wrong, how many does `b` fix? (value of b as a fallback for a)
                "b_fixes_a": float(1 - eb[ea == 1].mean()) if (ea == 1).any() else None,
            }
    return {"backends": table, "pairs": pairs}


def learning_curve(fit: Callable[[list[dict]], Judge], question: Question, train: list[dict], test: list[dict],
                   sizes=(50, 200, 1000)) -> list[dict]:
    """Train a classifier on growing label budgets; compare its test accuracy with a zero-shot judge."""
    golds = {x["id"]: x["label"] for x in test}
    rows = []
    for n in sizes:
        if n > len(train):
            break
        preds = run_judge(fit(train[:n]), test, question)
        rows.append({"n_train": n, "accuracy": compare({"clf": preds}, golds)["backends"]["clf"]["accuracy"]})
    return rows


def plot_sweep(curves: dict[str, list[dict]], path: str, title: str = "Threshold sweep") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 4.5))
    for name, rows in curves.items():
        ax.plot([r["coverage"] for r in rows], [r["error_rate"] for r in rows], marker=".", label=name)
    ax.set_xlabel("Coverage (share decided without escalation)")
    ax.set_ylabel("Error rate on decided share")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_reliability(curves: dict[str, dict], path: str, title: str = "Calibration") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], ls="--", color="gray", lw=1)
    for name, c in curves.items():
        xs, ys, _ = zip(*c["bins"]) if c["bins"] else ((), (), ())
        ax.plot(xs, ys, marker="o", label=f"{name} (ECE {c['ece']:.3f})")
    ax.set_xlabel("Predicted probability")
    ax.set_ylabel("Observed frequency")
    ax.set_title(title)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
