"""Alert triage service.

  ops-judge alerts watch --prometheus http://127.0.0.1:9090     poll Prometheus (no Alertmanager needed)
  ops-judge alerts serve --prometheus http://... --port 9095     receive Alertmanager webhooks

Each newly firing alert: facts -> cascade (rule -> decision model -> LLM; fail closed = page) -> route
(page / digest / mute). Pages go to OPSJUDGE_PAGE_WEBHOOK (Slack-compatible) when set; everything is
printed and written to the audit log.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from judge_core import Cascade, JevJudge, LLMJudge, RuleJudge, Store, load_policies
from judge_core.types import JudgeError, JudgeRequest, JudgeResult

from .facts import AlertHistory, build_state
from .prom import Prometheus
from .route import Route, ongoing, route

ROOT = Path(__file__).resolve().parents[2]


class _Skipped:
    def __init__(self, name: str, reason: str):
        self.name, self.reason = name, reason

    def judge(self, req: JudgeRequest) -> JudgeResult:
        raise JudgeError(f"skipped: {self.reason}")


class AlertTriage:
    def __init__(self, prom: Prometheus, jev_url: str | None = None, use_llm: bool = True, store: Store | None = None):
        policies = load_policies(ROOT / "policies/alerts.yaml")
        if jev_url:
            jev = JevJudge(url=jev_url)
        elif os.environ.get("TYPESAFE_API_KEY"):
            jev = JevJudge()
        else:
            jev = _Skipped("jev", "not configured")
        llm = LLMJudge() if use_llm and shutil.which("claude") else _Skipped("llm", "no claude CLI or --no-llm")
        (ROOT / "data").mkdir(exist_ok=True)
        self.store = store or Store(ROOT / "data/alerts_audit.sqlite")
        self.cascade = Cascade(policies, {"rule": RuleJudge.from_policies(policies), "jev": jev, "llm": llm}, self.store)
        self.prom, self.history = prom, AlertHistory()
        self.paged: dict[str, str] = {}  # alertname -> service, for alerts that paged

    def handle(self, alert: dict, firing: list[dict], now: float | None = None) -> dict:
        now = now or time.time()
        state = build_state(alert, self.prom, self.history, now=now, firing=firing)
        out = self.cascade.run(state, context={"alert": alert.get("labels", {})})
        r = route(out["alert_page"], out.get("alert_severity"),
                  ongoing(self.paged, state["service"], state["other_alerts_firing_now"]))
        self.history.add(state["alert"], state["service"], now)
        if r.kind == "page":
            self.paged[state["alert"]] = state["service"]
            notify_page(state, r)
        return {"state": state, "page": out["alert_page"].model_dump(), "severity": out["alert_severity"].model_dump(),
                "route": vars(r)}


def notify_page(state: dict, r: Route) -> None:
    hook = os.environ.get("OPSJUDGE_PAGE_WEBHOOK")
    if not hook:
        return
    text = f":rotating_light: {state['alert']} on {state['service']}: {state['summary']} ({r.reason})"
    req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def show(res: dict) -> None:
    s, r = res["state"], res["route"]
    tag = {"page": "PAGE  ", "digest": "digest", "mute": "mute  "}[r["kind"]]
    err = s["user_request_error_ratio_1m"]
    print(f"{time.strftime('%H:%M:%S')} {tag} {s['alert']:<24} {s['service']:<8} "
          f"err={'-' if err is None else f'{err:.0%}'} "
          f"p95={'-' if s['user_request_p95_latency_s'] is None else str(s['user_request_p95_latency_s']) + 's'} "
          f"replicas={s['replicas_available']}/{s['replicas_wanted']}  {r['reason']}", flush=True)


def key(alert: dict) -> str:
    return json.dumps(alert.get("labels", {}), sort_keys=True)


def watch(args) -> None:
    prom = Prometheus(args.prometheus)
    t = AlertTriage(prom, args.jev, not args.no_llm)
    active: set[str] = set()
    print(f"watching {args.prometheus} every {args.interval}s", flush=True)
    while True:
        alerts = prom.alerts()
        firing = [a for a in alerts if a.get("state") == "firing"]
        now_keys = {key(a) for a in firing}
        for a in firing:
            if key(a) not in active and (not args.namespace or a["labels"].get("namespace") == args.namespace):
                show(t.handle(a, firing))
        active = now_keys
        time.sleep(args.interval)


def serve(args) -> None:
    prom = Prometheus(args.prometheus)
    t = AlertTriage(prom, args.jev, not args.no_llm)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            firing = [{"labels": a.get("labels", {}), "annotations": a.get("annotations", {}), "state": "firing"}
                      for a in body.get("alerts", []) if a.get("status") == "firing"]
            results = [t.handle(a, firing) for a in firing]
            for r in results:
                show(r)
            data = json.dumps([r["route"] for r in results]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    print(f"Alertmanager webhook on :{args.port}/ (receiver url: http://<host>:{args.port}/)", flush=True)
    ThreadingHTTPServer(("", args.port), Handler).serve_forever()


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="ops-judge alerts")
    ap.add_argument("mode", choices=["watch", "serve"])
    ap.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    ap.add_argument("--jev", help="System One endpoint for the decision-model tier")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--namespace", help="only alerts from this namespace (watch mode)")
    ap.add_argument("--port", type=int, default=9095)
    args = ap.parse_args(argv)
    try:
        (watch if args.mode == "watch" else serve)(args)
    except KeyboardInterrupt:
        sys.exit(0)
