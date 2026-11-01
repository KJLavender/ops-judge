import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judge_core.types import Answer, JudgeError, JudgeRequest, JudgeResult  # noqa: E402


class FakeJudge:
    """Returns fixed answers per question key; `fail=True` raises JudgeError. Records requests."""

    def __init__(self, name: str, answers: dict[str, Answer] | None = None, fail: bool = False):
        self.name, self.answers, self.fail = name, answers or {}, fail
        self.requests: list[JudgeRequest] = []

    def judge(self, req: JudgeRequest) -> JudgeResult:
        self.requests.append(req)
        if self.fail:
            raise JudgeError(f"{self.name} down")
        return JudgeResult(judge=self.name, latency_ms=1.0, cost_usd=0.001,
                           answers={q.key: self.answers[q.key] for q in req.questions if q.key in self.answers})
