"""Deterministic rules: a regex hit decides the answer outright (confidence 1.0); no hit means no answer."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from ..policy import Policy, Rule
from ..types import Answer, JudgeRequest, JudgeResult, Question

# A python rule returns an answer value, or None when it does not apply.
PyRule = Callable[[JudgeRequest], Any]


def make_answer(q: Question, value: Any) -> Answer:
    if q.kind == "noul":
        return Answer.noul(1.0 if value in (True, "true", 1) else 0.0)
    value = str(value)
    if value not in q.options:
        raise ValueError(f"rule answer {value!r} is not an option of {q.key!r}")
    return Answer.from_probs(q.kind, {o: float(o == value) for o in q.options})


class RuleJudge:
    name = "rule"

    def __init__(self, rules: dict[str, list[Rule]] | None = None, py_rules: dict[str, list[PyRule]] | None = None):
        self.rules = {k: [(r, re.compile(r.regex, re.IGNORECASE | re.MULTILINE)) for r in v]
                      for k, v in (rules or {}).items()}
        self.py_rules = py_rules or {}

    @classmethod
    def from_policies(cls, policies: list[Policy], py_rules: dict[str, list[PyRule]] | None = None) -> RuleJudge:
        return cls({p.id: p.rules for p in policies if p.rules}, py_rules)

    def judge(self, req: JudgeRequest) -> JudgeResult:
        t0 = time.perf_counter()
        answers, hits = {}, {}
        for q in req.questions:
            value, why = self._match(req, q.key)
            if why is not None:
                answers[q.key] = make_answer(q, value)
                hits[q.key] = why
        return JudgeResult(judge=self.name, answers=answers,
                           latency_ms=(time.perf_counter() - t0) * 1000, raw={"hits": hits})

    def _match(self, req: JudgeRequest, key: str) -> tuple[Any, str | None]:
        for rule, rx in self.rules.get(key, []):
            text = req.state_text()
            if rule.field is not None:
                field = req.state.get(rule.field, "") if isinstance(req.state, dict) else ""
                text = field if isinstance(field, str) else json.dumps(field, ensure_ascii=False)
            if rx.search(text):
                return rule.answer, rule.regex
        for fn in self.py_rules.get(key, []):
            value = fn(req)
            if value is not None:
                return value, getattr(fn, "__name__", "python_rule")
        return None, None
