"""Turn the judges' answers into one of three routes: page now, add to the hourly digest, or mute."""

from __future__ import annotations

from dataclasses import dataclass

from judge_core.cascade import Decision


@dataclass
class Route:
    kind: str     # page | digest | mute
    reason: str


def route(page: Decision, severity: Decision | None, ongoing_page: str | None = None) -> Route:
    """`ongoing_page` names an alert on the same service that already paged and is STILL firing: that is the
    same incident, so don't wake anyone twice. A fixed time window is wrong here; it swallows a second,
    unrelated incident that starts soon after the first one ended."""
    sev = severity.decision if severity and severity.status == "decided" else None
    if page.decision is True:
        if ongoing_page:
            return Route("mute", f"same incident as {ongoing_page}, which already paged and is still firing")
        why = "decided by " + (page.decided_by or page.status) + ("" if page.status == "decided" else " (fail closed)")
        return Route("page", why + (f", severity {sev}" if sev is not None else ""))
    return Route("digest", "no user impact" + (f", severity {sev}" if sev is not None else ""))


def ongoing(paged: dict[str, str], service: str, firing_names: list[str]) -> str | None:
    """`paged` maps alertname -> service for alerts that paged; return one that is still firing on `service`."""
    return next((name for name, svc in paged.items() if svc == service and name in firing_names), None)
