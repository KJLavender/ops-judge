"""Post-hoc calibration (isotonic via PAV, Platt) and calibration metrics. numpy only.

noul: calibrate P(true) against the 0/1 label.
choice/score: calibrate the top-option probability against "top option was correct",
then rescale the remaining options proportionally.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .types import Answer


class IsotonicCalibrator:
    kind = "isotonic"

    def __init__(self, x: list[float] | None = None, y: list[float] | None = None):
        self.x = np.asarray(x or [0.0, 1.0], dtype=float)
        self.y = np.asarray(y or [0.0, 1.0], dtype=float)

    def fit(self, probs, labels) -> IsotonicCalibrator:
        p, t = np.asarray(probs, float), np.asarray(labels, float)
        order = np.argsort(p, kind="stable")
        p, t = p[order], t[order]
        # Pool-adjacent-violators: blocks of (sum, count, x_min, x_max)
        blocks: list[list[float]] = []
        for pi, ti in zip(p, t):
            blocks.append([ti, 1.0, pi, pi])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                s, c, lo, _ = blocks.pop(-2)
                blocks[-1] = [blocks[-1][0] + s, blocks[-1][1] + c, lo, blocks[-1][3]]
        xs, ys = [], []
        for s, c, lo, hi in blocks:
            xs += [lo, hi]
            ys += [s / c, s / c]
        self.x, self.y = np.asarray(xs), np.asarray(ys)
        return self

    def __call__(self, p: float) -> float:
        return float(np.interp(p, self.x, self.y))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "x": self.x.tolist(), "y": self.y.tolist()}


class PlattCalibrator:
    kind = "platt"

    def __init__(self, a: float = 1.0, b: float = 0.0):
        self.a, self.b = a, b

    def fit(self, probs, labels, iters: int = 100) -> PlattCalibrator:
        z = _logit(np.asarray(probs, float))
        t = np.asarray(labels, float)
        a, b = 1.0, 0.0
        for _ in range(iters):  # Newton's method on log-loss
            q = _sigmoid(a * z + b)
            w = q * (1 - q) + 1e-9
            g = np.array([np.sum((q - t) * z), np.sum(q - t)])
            h = np.array([[np.sum(w * z * z), np.sum(w * z)], [np.sum(w * z), np.sum(w)]]) + 1e-6 * np.eye(2)
            step = np.linalg.solve(h, g)
            a, b = a - step[0], b - step[1]
            if np.abs(step).max() < 1e-8:
                break
        self.a, self.b = float(a), float(b)
        return self

    def __call__(self, p: float) -> float:
        return float(_sigmoid(self.a * _logit(np.asarray(p, float)) + self.b))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "a": self.a, "b": self.b}


Calibrator = IsotonicCalibrator | PlattCalibrator


def calibrator_from_dict(d: dict) -> Calibrator:
    if d["kind"] == "isotonic":
        return IsotonicCalibrator(d["x"], d["y"])
    return PlattCalibrator(d["a"], d["b"])


def save_calibrators(cals: dict[tuple[str, str], Calibrator], path: str | Path) -> None:
    data = {f"{pid}|{judge}": c.to_dict() for (pid, judge), c in cals.items()}
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_calibrators(path: str | Path) -> dict[tuple[str, str], Calibrator]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {tuple(k.split("|", 1)): calibrator_from_dict(v) for k, v in data.items()}  # type: ignore[misc]


def apply(cal: Calibrator, a: Answer) -> Answer:
    if a.kind == "noul":
        return Answer.noul(cal(float(a.value)))
    assert a.probabilities is not None
    top = max(a.probabilities, key=a.probabilities.get)  # type: ignore[arg-type]
    p_top, new_top = a.probabilities[top], cal(a.probabilities[top])
    rest = 1 - p_top
    probs = {k: (new_top if k == top else (v / rest * (1 - new_top) if rest > 0 else (1 - new_top) / (len(a.probabilities) - 1)))
             for k, v in a.probabilities.items()}
    return Answer(kind=a.kind, value=a.value, probabilities=probs, confidence=new_top)


def ece(probs, labels, n_bins: int = 10) -> float:
    """Expected calibration error for binary outcomes (p = predicted P(label==1))."""
    p, t = np.asarray(probs, float), np.asarray(labels, float)
    bins = np.minimum((p * n_bins).astype(int), n_bins - 1)
    total = 0.0
    for b in range(n_bins):
        m = bins == b
        if m.any():
            total += m.mean() * abs(p[m].mean() - t[m].mean())
    return float(total)


def reliability(probs, labels, n_bins: int = 10) -> list[tuple[float, float, int]]:
    """(mean predicted, observed frequency, count) per non-empty bin."""
    p, t = np.asarray(probs, float), np.asarray(labels, float)
    bins = np.minimum((p * n_bins).astype(int), n_bins - 1)
    return [(float(p[bins == b].mean()), float(t[bins == b].mean()), int((bins == b).sum()))
            for b in range(n_bins) if (bins == b).any()]


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _sigmoid(z):
    return 1 / (1 + np.exp(-z))
