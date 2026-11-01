import numpy as np
import pytest

from judge_core import calibration as cal
from judge_core.evaluate import Prediction, calibration, compare, fit_calibrator, rule_of_three, sweep_thresholds
from judge_core.types import Answer


def test_isotonic_is_monotone_and_fits():
    rng = np.random.default_rng(0)
    p = rng.uniform(size=500)
    t = (rng.uniform(size=500) < p ** 2).astype(float)  # overconfident source
    c = cal.IsotonicCalibrator().fit(p, t)
    grid = np.linspace(0, 1, 50)
    out = [c(x) for x in grid]
    assert all(b >= a - 1e-12 for a, b in zip(out, out[1:]))
    assert cal.ece([c(x) for x in p], t) < cal.ece(p, t)


def test_platt_recovers_underconfidence():
    rng = np.random.default_rng(1)
    z = rng.normal(size=2000) * 3
    t = (rng.uniform(size=2000) < 1 / (1 + np.exp(-z))).astype(float)
    raw = 1 / (1 + np.exp(-z / 2))  # under-confident by a factor of 2
    c = cal.PlattCalibrator().fit(raw, t)
    assert c.a == pytest.approx(2.0, rel=0.2)


def test_apply_choice_keeps_distribution():
    a = Answer.from_probs("choice", {"a": 0.6, "b": 0.3, "c": 0.1})
    b = cal.apply(cal.PlattCalibrator(a=2, b=0), a)
    assert b.value == "a" and b.probabilities["a"] > 0.6
    assert sum(b.probabilities.values()) == pytest.approx(1)
    assert b.probabilities["b"] / b.probabilities["c"] == pytest.approx(3)


def test_calibrators_roundtrip(tmp_path):
    cals = {("alert_page", "jev"): cal.PlattCalibrator(1.5, 0.2), ("x", "llm"): cal.IsotonicCalibrator([0, 1], [0.1, 0.9])}
    cal.save_calibrators(cals, tmp_path / "c.json")
    back = cal.load_calibrators(tmp_path / "c.json")
    assert back[("alert_page", "jev")](0.5) == pytest.approx(cals[("alert_page", "jev")](0.5))
    assert back[("x", "llm")](0.5) == pytest.approx(0.5)


def preds(values):
    return [Prediction(id=str(i), answer=Answer.noul(p), latency_ms=10 * i, cost_usd=0.0001) for i, p in enumerate(values)]


def test_sweep_coverage_drops_as_threshold_rises():
    golds = {str(i): g for i, g in enumerate([True, True, False, False, True])}
    rows = sweep_thresholds(preds([0.99, 0.7, 0.4, 0.05, 0.55]), golds)
    assert rows[0]["coverage"] == 1.0 and rows[-1]["coverage"] < 1.0
    assert rows[0]["error_rate"] == pytest.approx(0.0)


def test_compare_reports_error_correlation():
    golds = {str(i): True for i in range(6)}
    a = preds([0.9, 0.9, 0.1, 0.1, 0.9, 0.9])   # wrong on 2, 3
    b = preds([0.9, 0.9, 0.1, 0.9, 0.9, 0.9])   # wrong on 2
    r = compare({"jev": a, "llm": b}, golds)
    assert r["backends"]["jev"]["accuracy"] == pytest.approx(4 / 6)
    pair = r["pairs"]["jev~llm"]
    assert pair["both_wrong"] == pytest.approx(1 / 6)
    assert pair["b_fixes_a"] == pytest.approx(0.5)
    assert r["backends"]["llm"]["error_upper95"] is None


def test_rule_of_three_and_calibration_summary():
    assert rule_of_three(30) == pytest.approx(0.1)
    golds = {str(i): g for i, g in enumerate([True, False, True, False])}
    c = calibration(preds([0.9, 0.2, 0.8, 0.1]), golds)
    assert c["n"] == 4 and 0 <= c["ece"] <= 1
    assert fit_calibrator(preds([0.9, 0.2, 0.8, 0.1]), golds)(0.85) > 0.5
