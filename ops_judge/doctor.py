"""`ops-judge doctor`: check every external dependency once, with a plain verdict per line."""

from __future__ import annotations

import os
import shutil
import subprocess

from judge_core.judges.jev import JevJudge
from judge_core.types import JudgeError, JudgeRequest, Question


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name:<16} {detail}")
    return ok


def main() -> int:
    results = []
    gh = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8") if shutil.which("gh") else None
    results.append(check("gh CLI", bool(gh and gh.returncode == 0),
                         "logged in" if gh and gh.returncode == 0 else "install gh and run `gh auth login`"))
    claude = shutil.which("claude")
    results.append(check("claude CLI", bool(claude), claude or "not on PATH: the LLM tier will be skipped"))

    jev = JevJudge(timeout=20)
    if not jev.api_key and not jev.is_local:
        check("decision model", True, "not configured (optional): set TYPESAFE_API_KEY, or JEV_BASE_URL to a local llama-server")
    else:
        try:
            q = Question(key="q", kind="noul", instructions="Is the sky blue?")
            r = jev.judge(JudgeRequest(state="The sky is blue.", questions=[q]))
            results.append(check("decision model", True, f"{jev.url} answered P(yes)={float(r.answers['q'].value):.2f} in {r.latency_ms:.0f} ms"))
        except JudgeError as e:
            hint = (" (a jv_live_ key belongs to jevtypesafeai.com, not api.typesafe.ai: set JEV_BASE_URL)"
                    if jev.api_key.startswith("jv_live_") and "jevtypesafeai.com" not in jev.url else "")
            results.append(check("decision model", False, f"{jev.url}: {e}{hint}"))
    hook = os.environ.get("OPSJUDGE_NOTIFY_WEBHOOK")
    check("notify webhook", True, "set" if hook else "not set (optional): notifications go to the Actions job summary")
    return 0 if all(results) else 1
