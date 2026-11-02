"""Inject faults into the k8s alert lab (k8s/alert-lab.yaml) and record every alert that fires, with the facts
at that moment and the ground truth: were users affected by the injected fault?

  kubectl apply -f k8s/alert-lab.yaml
  kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 &
  python scripts/alert_lab.py --prometheus http://127.0.0.1:19090 --rounds 2

Writes data/alerts.jsonl (appends). Only touches the ops-judge-lab namespace.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops_judge.alerts.facts import AlertHistory, build_state  # noqa: E402
from ops_judge.alerts.prom import Prometheus  # noqa: E402

NS = "ops-judge-lab"


def kubectl(*args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, encoding="utf-8", timeout=90)
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"kubectl {' '.join(args)}: timed out")
        return ""
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def shop_pods() -> list[str]:
    return [p.split("/", 1)[1] for p in kubectl("get", "pods", "-l", "app=shop", "--field-selector=status.phase=Running", "-o", "name").split()]


def set_faults(pods: list[str], **kw) -> None:
    query = "&".join(f"{k}={v}" for k, v in kw.items())
    for p in pods:
        kubectl("exec", p, "--", "python", "-c",
                f"import urllib.request as u; u.urlopen('http://localhost:8080/admin?{query}', timeout=5).read()", check=False)


def reset() -> None:
    kubectl("scale", "deploy/shop", "--replicas=2")
    kubectl("rollout", "status", "deploy/shop", "--timeout=180s", check=False)
    set_faults(shop_pods(), error_rate=0, latency_ms=0, memory_mb=0)


# name, users affected?, inject
SCENARIOS = [
    ("errors_50pct", True, lambda: set_faults(shop_pods(), error_rate=0.5)),
    ("errors_8pct", True, lambda: set_faults(shop_pods(), error_rate=0.08)),
    ("slow_900ms", True, lambda: set_faults(shop_pods(), latency_ms=900)),
    ("full_outage", True, lambda: kubectl("scale", "deploy/shop", "--replicas=0")),
    ("one_pod_oom", False, lambda: set_faults(shop_pods()[:1], memory_mb=400)),      # limit is 256Mi
    ("memory_high", False, lambda: set_faults(shop_pods()[:1], memory_mb=150)),
    ("batch_crash", False, lambda: kubectl("exec", "deploy/batch", "--", "kill", "-USR1", "1", check=False)),
    ("rollout", False, lambda: kubectl("rollout", "restart", "deploy/shop")),
]


def alerts(prom: Prometheus) -> list[dict]:
    """prom.alerts() that survives a restarting port-forward: retry instead of hanging or crashing."""
    for _ in range(12):
        try:
            return prom.alerts()
        except OSError as e:
            print(f"   prometheus unreachable ({e}); retrying", flush=True)
            time.sleep(10)
    raise RuntimeError("prometheus unreachable for 2 minutes")


def lab_alerts(prom: Prometheus) -> list[dict]:
    return [a for a in alerts(prom) if a["labels"].get("namespace") == NS]


def key(a: dict) -> str:
    return json.dumps(a["labels"], sort_keys=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prometheus", default="http://127.0.0.1:19090")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--observe", type=int, default=180, help="seconds to watch after each injection")
    ap.add_argument("--out", default=str(ROOT / "data/alerts.jsonl"))
    ap.add_argument("--only", help="comma-separated scenario names")
    args = ap.parse_args()
    prom, history = Prometheus(args.prometheus), AlertHistory()
    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    wanted = set(args.only.split(",")) if args.only else None

    for rnd in range(1, args.rounds + 1):
        for name, impact, inject in SCENARIOS:
            if wanted and name not in wanted:
                continue
            print(f"round {rnd} · {name}: resetting, waiting for a quiet namespace", flush=True)
            reset()
            # Wait for a quiet namespace so alerts are attributable to this scenario.
            deadline = time.time() + 420
            while (left := lab_alerts(prom)) and time.time() < deadline:
                print(f"   still active: {sorted({a['labels']['alertname'] for a in left})}", flush=True)
                time.sleep(20)
            already = {key(a) for a in lab_alerts(prom)}
            print(f"round {rnd} · {name} (users affected: {impact})", flush=True)
            inject()
            seen: set[str] = set()
            end = time.time() + args.observe
            while time.time() < end:
                firing = [a for a in alerts(prom) if a.get("state") == "firing"]
                for a in firing:
                    k = key(a)
                    if a["labels"].get("namespace") != NS or k in seen or k in already:
                        continue
                    seen.add(k)
                    now = time.time()
                    try:
                        state = build_state(a, prom, history, now=now, firing=firing)
                    except OSError as e:
                        print(f"   could not read facts for {a['labels'].get('alertname')}: {e}", flush=True)
                        seen.discard(k)
                        continue
                    history.add(state["alert"], state["service"], now)
                    row = {"id": f"r{rnd}-{name}-{state['alert']}", "round": rnd, "scenario": name, "users_affected": impact,
                           "t": now, "labels": a["labels"], "annotations": a.get("annotations", {}), "state": state}
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(f"   fired {state['alert']:<24} err={state['user_request_error_ratio_1m']} "
                          f"p95={state['user_request_p95_latency_s']} replicas={state['replicas_available']}", flush=True)
                time.sleep(10)
    reset()
    print("done", flush=True)


if __name__ == "__main__":
    main()
