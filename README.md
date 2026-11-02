# ops-judge

**English** | [繁體中文](README.zh-TW.md)

A calibrated, auditable judgment layer for ops: **CI failure triage** and **alert noise reduction**.

Most CI/alert triage bots send every event to an LLM. ops-judge puts cheap, millisecond-latency judges in front and escalates only the gray zone:

```
RuleJudge (regex) ─▶ JevJudge (System One model) ─▶ LLMJudge ─▶ HumanQueue
```

- Each tier sends **one batched request** containing only the questions still undecided.
- Confidence is **calibrated** per (policy, backend) before thresholds are applied.
- Every decision is written to a **SQLite audit log**: input hash, tier-by-tier answers, latency, cost, and the path taken.
- Human labels feed back as calibration and training data.

## What it is, and what it is not

**ops-judge is a triage judge, not a repair agent.** It answers "is this CI failure a code bug, an environment problem, or a fluke — rerun it, page a person, or hand it to a repair agent?" It does not edit code or open PRs.

| Role | Examples | Responsible for |
|---|---|---|
| **Judge** (this project) | ops-judge | Cheap, auditable decision on cause and next step |
| Workflow framework | GitHub Agentic Workflows | When an AI runs and which actions it may take |
| Repair agent | Copilot coding agent, auto-heal CI agents | Read the error, change code, run tests, open a PR |

They compose rather than compete. Triage filters out the many "just rerun it" failures first, so a repair agent never spends effort fixing something that was not broken:

```
CI failure ─▶ ops-judge: cause and next step
               ├─ transient ─▶ automatic rerun
               ├─ environment / infrastructure ─▶ notify the platform team
               ├─ code bug ─▶ repair agent ─▶ rerun tests, human review
               └─ unsure ─▶ human queue
```

**What works today** (honest version):

- **Done**: CI log preprocessing, tiered judging, calibration, audit log, human queue, evaluation tooling, and a command that triages any public GitHub Actions run (`ops-judge triage`).
- **Not wired yet**: the rerun / PR comment / notify decisions are implemented but not yet executed against GitHub; alert noise reduction is planned; the hosted Jev tier awaits an API key.
- So today it is a **usable, tested judgment layer evaluated on real data**, not a deployed self-healing CI bot.

## Status

| Part | State |
|---|---|
| `judge_core`: four backends, YAML policies, cascade, audit log, human queue | done |
| Demo: case explorer (`docs/index.html`) and `ops-judge triage <run-url>` | done |
| Calibration (isotonic / Platt) and evaluation (threshold sweep, ECE, compare with error correlation, learning curve) | done |
| CI preprocessing: log cleanup, error excerpt, stack/changed-file overlap, infra/env signals, Drain3 templates | done |
| CI actions: rerun / comment / notify, no auto-rerun for fork PRs, flaky → code_bug when the rerun fails | done (logic only, not yet wired to GitHub) |
| Real-data evaluation (`collect_reruns.py`, `collect_history.py`, `collect_context.py`, `build_dataset.py`, `eval_reruns.py`, `experiments.py`) | 316 failures from 8 repos → 180 cases; always-rerun 77.2%, LLM 82.8% after calibration (90% when answering only above 85% confidence), local Kev-4B 80.6% (see `plans/01-ops-judge-plan.md` §11.3) |
| Jev live run | waiting for an API key (`TYPESAFE_API_KEY`) |
| Alert pipeline (Alertmanager webhook, K8s + Chaos Mesh) | planned (see `plans/01-ops-judge-plan.md`) |

## Demo

- **Case explorer** — live at **https://kjlavender.github.io/ops-judge/** (`docs/index.html`, no backend): all 180 real failures; for each one the facts the judges saw, every model's probabilities, the cascade path, and what actually happened. Rebuild with `python scripts/build_site.py`.
- **Live triage** of any public GitHub Actions run:

```bash
ops-judge triage https://github.com/sveltejs/kit/actions/runs/40762220138
ops-judge triage <run-url> --jev http://127.0.0.1:8094/v1/systemone   # add a local decision model tier
ops-judge triage <run-url> --no-history --json                         # faster / machine-readable
```

It prints the extracted facts, each tier's answer (accept or escalate), the decision, the planned action and, for runs that were rerun, what actually happened.

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest                       # 46 tests, no network
python scripts/toy_eval.py             # 20-item toy CI set → examples/out/{sweep,calibration}.png, compare.json, audit.sqlite
python scripts/collect_reruns.py owner/repo --pages 3   # flaky labels from GitHub Actions reruns (needs gh auth)
```

## Backends and configuration

| Backend | Notes | Env |
|---|---|---|
| `RuleJudge` | Regex rules from the policy YAML, plus optional Python rules | — |
| `JevJudge` | `POST /v1/systemone`; non-ASCII is sent as raw UTF-8, never `\u` escapes | `TYPESAFE_API_KEY`, `JEV_MODEL` (default `jev-latest`), `JEV_BASE_URL` |
| `LLMJudge` | Local `claude -p` CLI (subscription, no API key); the model self-reports a probability distribution, so calibrate it like any other backend | `OPSJUDGE_LLM_MODEL` (default `claude-sonnet-5-5`), `OPSJUDGE_LLM_EFFORT` (default `medium`) |
| `HumanQueue` | SQLite review queue; inputs that already have a label are answered directly | — |

## Policies

See `policies/ci.yaml` and `policies/alerts.yaml`. What each role does when a judgment stays unresolved:

| Role | Gray zone | All tiers failed / still unsure |
|---|---|---|
| routing | escalate, then human queue | `unrouted` / queued |
| gating | escalate; never pass on a gray answer | `fail_decision` (e.g. page anyway) |
| ranking | no threshold, accept the score | `unscored` |
| extraction | per-use-case thresholds | `unprocessed` |

## Layout

```
judge_core/   types, policy, cascade, store (audit + queue), calibration, evaluate, judges/
ops_judge/ci/ extract (log → excerpt + signals), facts (precomputed state), templates (Drain3), actions
policies/     YAML policies
scripts/      toy_eval.py, collect_reruns.py
examples/     toy_ci.jsonl
plans/        project plans (Traditional Chinese)
```
