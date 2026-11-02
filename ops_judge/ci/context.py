"""Context facts beyond the failing log itself. Every function only looks at evidence from BEFORE the
failure (`when`), so the same code serves offline evaluation and live triage without leaking the future."""

from __future__ import annotations

from datetime import datetime, timedelta

from .facts import overlap


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _before(rows: list[dict], when: str, days: float) -> list[dict]:
    t = _t(when)
    return [r for r in rows if t - timedelta(days=days) <= _t(r["created_at"]) < t]


def test_history(tests: list[str], main_failures: list[dict], when: str, days: float = 14) -> dict:
    """How often did the SAME failing test(s) fail on the default branch recently, and how often did a
    plain rerun fix it there? Finer than job-level history: one flaky test hides in a big job."""
    wanted = set(tests)
    hits = [r for r in _before(main_failures, when, days) if wanted & set(r.get("tests") or [])]
    return {"same_test_failed_on_main": len(hits),
            "same_test_fixed_by_rerun_on_main": sum(r["fixed_by_rerun"] for r in hits)}


def same_error_elsewhere(template: str, branch: str | None, other_failures: list[dict], when: str,
                         days: float = 3) -> dict:
    """Distinct OTHER branches that hit the same error template recently. Many unrelated branches failing
    the same way points at the environment, not at this change."""
    if not template:
        return {"same_error_on_other_branches_3d": 0}
    branches = {r["head_branch"] for r in _before(other_failures, when, days)
                if r.get("template") == template and r.get("head_branch") != branch}
    return {"same_error_on_other_branches_3d": len(branches)}


# Incidents that can break a CI job. "Disruption with some GitHub services" is GitHub's generic title.
CI_RELEVANT = ("Actions", "Packages", "Git Operations", "API Requests", "Webhooks", "some GitHub services")


def github_incident(when: str, incidents: list[dict]) -> dict:
    """A CI-relevant GitHub status incident already declared and not yet resolved at failure time."""
    t = _t(when)
    for inc in incidents:
        if not any(k.lower() in inc["name"].lower() for k in CI_RELEVANT) or "copilot" in inc["name"].lower():
            continue
        start = _t(inc["created_at"])
        end = _t(inc["resolved_at"]) if inc.get("resolved_at") else None
        if start <= t and (end is None or t <= end):
            return {"github_incident_active": True, "github_incident": inc["name"][:120]}
    return {"github_incident_active": False}


def changed_lines_hit(frames: list[tuple[str, int]], hunks: dict[str, list], slack: int = 3) -> dict:
    """Does a stack frame / compiler error point INTO lines this change touched (not just the same file)?"""
    for path, line in frames:
        for f in overlap([path], list(hunks)):
            if any(lo - slack <= line <= hi + slack for lo, hi in hunks.get(f, [])):
                return {"error_in_changed_lines": True}
    return {"error_in_changed_lines": False}
