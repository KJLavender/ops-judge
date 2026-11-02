"""Build the static case explorer (docs/index.html, GitHub Pages ready) from cached evaluation results.

Every number on the page is recomputed here from data/ci_eval.jsonl and examples/out/preds_reruns_*.jsonl,
and the cascade path for each case is simulated with the real policy thresholds (judge_core.cascade.verdict).

Usage:
  python scripts/build_site.py [--out docs/index.html]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_reruns import TRANSIENT, auroc, dedupe  # noqa: E402
from judge_core.calibration import ece  # noqa: E402
from judge_core.cascade import Decision, verdict  # noqa: E402
from judge_core.judges.rule import RuleJudge  # noqa: E402
from judge_core.policy import load_policies  # noqa: E402
from judge_core.types import Answer, JudgeRequest  # noqa: E402
from ops_judge.ci.actions import plan_action  # noqa: E402

OUT = ROOT / "examples/out"
# (cache name, display name, note) in display order; the cascade uses kev then llm.
MODELS = [
    ("llm", "LLM (Sonnet), all facts", "claude CLI"),
    ("llm-nocontext", "LLM, without the new context facts", "claude CLI"),
    ("llm-split", "LLM, two yes/no questions", "claude CLI"),
    ("kev", "Kev-4B, all facts", "local, llama.cpp (CPU)"),
    ("kev-split", "Kev-4B, two yes/no questions", "local, llama.cpp (CPU)"),
    ("llm-nohist", "LLM (Sonnet), log only", "claude CLI"),
    ("kev-nohist", "Kev-4B, log only", "local GPU, llama.cpp"),
    ("julia-nohist", "Julia-1, log only", "local CPU, llama.cpp"),
    ("laya-nohist", "Laya, log only", "local CPU, llama.cpp"),
]
FACT_KEYS = ["first_error_line", "stack_overlaps_changed_files", "overlapping_files", "changed_files_count",
             "main_pass_rate", "main_runs_considered", "main_failures", "main_failures_fixed_by_rerun",
             "error_seen_on_main_recently", "infra_signals", "env_signals"]


def load_preds(name: str) -> dict[str, dict]:
    path = OUT / f"preds_reruns_{name}.jsonl"
    if not path.exists():
        return {}
    return {p["id"]: p for p in map(json.loads, path.read_text(encoding="utf-8").splitlines())}


def p_transient(a: dict) -> float:
    if a["kind"] == "noul":  # split-question runs answer P(transient) directly
        return float(a["value"])
    return sum(a["probabilities"].get(k, 0.0) for k in TRANSIENT)


def display_probs(a: dict) -> dict[str, float]:
    """Split-question runs answer P(transient) as a yes/no; name the two sides so the page reads plainly."""
    if a["kind"] == "noul":
        return {"rerun_fixes_it": float(a["value"]), "rerun_wont_help": 1 - float(a["value"])}
    return a["probabilities"]


def baselines(rows: list[dict], y: np.ndarray) -> list[dict]:
    def feats(s: dict) -> list[float]:
        inf = s["infra_signals"]
        return [1, "spot_interruption" in inf,
                any(k in inf for k in ["timeout", "dns", "network", "rate_limit", "package_download", "disk", "runner"]),
                "sigkill" in inf, "main_runs_considered" in s, s.get("main_pass_rate", 1.0),
                s.get("main_failures_fixed_by_rerun", 0) > 0, s.get("error_seen_on_main_recently") is True,
                s["stack_overlaps_changed_files"], len(s["env_signals"]) > 0]

    X = np.array([feats(r["state"]) for r in rows], float)
    rule = ((X[:, 1] + X[:, 2] + X[:, 6] + X[:, 7]) > 0).astype(float)

    def fit(X, y, l2=1.0):
        w = np.zeros(X.shape[1])
        reg = np.r_[0, np.ones(X.shape[1] - 1)]
        for _ in range(300):
            p = 1 / (1 + np.exp(-X @ w))
            H = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.diag(reg + 1e-6)
            w -= np.linalg.solve(H, X.T @ (p - y) + l2 * reg * w)
        return w

    idx = np.random.default_rng(0).permutation(len(y))
    score = np.zeros(len(y))
    for f in np.array_split(idx, 5):
        w = fit(X[np.setdiff1d(idx, f)], y[np.setdiff1d(idx, f)])
        score[f] = 1 / (1 + np.exp(-X[f] @ w))
    return [
        {"name": "Always rerun (majority class)", "kind": "baseline", "accuracy": float(y.mean()), "auroc": None},
        {"name": "Rule: infra signal / fixed on main / seen on main", "kind": "baseline",
         "accuracy": float(np.mean(rule == y)), "auroc": None},
        {"name": "Logistic regression on the same facts (5-fold)", "kind": "baseline",
         "accuracy": float(np.mean((score >= 0.5) == y)), "auroc": auroc(score, y)},
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs/index.html"))
    args = ap.parse_args()

    raw = [json.loads(x) for x in (ROOT / "data/ci_eval.jsonl").read_text(encoding="utf-8").splitlines() if x]
    rows = dedupe(raw)
    y = np.array([r["label"] == "flaky_test" for r in rows], float)
    preds = {m: load_preds(m) for m, _, _ in MODELS}
    # Only models that answered (nearly) every case, so all rows of the table compare the same questions.
    preds = {m: p for m, p in preds.items() if sum(r["id"] in p for r in rows) >= 0.9 * len(rows)}
    policy = load_policies(ROOT / "policies/ci.yaml")[0]
    rule = RuleJudge.from_policies([policy])
    q = policy.to_question()

    exp_path = ROOT / "data/experiments.json"
    experiments = json.loads(exp_path.read_text(encoding="utf-8")) if exp_path.exists() else None
    methods = baselines(rows, y)
    for key, label, note in MODELS:
        p = preds.get(key)
        if not p:
            continue
        ok = [r for r in rows if p.get(r["id"], {}).get("answer")]
        s = np.array([p_transient(p[r["id"]]["answer"]) for r in ok])
        t = np.array([r["label"] == "flaky_test" for r in ok], float)
        methods.append({"name": label, "kind": "model", "note": note, "n": len(ok),
                        "accuracy": float(np.mean((s >= 0.5) == t)), "auroc": auroc(s, t), "ece": ece(s, t, 5),
                        "latency_ms": float(np.median([p[r["id"]]["latency_ms"] for r in ok])),
                        "accuracy_calibrated": (experiments or {}).get("models", {}).get(key, {}).get("calibrated", {}).get("accuracy")})

    cases, tier_counts, cascade_correct = [], {"rule": 0, "kev": 0, "llm": 0, "human": 0}, []
    for r in rows:
        gold = r["label"] == "flaky_test"
        path, final, by = [], None, "human"
        hit = rule.judge(JudgeRequest(state=r["state"], questions=[q])).answers.get(policy.id)
        if hit:
            path.append({"tier": "rule", "value": hit.value, "top": 1.0, "accepted": True})
            final, by = hit.value, "rule"
        kev_key = next((k for k in ("kev", "kev-nohist") if k in preds), None)
        for tier, key in (("kev", kev_key), ("llm", "llm")):
            if final is not None:
                break
            if key not in preds:
                continue
            a = preds[key].get(r["id"], {}).get("answer")
            if not a:
                continue
            ans = Answer.model_validate(a)
            accepted, value = verdict(policy, ans)
            path.append({"tier": tier, "value": ans.value, "top": ans.top_prob, "accepted": accepted})
            if accepted:
                final, by = value, tier
        if final is None:
            path.append({"tier": "human", "value": None, "top": None, "accepted": True})
        tier_counts[by] += 1
        if final is not None:
            cascade_correct.append((final in TRANSIENT) == gold)
        d = Decision(policy_id=policy.id, status="decided" if final else "queued", decision=final, decided_by=by,
                     confidence=path[-1]["top"])
        action = plan_action(d, is_fork_pr=r["is_fork_pr"], already_rerun=False)
        repo, run_id, job = r["id"].split("#", 2)
        cases.append({
            "id": r["id"], "repo": repo, "job": job, "run_url": f"https://github.com/{repo}/actions/runs/{run_id}",
            "gold": "transient" if gold else "persistent", "fork": r["is_fork_pr"], "log_lines": r["log_lines"],
            "facts": {k: r["state"][k] for k in FACT_KEYS if k in r["state"]},
            "excerpt": r["state"]["error_excerpt"],
            "models": {label: {"value": p[r["id"]]["answer"]["value"], "probs": display_probs(p[r["id"]]["answer"])}
                       for key, label, _ in MODELS if (p := preds.get(key, {})).get(r["id"], {}).get("answer")},
            "path": path, "final": final, "decided_by": by, "action": vars(action),
            "correct": None if final is None else (final in TRANSIENT) == gold,
        })

    summary = {
        "experiments": experiments,
        "n_raw": len(raw), "n": len(rows), "n_transient": int(y.sum()), "repos": sorted({c["repo"] for c in cases}),
        "methods": methods, "cascade": {"tiers": tier_counts,
                                        "accuracy_on_decided": float(np.mean(cascade_correct)) if cascade_correct else None,
                                        "decided": len(cascade_correct)},
    }
    template = (ROOT / "site/template.html").read_text(encoding="utf-8")
    data = json.dumps({"summary": summary, "cases": cases}, ensure_ascii=False).replace("</", "<\\/")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template.replace("/*__DATA__*/null", data), encoding="utf-8")
    print(f"{len(cases)} cases -> {out} ({out.stat().st_size / 1024:.0f} KB); cascade tiers {tier_counts}")


if __name__ == "__main__":
    main()
