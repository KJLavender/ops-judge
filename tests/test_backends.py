"""One contract, four backends: Rule, Jev (mocked HTTP), LLM (fake CLI runner), Human (pre-labeled)."""

import json

import httpx
import pytest

from judge_core.judges.human import HumanQueue
from judge_core.judges.jev import JevJudge, build_payload
from judge_core.judges.llm import LLMJudge, build_schema
from judge_core.judges.rule import RuleJudge
from judge_core.policy import Rule
from judge_core.store import Store
from judge_core.types import JudgeError, JudgeRequest, JudgeResult, Question

QUESTIONS = [
    Question(key="is_outage", kind="noul", instructions="Are users affected?"),
    Question(key="team", kind="choice", instructions="Which team?", criteria={"web": "frontend", "db": "database"}),
    Question(key="severity", kind="score", instructions="How severe?", criteria=["low", "mid", "high"]),
]
STATE = {"alert": "資料庫連線逾時 db timeout", "error_rate": 0.31}
REQ = JudgeRequest(state=STATE, questions=QUESTIONS)


def jev_transport(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={
            "model": "jev-latest",
            "answers": {
                "is_outage": {"type": "noul", "noul": 0.93},
                "team": {"type": "choice", "choice": "db", "probabilities": {"web": 0.1, "db": 0.9}, "confidence": 0.8},
                "severity": {"type": "score", "score": 2, "legend": {"0": "low", "1": "mid", "2": "high"},
                             "probabilities": {"0": 0.05, "1": 0.15, "2": 0.8}, "confidence": 0.7},
            },
            "usage": {"input_tokens": 1000, "output_tokens": 3},
        })
    return httpx.MockTransport(handler)


def llm_runner(system, user, schema):
    return {"total_cost_usd": 0.002, "structured_output": {
        "is_outage": {"p_true": 0.9},
        "team": {"probabilities": {"web": 0.2, "db": 0.8}},
        "severity": {"probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
    }}


def make_backends():
    captured: dict = {}
    rule = RuleJudge({"is_outage": [Rule(regex="timeout", answer=True)],
                      "team": [Rule(regex="db", answer="db", field="alert")],
                      "severity": [Rule(regex="error_rate", answer=2)]})
    jev = JevJudge(api_key="k", transport=jev_transport(captured))
    llm = LLMJudge(runner=llm_runner)
    store = Store()
    human = HumanQueue(store)
    h = REQ.input_hash()
    for key, label in {"is_outage": True, "team": "db", "severity": 2}.items():
        store.label(store.enqueue(key, h, STATE, {}, {}), label)
    return {"rule": rule, "jev": jev, "llm": llm, "human": human}, captured


@pytest.mark.parametrize("name", ["rule", "jev", "llm", "human"])
def test_backend_contract(name):
    backends, _ = make_backends()
    res = backends[name].judge(REQ)
    assert isinstance(res, JudgeResult) and res.judge == name
    assert set(res.answers) == {"is_outage", "team", "severity"}
    for q in QUESTIONS:
        a = res.answers[q.key]
        assert a.kind == q.kind
        assert 0.0 <= a.confidence <= 1.0
        assert abs(sum(a.probabilities.values()) - 1) < 1e-6
        assert set(a.probabilities) == set(q.options)
    assert res.answers["is_outage"].value > 0.5
    assert res.answers["team"].value == "db"
    assert res.answers["severity"].value == 2
    assert res.latency_ms >= 0 and res.cost_usd >= 0


def test_jev_sends_utf8_not_escaped_and_batches_questions():
    backends, captured = make_backends()
    res = backends["jev"].judge(REQ)
    body = captured["body"]
    assert "資料庫".encode("utf-8") in body and b"\\u8cc7" not in body
    sent = json.loads(body)
    assert set(sent["questions"]) == {"is_outage", "team", "severity"}
    assert sent["questions"]["team"]["criteria"] == {"web": "frontend", "db": "database"}
    assert res.answers["team"].confidence == 0.8  # Jev's concentration value, not the top probability
    assert res.cost_usd == pytest.approx(1000 * 0.042 / 1e6)


def test_jev_errors_become_judge_error():
    jev = JevJudge(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    with pytest.raises(JudgeError):
        jev.judge(REQ)
    with pytest.raises(JudgeError):
        JevJudge(api_key="").judge(REQ)


def test_human_enqueues_unlabeled_once():
    store = Store()
    human = HumanQueue(store)
    r1 = human.judge(REQ)
    r2 = human.judge(REQ)
    assert r1.answers == {} and r1.raw["queued"] == r2.raw["queued"]
    assert len(store.pending()) == 3


def test_payload_and_schema_shapes():
    p = build_payload(REQ, "jev-latest")
    assert p["questions"]["severity"] == {"type": "score", "instructions": "How severe?", "criteria": ["low", "mid", "high"]}
    s = build_schema(QUESTIONS)
    assert s["properties"]["team"]["properties"]["probabilities"]["required"] == ["web", "db"]
    assert s["properties"]["severity"]["properties"]["probabilities"]["required"] == ["0", "1", "2"]


def test_local_llama_server_needs_no_key_and_costs_nothing():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return jev_transport({}).handle_request(request)

    jev = JevJudge(api_key="", url="http://127.0.0.1:8094/v1/systemone", transport=httpx.MockTransport(handler))
    res = jev.judge(REQ)
    assert captured["auth"] is None and res.cost_usd == 0
    assert res.answers["team"].value == "db"
