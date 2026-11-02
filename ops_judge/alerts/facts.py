"""Facts for one alert, computed at the moment it fires. As with CI logs, the judge gets answers, not raw
series: a small model cannot do PromQL or date math, so "error ratio is 31%" and "deployed 4 minutes ago"
are worked out here.

Conventions: the alert carries `namespace` and `service` labels; the service is a Deployment of that name
exposing http_requests_total{code} and http_request_duration_seconds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .prom import Prometheus


@dataclass
class AlertHistory:
    """Alert firings seen so far: (unix time, alertname, service)."""
    events: list[tuple[float, str, str]] = field(default_factory=list)

    def count(self, name: str, service: str, since_s: float, now: float) -> int:
        return sum(1 for t, n, s in self.events if n == name and s == service and now - since_s <= t < now)

    def add(self, name: str, service: str, now: float) -> None:
        self.events.append((now, name, service))


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def build_state(alert: dict, prom: Prometheus, history: AlertHistory, now: float | None = None,
                firing: list[dict] | None = None) -> dict:
    now = now or time.time()
    labels, ann = alert.get("labels", {}), alert.get("annotations", {})
    ns, svc, name = labels.get("namespace", ""), labels.get("service", ""), labels.get("alertname", "")
    sel = f'namespace="{ns}"'
    err = prom.value(f'sum(rate(http_requests_total{{{sel},code=~"5.."}}[1m])) / sum(rate(http_requests_total{{{sel}}}[1m]))')
    p95 = prom.value(f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[1m])))')
    avail = prom.value(f'kube_deployment_status_replicas_available{{{sel},deployment="{svc}"}}')
    want = prom.value(f'kube_deployment_spec_replicas{{{sel},deployment="{svc}"}}')
    newest_rs = prom.value(f'max(kube_replicaset_created{{{sel},replicaset=~"{svc}-.*"}})')
    others = sorted({a["labels"].get("alertname") for a in (firing or [])
                     if a.get("state") == "firing" and a["labels"].get("namespace") == ns
                     and a["labels"].get("alertname") != name})
    state = {
        "alert": name,
        "severity_label": labels.get("severity"),
        "summary": ann.get("summary", ""),
        "service": svc,
        "user_request_error_ratio_1m": _r(err),
        "user_request_p95_latency_s": _r(p95),
        "replicas_available": None if avail is None else int(avail),
        "replicas_wanted": None if want is None else int(want),
        "service_fully_down": avail == 0 if avail is not None else None,
        "other_alerts_firing_now": others,
        "same_alert_fired_in_last_hour": history.count(name, svc, 3600, now),
        "minutes_since_last_deploy": None if newest_rs is None else round((now - newest_rs) / 60, 1),
    }
    return state
