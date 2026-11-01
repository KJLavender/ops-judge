"""TypeSafe Jev via POST /v1/systemone. All questions in a request go out in one call.

The same API is served locally by llama.cpp's `llama-server` (/v1/systemone, since 2026-10-02) for open
decision models such as Kev, Laya and Julia-1: point JEV_BASE_URL at it, no key needed.

Env:
  TYPESAFE_API_KEY   required for remote URLs
  JEV_BASE_URL       default https://api.typesafe.ai/v1/systemone
  JEV_MODEL          default jev-latest (local llama-server ignores it)
  JEV_TIMEOUT        seconds, default 10
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

from ..types import Answer, JudgeError, JudgeRequest, JudgeResult, Question

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
PRICE_PER_MTOK_INPUT = 0.042  # output tokens are free
LOCAL_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/")


def build_payload(req: JudgeRequest, model: str) -> dict:
    questions = {}
    for q in req.questions:
        item: dict = {"type": q.kind, "instructions": q.instructions}
        if q.criteria is not None:
            item["criteria"] = q.criteria
        questions[q.key] = item
    return {"model": model, "state": req.state, "questions": questions}


def parse_answer(q: Question, a: dict) -> Answer:
    if q.kind == "noul":
        return Answer.noul(a["noul"])
    probs = a.get("probabilities") or {}
    if q.kind == "score":
        # Keys may be level indices or level descriptions; normalize to index strings.
        levels = list(q.criteria)  # type: ignore[arg-type]
        probs = {str(levels.index(k)) if k in levels else str(int(k)): float(v) for k, v in probs.items()}
        if not probs:
            probs = {str(int(round(a["score"]))): 1.0}
    elif not probs:
        probs = {a["choice"]: 1.0}
    probs = {o: probs.get(o, 0.0) for o in q.options}
    return Answer.from_probs(q.kind, probs, a.get("confidence"))


class JevJudge:
    name = "jev"

    def __init__(self, api_key: str | None = None, model: str | None = None, url: str | None = None,
                 timeout: float | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.model = model or os.environ.get("JEV_MODEL", "jev-latest")
        self.url = url or os.environ.get("JEV_BASE_URL", DEFAULT_URL)
        # Local CPU decision models on long inputs can take well over the 10 s a hosted API needs.
        timeout = timeout or float(os.environ.get("JEV_TIMEOUT", "10"))
        self.client = httpx.Client(timeout=timeout, transport=transport)

    @property
    def is_local(self) -> bool:
        return bool(LOCAL_URL.match(self.url))

    def judge(self, req: JudgeRequest) -> JudgeResult:
        if not self.api_key and not self.is_local:
            raise JudgeError("TYPESAFE_API_KEY is not set")
        # Serialize ourselves: non-ASCII must go out as real UTF-8, not \u escapes (Jev degrades on escaped CJK).
        body = json.dumps(build_payload(req, self.model), ensure_ascii=False).encode("utf-8")
        t0 = time.perf_counter()
        try:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            resp = self.client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as e:
            raise JudgeError(f"jev request failed: {e}") from e
        latency = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            raise JudgeError(f"jev HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            answers = {q.key: parse_answer(q, data["answers"][q.key]) for q in req.questions}
        except (KeyError, ValueError, TypeError) as e:
            raise JudgeError(f"jev response missing/invalid answer: {e}") from e
        tokens = 0 if self.is_local else (data.get("usage") or {}).get("input_tokens", 0)
        return JudgeResult(judge=self.name, answers=answers, latency_ms=latency,
                           cost_usd=tokens * PRICE_PER_MTOK_INPUT / 1e6, raw=data)
