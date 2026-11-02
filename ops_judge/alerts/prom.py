"""Minimal Prometheus HTTP API client (stdlib only)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


class Prometheus:
    def __init__(self, url: str, timeout: float = 10):
        self.url, self.timeout = url.rstrip("/"), timeout

    def _get(self, path: str, **params) -> dict:
        q = f"?{urllib.parse.urlencode(params)}" if params else ""
        with urllib.request.urlopen(f"{self.url}{path}{q}", timeout=self.timeout) as resp:
            return json.load(resp)["data"]

    def value(self, expr: str) -> float | None:
        """First sample of an instant query, or None when the query returns nothing / NaN."""
        result = self._get("/api/v1/query", query=expr)["result"]
        if not result:
            return None
        v = float(result[0]["value"][1])
        return None if v != v else v

    def alerts(self) -> list[dict]:
        """Alerts as Prometheus evaluates them (state: pending | firing)."""
        return self._get("/api/v1/alerts")["alerts"]
