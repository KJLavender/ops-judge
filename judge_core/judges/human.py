"""Human review queue as the last tier. Already-labeled inputs are answered with confidence 1.0;
everything else is enqueued and left unanswered."""

from __future__ import annotations

import time

from ..store import Store
from ..types import JudgeRequest, JudgeResult
from .rule import make_answer


class HumanQueue:
    name = "human"

    def __init__(self, store: Store):
        self.store = store

    def judge(self, req: JudgeRequest, context: dict | None = None) -> JudgeResult:
        t0 = time.perf_counter()
        h = req.input_hash()
        answers, queued = {}, {}
        for q in req.questions:
            found, label = self.store.lookup_label(q.key, h)
            if found:
                answers[q.key] = make_answer(q, label)
            else:
                queued[q.key] = self.store.enqueue(q.key, h, req.state, q.model_dump(), context or {})
        return JudgeResult(judge=self.name, answers=answers,
                           latency_ms=(time.perf_counter() - t0) * 1000, raw={"queued": queued})
