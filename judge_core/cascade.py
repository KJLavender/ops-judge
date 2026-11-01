"""Cascade: rule -> jev -> llm -> human. Each tier gets one batched request with only the still-undecided
questions; answers are calibrated per (policy, judge) before thresholds are applied."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from .calibration import Calibrator, apply
from .judges.human import HumanQueue
from .policy import Policy
from .store import Store
from .types import Answer, Judge, JudgeError, JudgeRequest, State


class Decision(BaseModel):
    policy_id: str
    status: str            # decided | queued | fallback | unscored | unrouted | unprocessed
    decision: Any = None   # final value (bool for noul, option for choice, level index for score)
    decided_by: str | None = None
    confidence: float | None = None
    path: list[str] = Field(default_factory=list)
    tiers: list[dict] = Field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float = 0.0


def verdict(p: Policy, a: Answer) -> tuple[bool, Any]:
    """Is this (calibrated) answer confident enough to stop here? Returns (accept, decision value)."""
    th = p.thresholds
    if p.kind == "noul":
        pt = float(a.value)
        if th.yes_if_above is not None and pt >= th.yes_if_above:
            return True, True
        if th.no_if_below is not None and pt <= th.no_if_below:
            return True, False
        if th.yes_if_above is None and th.no_if_below is None and p.role in ("ranking", "extraction"):
            return True, pt >= 0.5
        return False, None
    if p.role == "ranking":  # ranking never gates on a threshold
        return True, a.value
    if str(a.value) in p.escalate_options:
        return False, None
    need = th.option_thresholds.get(str(a.value), th.accept_if_above)
    if need is None or a.top_prob >= need:
        return True, a.value
    return False, None


def exhausted(p: Policy, status_hint: str) -> tuple[str, Any]:
    """Every tier failed or stayed in the gray zone."""
    conservative = p.fail_decision if (p.role == "gating" or p.fail_mode == "closed") else None
    if status_hint == "queued":
        return "queued", conservative  # gating stays conservative while waiting for a human
    if conservative is not None:
        return "fallback", conservative
    return {"ranking": "unscored", "routing": "unrouted"}.get(p.role, "unprocessed"), None


class Cascade:
    def __init__(self, policies: list[Policy], judges: dict[str, Judge], store: Store | None = None,
                 calibrators: dict[tuple[str, str], Calibrator] | None = None):
        self.policies = {p.id: p for p in policies}
        self.judges = judges
        self.store = store or Store()
        self.calibrators = calibrators or {}
        for p in policies:
            missing = [t for t in p.tiers if t not in judges]
            if missing:
                raise ValueError(f"policy {p.id!r} needs judges {missing}")

    def run(self, state: State, policy_ids: list[str] | None = None, context: dict | None = None) -> dict[str, Decision]:
        pending = [self.policies[i] for i in (policy_ids or list(self.policies))]
        out = {p.id: Decision(policy_id=p.id, status="pending") for p in pending}
        order = _tier_order(pending)
        input_hash = JudgeRequest(state=state, questions=[]).input_hash()

        for tier in order:
            todo = [p for p in pending if tier in p.tiers]
            if not todo:
                continue
            req = JudgeRequest(state=state, questions=[p.to_question() for p in todo])
            judge = self.judges[tier]
            t0 = time.perf_counter()
            try:
                res = judge.judge(req, context) if isinstance(judge, HumanQueue) else judge.judge(req)
            except JudgeError as e:
                for p in todo:
                    d = out[p.id]
                    d.path.append(tier)
                    d.tiers.append({"judge": tier, "error": str(e)})
                    d.latency_ms += (time.perf_counter() - t0) * 1000
                continue
            for p in todo:
                d = out[p.id]
                d.path.append(tier)
                d.latency_ms += res.latency_ms
                d.cost_usd += res.cost_usd / len(todo)
                raw_ans = res.answers.get(p.id)
                if raw_ans is None:
                    entry = {"judge": tier, "answer": None}
                    if tier == "human":
                        entry["queued_id"] = res.raw.get("queued", {}).get(p.id)
                        d.status = "queued"
                    d.tiers.append(entry)
                    continue
                cal = self.calibrators.get((p.id, tier))
                ans = apply(cal, raw_ans) if cal else raw_ans
                ok, value = verdict(p, ans) if tier not in ("rule", "human") else (True, _decision_value(p, ans))
                d.tiers.append({"judge": tier, "raw": raw_ans.model_dump(), "calibrated": cal is not None,
                                "answer": ans.model_dump(), "accepted": ok})
                if ok:
                    d.status, d.decision, d.decided_by, d.confidence = "decided", value, tier, ans.top_prob
            pending = [p for p in pending if out[p.id].status != "decided"]
            if not pending:
                break

        for p in pending:
            d = out[p.id]
            d.status, d.decision = exhausted(p, d.status)
        for d in out.values():
            self.store.log_decision(d.policy_id, input_hash, d.decision, d.decided_by or d.status,
                                    d.path, d.tiers, d.latency_ms, d.cost_usd)
        return out


def _decision_value(p: Policy, a: Answer) -> Any:
    return float(a.value) >= 0.5 if p.kind == "noul" else a.value


def _tier_order(policies: list[Policy]) -> list[str]:
    order: list[str] = []
    for p in policies:
        for t in p.tiers:
            if t not in order:
                order.append(t)
    # human is always last
    return [t for t in order if t != "human"] + (["human"] if "human" in order else [])
