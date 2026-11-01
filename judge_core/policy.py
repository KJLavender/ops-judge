"""Policy = one judgment, defined in YAML. A YAML file may hold one policy or a list of them."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .types import Kind, Question

Role = Literal["routing", "gating", "ranking", "extraction"]


class Thresholds(BaseModel):
    # noul
    yes_if_above: float | None = None
    no_if_below: float | None = None
    # choice / score
    accept_if_above: float | None = None
    option_thresholds: dict[str, float] = Field(default_factory=dict)


class Rule(BaseModel):
    regex: str
    answer: Any
    field: str | None = None  # match only this top-level state field; default: whole state


class Policy(BaseModel):
    id: str
    role: Role
    kind: Kind
    question: str
    criteria: dict[str, str | None] | list[str] | None = None
    options: list[str] | None = None  # shorthand for choice criteria without descriptions
    thresholds: Thresholds = Field(default_factory=Thresholds)
    escalate_options: list[str] = Field(default_factory=list)
    fallback: list[str] = Field(default_factory=list)  # tiers after jev, e.g. [llm, human]
    fail_mode: Literal["open", "closed"] = "open"
    fail_decision: Any = None
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self) -> Policy:
        if self.kind == "choice" and self.criteria is None and self.options:
            self.criteria = {o: None for o in self.options}
        if self.role == "gating" and self.fail_mode == "closed" and self.fail_decision is None:
            raise ValueError(f"gating policy {self.id!r} with fail_mode=closed needs fail_decision")
        self.to_question()  # validate criteria shape early
        return self

    @property
    def tiers(self) -> list[str]:
        return (["rule"] if self.rules else []) + ["jev"] + list(self.fallback)

    def to_question(self) -> Question:
        return Question(key=self.id, kind=self.kind, instructions=self.question, criteria=self.criteria)


def load_policies(path: str | Path) -> list[Policy]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else [data]
    return [Policy.model_validate(d) for d in items]
