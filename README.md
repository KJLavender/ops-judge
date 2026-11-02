# ops-judge

**English** | [繁體中文](README.zh-TW.md)

When a CI job goes red, someone has to work out whether their change broke it or whether it was a flake that a rerun would fix. ops-judge makes that call: rerun it, send it to a person, or pass it on to whatever fixes code. It does not edit code or open PRs itself.

It tries cheap things first. A regex can tell you the disk is full. A small local model can often tell you the npm registry was down. Only the unclear cases go to an LLM, and the ones the LLM isn't sure about go to a person.

Live case explorer with all 287 evaluated failures: https://kjlavender.github.io/ops-judge/

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

I used GitHub's own rerun history as the answer key. If a job failed and passed when someone hit "re-run", a rerun would have fixed it; if it failed again, it wouldn't. That gave 447 failed jobs from 14 repos (airflow, deno, sveltejs/kit, vue, cpython, ruff, sentry-python, k6, pydantic, electron/forge, superset, TypeScript, transformers, langchain), 287 after removing duplicates. The judge only sees what existed before the rerun.

| | accuracy |
|---|---|
| Always say "rerun" | 80.5% |
| LLM, raw probabilities | 63.1% |
| LLM, calibrated | 84.3% |
| Kev-4B (runs on your own machine), calibrated | 80.8% |
| LLM, calibrated, answering only when ≥ 90% sure | 91.3% on 36% of failures; the rest go to a person |

Three things surprised me:

- The raw LLM is worse than always saying "rerun". Fixing what its confidence means, by fitting it on known outcomes and checking it on cases the fit never saw, was the single biggest improvement: 63% to 84%.
- More context didn't help. Adding the failing test's own history, the same error on other branches, GitHub incidents and whether the error sits in changed lines moved AUROC from 0.729 to 0.736. Splitting the question into two yes/no questions made both models worse.
- Whether it is worth running at all depends on what a person's time costs. If looking into a failure takes 15 minutes, "always rerun" is already close to optimal and the model saves about 3%. At 5 minutes it saves about 9%. It helps on repos where failures have mixed causes (airflow, sveltejs/kit, ruff, sentry-python) and adds little where nearly everything is a flake.

The first version of the labels was wrong: I labelled by whether the whole run passed on rerun, but a run can stay red because of one job while another job passed. Labelling each job by its own last attempt fixed 23 cases. The full write-up, including what didn't work, is in [`plans/01-ops-judge-plan.md`](plans/01-ops-judge-plan.md) §11.

## Alerts

The same cascade decides whether an alert should wake someone. `ops-judge alerts watch` polls Prometheus (no Alertmanager needed); `ops-judge alerts serve` receives Alertmanager webhooks. For each new alert it works out the facts first (the service's request error ratio and p95 latency, available replicas, which other alerts are firing, how recently it was deployed), then routes it: page now, put it in the digest, or mute it because an alert on the same service already paged and is still firing. If no model can decide, it pages anyway.

I tested it on the k3s cluster at home with a small "shop" service, synthetic traffic and seven alert rules, injecting eight kinds of faults twice. This is the live output from the second round:

```
$ ops-judge alerts watch --prometheus http://127.0.0.1:19090 --namespace ops-judge-lab
21:33:20 PAGE   ShopHighErrorRate        shop     err=56% p95=0.048s replicas=2/2  decided by llm, severity 3
21:35:34 PAGE   ShopHighErrorRate        shop     err=6% p95=0.048s replicas=2/2  decided by llm, severity 2
21:38:12 PAGE   ShopSlowResponses        shop     err=0% p95=2.244s replicas=2/2  decided by fallback (fail closed), severity 2
21:41:38 PAGE   ShopDown                 shop     err=- p95=- replicas=0/0  decided by rule, severity 4
21:44:13 digest ShopPodRestarted         shop     err=0% p95=0.048s replicas=2/2  no user impact, severity 0
21:47:37 digest ShopHighMemory           shop     err=0% p95=0.048s replicas=2/2  no user impact, severity 0
21:50:41 digest BatchWorkerRestarted     batch    err=0% p95=0.048s replicas=1/1  no user impact, severity 0
```

Over both rounds (14 alerts, 8 of them during faults that affected users):

| | pages | pages with no user impact | incidents missed |
|---|---|---|---|
| Page on every alert | 14 | 6 | 0 |
| Page on `severity: critical` only | 6 | 0 | 2 (both slow-response incidents) |
| ops-judge | 8 | 0 | 0 |

Eight incidents is a small sample: zero misses out of eight still allows a true miss rate of up to about 37%. The slow-response alert paged because the LLM wasn't sure and the gate fails closed, not because the model got it right. The lab (`k8s/alert-lab.yaml`) and the scripts that inject faults and score the results (`scripts/alert_lab.py`, `scripts/eval_alerts.py`) are in the repo, so this can be rerun on any cluster with kube-prometheus-stack.

The first version muted any second page for a service within 30 minutes. In the lab that swallowed four separate incidents, because they started a few minutes after the previous one ended. Muting now only applies while the alert that paged is still firing.

## Where it fits

ops-judge is the triage step, not the repair step. It pairs with things like Copilot coding agent or a self-healing agent such as [AIOps-v2](https://github.com/KJLavender/AIOps-v2): triage filters out the failures that only needed a rerun, so the repair agent only spends effort on ones that are actually broken.

```
CI failure → ops-judge
               ├ transient   → rerun
               ├ environment → tell the platform team
               ├ code bug    → repair agent → tests, human review
               └ unsure      → human
```

## Use it on your repository

Copy [`examples/workflows/ops-judge.yml`](examples/workflows/ops-judge.yml) to `.github/workflows/`. When a watched workflow fails, it triages each failed job and acts on the decision:

- transient (flaky test, infrastructure): rerun the failed jobs once
- environment / infrastructure: also post to `OPSJUDGE_NOTIFY_WEBHOOK` (Slack-compatible), or the job summary
- every case: one note on the pull request explaining the call, edited in place if a later triage knows more
- a job it called transient that fails again after the rerun gets a follow-up saying it is probably a real problem
- runs from fork PRs are never rerun automatically

For the LLM tier, add a `CLAUDE_CODE_OAUTH_TOKEN` secret (from `claude setup-token`). Without it only the rule tier runs and unclear cases are handed to a person, which is what you want from a gate that can't see well.

[KJLavender/ops-judge-demo](https://github.com/KJLavender/ops-judge-demo) shows it on two pull requests. In [#1](https://github.com/KJLavender/ops-judge-demo/pull/1) the first CI attempt hit a simulated registry outage; ops-judge called it infrastructure (0.81), reran it, and attempt 2 passed. In [#2](https://github.com/KJLavender/ops-judge-demo/pull/2) a change broke a test; it called it a code bug (0.78), explained why on the PR and did not rerun. When #2 was rerun by hand and failed again, it stayed quiet, because it had never called that one a flake.

`ops-judge doctor` checks the gh login, the claude CLI and the decision-model endpoint.

## Run it

```bash
pip install -e ".[dev]"
python -m pytest                      # 46 tests, no network

ops-judge triage <run-url>            # needs an authenticated gh CLI
ops-judge triage <run-url> --jev http://127.0.0.1:8094/v1/systemone   # add a local decision model
ops-judge triage <run-url> --no-history --no-context                  # faster
ops-judge triage <run-url> --dry-run                                  # show what --act would do
ops-judge doctor
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

## Limits

- The hosted Jev tier hasn't been run; I don't have an API key. Both `https://api.typesafe.ai/v1/systemone` (the default, TypeSafe's documented endpoint) and `jevtypesafeai.com` answer with "API key required"; a `jv_live_…` key belongs to the second one. `ops-judge doctor` tells you which you have. Kev-4B stands in locally.
- 287 CI cases and 14 lab alerts are small samples, and a few repos dominate the CI set.
- The alert lab injects faults with `kubectl` rather than Chaos Mesh, and its rules assume a service exposes `http_requests_total` and `http_request_duration_seconds`.

## Layout

```
judge_core/     judges (rule, Jev/System One, LLM, human queue), policies, cascade, audit store, calibration, evaluation
ops_judge/ci/   log extraction, facts, context facts, GitHub access, actions and their execution
ops_judge/alerts/     Prometheus client, alert facts, routing, watch / webhook service
ops_judge/triage.py   the CI command
action.yml, examples/workflows/   the GitHub Action
k8s/alert-lab.yaml    the alert lab
policies/       YAML policies
scripts/        data collection, evaluation, experiments, site build
site/, docs/    case explorer template and the built page
plans/          plans and notes (Traditional Chinese)
```
