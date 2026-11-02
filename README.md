# ops-judge

**English** | [繁體中文](README.zh-TW.md)

When a CI job goes red, someone has to work out whether their change broke it or whether it was a flake that a rerun would fix. ops-judge makes that call: rerun it, send it to a person, or pass it on to whatever fixes code. It does not edit code or open PRs itself.

It tries cheap things first. A regex can tell you the disk is full. A small local model can often tell you the npm registry was down. Only the unclear cases go to an LLM, and the ones the LLM isn't sure about go to a person.

Live case explorer with all 180 evaluated failures: https://kjlavender.github.io/ops-judge/

## What it looks like

Point it at any public GitHub Actions run. This one failed on the first attempt and passed on the second; ops-judge only sees the first attempt.

```
$ ops-judge triage https://github.com/sveltejs/kit/actions/runs/40762220138

▶ sveltejs/kit  run 40762220138  ·  bun

1. Facts (precomputed, 422 log lines → 29-line excerpt)
  first error        Error: Process from config.webServer was not able to start. Exit code: 1
  touches PR files   False
  main history       pass rate 90% over 29 runs · 3 failures, 1 fixed by rerun
  error seen on main True

2. Cascade
  rule   no answer
  jev    skipped: no TYPESAFE_API_KEY and no --jev URL
  llm    code_bug        ██████·············· 0.30  escalate
         flaky_test 0.25   unknown 0.23
  human  no answer → queued for review (#3)

3. Decision
  queued  by -  ·  P(rerun fixes it) 0.27  ·  5.3s judging  ·  $0.0110
  action: needs_review — undecided (queued); waiting for a human
  what actually happened: attempt 2 concluded success (the decision above did not see this)
```

The LLM wasn't sure (its top answer was 0.30), so instead of guessing it handed the case to a person. The rerun did fix it. A lower threshold would have caught this one; how to pick that threshold is most of what the evaluation below is about.

## How it decides

```
rule (regex) → decision model (Kev-4B locally, or Jev) → LLM → human queue
```

Each tier returns a probability for every answer, not just a label. Thresholds live in YAML (`policies/ci.yaml`): `flaky_test` needs 0.85 because it triggers an automatic rerun, `unknown` always goes up a tier, and runs from fork PRs are never rerun automatically, because anyone can put text in a fork's logs. Every decision is written to a SQLite audit log with the answers from each tier, the latency and the cost.

The model never sees a raw 3,000-line log. Preprocessing cuts the log at the failed step (what comes after is cleanup noise), keeps GitHub's `##[error]` annotation, and works out facts in advance: does the stack trace touch files the PR changed, how often this job failed on `main` in the two weeks before, whether a rerun fixed it there. A small model can't count or compare dates reliably, so it gets the answers to those questions rather than the raw data.

## Does it work?

I used GitHub's own rerun history as the answer key. If a job failed and passed when someone hit "re-run", a rerun would have fixed it; if it failed again, it wouldn't. That gave 316 failed jobs from 8 repos (airflow, deno, sveltejs/kit, vue, cpython, ruff, TypeScript, transformers), 180 after removing duplicates. The judge only sees what existed before the rerun.

| | accuracy |
|---|---|
| Always say "rerun" | 77.2% |
| LLM, raw probabilities | 67.2% |
| LLM, calibrated | 82.8% |
| Kev-4B (runs on your own machine), calibrated | 80.6% |
| LLM, calibrated, answering only when ≥ 85% sure | 89.5% on 48% of failures; the rest go to a person |

Three things surprised me:

- The raw LLM is worse than always saying "rerun". Fixing what its confidence means, by fitting it on known outcomes and checking it on cases the fit never saw, was the single biggest improvement: 67% to 83%.
- More context didn't help. Adding the failing test's own history, the same error on other branches, GitHub incidents and whether the error sits in changed lines moved AUROC from 0.722 to 0.733. Splitting the question into two yes/no questions made both models worse.
- Whether it is worth running at all depends on what a person's time costs. If looking into a failure takes 15 minutes, "always rerun" is already close to optimal and the model saves about 4%. At 5 minutes it saves about 12%. It helps on repos where failures have mixed causes (airflow, sveltejs/kit, ruff) and adds nothing where nearly everything is a flake (deno, vue).

The first version of the labels was wrong: I labelled by whether the whole run passed on rerun, but a run can stay red because of one job while another job passed. Labelling each job by its own last attempt fixed 20 cases. The full write-up, including what didn't work, is in [`plans/01-ops-judge-plan.md`](plans/01-ops-judge-plan.md) §11.

## Where it fits

ops-judge is the triage step, not the repair step. It pairs with things like Copilot coding agent or a self-healing agent such as [AIOps-v2](https://github.com/KJLavender/AIOps-v2): triage filters out the failures that only needed a rerun, so the repair agent only spends effort on ones that are actually broken.

```
CI failure → ops-judge
               ├ transient   → rerun
               ├ environment → tell the platform team
               ├ code bug    → repair agent → tests, human review
               └ unsure      → human
```

## Run it

```bash
pip install -e ".[dev]"
python -m pytest                      # 46 tests, no network

ops-judge triage <run-url>            # needs an authenticated gh CLI
ops-judge triage <run-url> --jev http://127.0.0.1:8094/v1/systemone   # add a local decision model
ops-judge triage <run-url> --no-history --no-context                  # faster
```

The LLM tier calls the local `claude` CLI, so it runs on a Claude subscription without an API key. The decision-model tier speaks the System One API, which both TypeSafe's hosted Jev and llama.cpp's `llama-server` (`/v1/systemone`) implement; Kev-4B, Laya and Julia-1 run locally that way.

| Variable | Default | |
|---|---|---|
| `TYPESAFE_API_KEY` | | hosted Jev; not needed for a local llama-server |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1/systemone` | |
| `JEV_TIMEOUT` | `10` | seconds; raise it for CPU-only local models |
| `OPSJUDGE_LLM_MODEL` | `claude-sonnet-5-5` | |
| `OPSJUDGE_LLM_EFFORT` | `medium` | |

Reproducing the evaluation:

```bash
python scripts/collect_reruns.py apache/airflow denoland/deno ... --pages 30
python scripts/collect_context.py && python scripts/collect_history.py && python scripts/collect_context.py
python scripts/build_dataset.py data/reruns.jsonl
python scripts/eval_reruns.py --judges llm,kev@http://127.0.0.1:8094/v1/systemone
python scripts/experiments.py
python scripts/build_site.py
```

## Not done yet

- The rerun, PR-comment and notify actions are decided but not executed against GitHub. Wiring that up as a GitHub Action is the next step.
- The hosted Jev tier hasn't been tested; I don't have an API key yet. Kev-4B stands in for it locally.
- The alert noise-reduction half of the original plan (Alertmanager, Kubernetes, Chaos Mesh) hasn't been started.
- 180 cases is small, and a few repos dominate it.

## Layout

```
judge_core/     judges (rule, Jev/System One, LLM, human queue), policies, cascade, audit store, calibration, evaluation
ops_judge/ci/   log extraction, facts, context facts, GitHub access, actions
ops_judge/triage.py   the CLI
policies/       YAML policies
scripts/        data collection, evaluation, experiments, site build
site/, docs/    case explorer template and the built page
plans/          plans and notes (Traditional Chinese)
```
