"""Build the state sent to the judges. Every comparison is precomputed here: Jev cannot count, compare
dates, or intersect two lists reliably, so it gets booleans and numbers, not raw lists."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import extract
from .templates import TemplateIndex


@dataclass
class CIFailure:
    log: str
    changed_files: list[str]
    test_name: str | None = None
    main_history: list[bool] = field(default_factory=list)  # recent results of this test/job on main, True = pass
    main_fixed_by_rerun: int = 0                              # of the main failures above, how many a rerun fixed
    rerun_passed: bool | None = None                          # None = not rerun yet
    is_fork_pr: bool = False


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def overlap(stack: list[str], changed: list[str]) -> list[str]:
    """Stack paths may be absolute or deeper than repo-relative paths; match on suffix."""
    changed_n = [_norm(c) for c in changed]
    hits = []
    for s in stack:
        s_n = _norm(s)
        for c in changed_n:
            if s_n == c or s_n.endswith("/" + c) or c.endswith("/" + s_n):
                if c not in hits:
                    hits.append(c)
    return hits


PYTEST_E = re.compile(r"^E {2,}\S")

# Marker lines that say "something failed" without saying what.
GENERIC = re.compile(r"^Traceback \(most recent call last\)|##\[error\]Process completed with exit code|^FAILED$")


def first_error_line(excerpt: str, log: str | None = None) -> str:
    if log is not None:
        notes = extract.annotations(log)
        if notes:
            return notes[0][:300]
    # pytest marks the actual exception with "E   "; the lines around it are echoed source code, whose string
    # literals can look like errors themselves.
    for line in excerpt.split("\n"):
        if PYTEST_E.match(line):
            return line.strip()[1:].strip()[:300]
    fallback = ""
    for line in excerpt.split("\n"):
        line = line.strip()
        if extract.ERROR_MARKERS.search(line):
            if not GENERIC.search(line):
                return line[:300]
            fallback = fallback or line[:300]
    return fallback


def build_state(f: CIFailure, main_templates: TemplateIndex | None = None) -> dict:
    excerpt = extract.error_excerpt(f.log)
    stack = extract.stack_files(excerpt)
    hits = overlap(stack, f.changed_files)
    err = first_error_line(excerpt, f.log)
    n = len(f.main_history)
    state: dict = {
        "test_name": f.test_name,
        "first_error_line": err,
        "stack_overlaps_changed_files": bool(hits),
        "overlapping_files": hits,
        "changed_files_count": len(f.changed_files),
        "main_runs_considered": n,
        "main_pass_rate": round(sum(f.main_history) / n, 2) if n else None,
        # The job's own flakiness on main: failures there that a plain rerun fixed.
        "main_failures": n - sum(f.main_history),
        "main_failures_fixed_by_rerun": f.main_fixed_by_rerun,
        "rerun_result": {None: "not rerun", True: "passed", False: "failed again"}[f.rerun_passed],
        "infra_signals": extract.infra_signals(excerpt),
        "env_signals": extract.env_signals(excerpt),
        "error_excerpt": excerpt,
    }
    if main_templates is not None and err:
        state["error_seen_on_main_recently"] = main_templates.contains(err)
    return state
