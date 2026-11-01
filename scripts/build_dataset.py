"""Turn rerun-labeled CI failures (collect_reruns.py output) into an eval dataset for ci_failure_class.

The state is built as of the FIRST failure, before any rerun: the decision being evaluated is
"should we rerun?", and the rerun outcome is exactly what produced the label. Including it would leak the answer.

Labels:
  flaky               -> flaky_test (gold)
  persistent_failure  -> not flaky; kept with label null for manual labeling (code_bug / environment / infrastructure)

Usage:
  python scripts/build_dataset.py data/reruns.jsonl --out data/ci_eval.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_judge.ci.facts import CIFailure, build_state  # noqa: E402
from ops_judge.ci.templates import TemplateIndex  # noqa: E402


def load_history(path: Path) -> dict[tuple[str, str], list[dict]]:
    """(repo, job name) -> main-branch history rows from collect_history.py."""
    hist: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                h = json.loads(line)
                hist[(h["repo"], h["job_name"])].append(h)
    return hist


def history_before(rows: list[dict], when: str, days: int) -> list[dict]:
    """Only history strictly before the failure, within the window: no peeking into the future."""
    t = datetime.fromisoformat(when.replace("Z", "+00:00"))
    lo = t - timedelta(days=days)
    return [h for h in rows if lo <= datetime.fromisoformat(h["created_at"].replace("Z", "+00:00")) < t]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--out", default="data/ci_eval.jsonl")
    ap.add_argument("--history", default="data/history.jsonl", help="collect_history.py output; skipped if missing")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    hist = load_history(Path(args.history))
    rows = [json.loads(line) for line in Path(args.src).read_text(encoding="utf-8").splitlines() if line]
    out, labels = [], Counter()
    for r in rows:
        past = history_before(hist.get((r["repo"], r["job_name"]), []), r["created_at"], args.days) if hist else []
        templates = None
        if hist:
            templates = TemplateIndex(use_drain=False)
            templates.seen = {h["error_template"] for h in past if h.get("error_template")}
        f = CIFailure(log=r["log"], changed_files=r["changed_files"], is_fork_pr=r["is_fork_pr"],
                      main_history=[h["first_ok"] for h in past],
                      main_fixed_by_rerun=sum(h["fixed_by_rerun"] for h in past))
        state = build_state(f, templates)
        del state["rerun_result"]  # unknown at decision time; see module docstring
        if not past:
            for k in ("main_runs_considered", "main_pass_rate", "main_failures", "main_failures_fixed_by_rerun"):
                state.pop(k, None)  # no history: absent beats a misleading zero
        if state.get("test_name") is None:
            state.pop("test_name", None)
        label = "flaky_test" if r["label"] == "flaky" else None
        labels[label or "unlabeled(persistent)"] += 1
        out.append({"id": r["id"], "label": label, "repo": r["repo"], "is_fork_pr": r["is_fork_pr"],
                    "log_lines": len(r["log"].splitlines()), "state": state})
    Path(args.out).write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in out), encoding="utf-8")
    print(f"{len(out)} rows -> {args.out}: {dict(labels)}")


if __name__ == "__main__":
    main()
