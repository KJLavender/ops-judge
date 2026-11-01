"""Shared data structures. One request = one state + several named questions (mirrors Jev's API)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol, Union

from pydantic import BaseModel, Field, model_validator

Kind = Literal["noul", "choice", "score"]
State = Union[str, dict, list]


class Question(BaseModel):
    key: str
    kind: Kind
    instructions: str
    # noul: optional {"true": ..., "false": ...}; choice: {option: description|None}; score: ordered levels
    criteria: dict[str, str | None] | list[str] | None = None

    @model_validator(mode="after")
    def _check_criteria(self) -> Question:
        if self.kind == "choice" and not isinstance(self.criteria, dict):
            raise ValueError(f"choice question {self.key!r} needs criteria as {{option: description}}")
        if self.kind == "score" and not (isinstance(self.criteria, list) and 2 <= len(self.criteria) <= 10):
            raise ValueError(f"score question {self.key!r} needs 2-10 ordered levels")
        return self

    @property
    def options(self) -> list[str]:
        if self.kind == "choice":
            return list(self.criteria)  # type: ignore[arg-type]
        if self.kind == "score":
            return [str(i) for i in range(len(self.criteria))]  # type: ignore[arg-type]
        return ["true", "false"]


class JudgeRequest(BaseModel):
    state: State
    questions: list[Question]

    def state_text(self) -> str:
        if isinstance(self.state, str):
            return self.state
        return json.dumps(self.state, ensure_ascii=False, sort_keys=True)

    def input_hash(self) -> str:
        return hashlib.sha256(self.state_text().encode("utf-8")).hexdigest()[:16]


class Answer(BaseModel):
    kind: Kind
    # noul: P(true); choice: chosen option; score: level index
    value: float | str | int
    probabilities: dict[str, float] | None = None
    # noul: max(p, 1-p); choice/score: probability of the chosen value unless the backend reports otherwise
    confidence: float

    @classmethod
    def noul(cls, p: float) -> Answer:
        p = min(max(float(p), 0.0), 1.0)
        return cls(kind="noul", value=p, probabilities={"true": p, "false": 1 - p}, confidence=max(p, 1 - p))

    @classmethod
    def from_probs(cls, kind: Kind, probs: dict[str, float], confidence: float | None = None) -> Answer:
        total = sum(probs.values()) or 1.0
        probs = {k: v / total for k, v in probs.items()}
        top = max(probs, key=probs.get)  # type: ignore[arg-type]
        value: str | int = int(top) if kind == "score" else top
        return cls(kind=kind, value=value, probabilities=probs,
                   confidence=probs[top] if confidence is None else confidence)

    @property
    def top_prob(self) -> float:
        if self.kind == "noul":
            return self.confidence
        assert self.probabilities is not None
        return max(self.probabilities.values())


class JudgeResult(BaseModel):
    judge: str
    answers: dict[str, Answer] = Field(default_factory=dict)  # a backend may answer only some questions
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


class JudgeError(Exception):
    """Backend failed (network, quota, unparsable output). The cascade treats it as 'no answer'."""


class Judge(Protocol):
    name: str

    def judge(self, req: JudgeRequest) -> JudgeResult: ...
