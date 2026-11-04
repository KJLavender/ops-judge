# ops-judge

**English** | [繁體中文](README.zh-TW.md)

When a CI job (an automated build or test run, here on GitHub Actions) fails, someone has to work out whether their change broke it, or whether it was a flake (a random failure) that a rerun would fix. ops-judge makes that call: rerun it, send it to a person, or pass it on to whatever fixes code. It does not edit code or open pull requests itself.

It tries the cheap things first. A regex can tell you the disk is full. A small model on your own machine can often tell you the npm registry was down. Only the unclear cases go to an LLM, and the ones the LLM isn't sure about go to a person.

You can browse all 287 evaluated failures here: https://kjlavender.github.io/ops-judge/

## What it looks like

Point it at any public GitHub Actions run. This run failed on the first attempt and passed on the second. ops-judge only sees the first attempt.

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

The LLM wasn't sure (its top answer scored 0.30), so it handed the case to a person instead of guessing. The rerun did fix it. A lower threshold would have caught this one. How to pick that threshold is most of what the evaluation below is about.

## How it decides

```
rule (regex) → decision model (Kev-4B locally, or Jev) → LLM → human queue
```

A decision model is a small model that answers pick-one questions and returns a probability for each option. Jev is TypeSafe's hosted one. Kev-4B is an open one you can run yourself. Each tier returns a probability for every answer, not just a label. Thresholds live in YAML (`policies/ci.yaml`):

- `flaky_test` needs 0.85, because it triggers an automatic rerun.
- `unknown` always goes up a tier.
- Runs from fork pull requests are never rerun automatically, because anyone can put text in a fork's logs.

Every decision is written to a SQLite audit log with each tier's answer, the latency and the cost.

The model never sees a raw 3,000-line log. Preprocessing cuts the log at the failed step (what comes after is cleanup noise) and keeps GitHub's `##[error]` annotation. It also works out some facts in advance: whether the stack trace touches files the pull request changed, how often this job failed on `main` in the previous two weeks, and whether a rerun fixed it there. A small model can't reliably count or compare dates, so it gets the answers instead of the raw data.

## Does it work?

I used GitHub's own rerun history as the answer key. If a job failed and then passed when someone clicked "re-run", a rerun would have fixed it. If it failed again, it wouldn't. That gave 447 failed jobs from 14 repos (airflow, deno, sveltejs/kit, vue, cpython, ruff, sentry-python, k6, pydantic, electron/forge, superset, TypeScript, transformers, langchain), and 287 after removing duplicates. The judge only sees what existed before the rerun.

"Calibrated" means the model's stated confidence was adjusted to match how often it was right, fitted on known outcomes and checked on cases the fit had not seen.

| | accuracy |
|---|---|
| Always say "rerun" | 80.5% |
| LLM, raw probabilities | 63.1% |
| LLM, calibrated | 84.3% |
| Kev-4B (runs on your own machine), calibrated | 80.8% |
| LLM, calibrated, answering only when 90% sure or more | 91.3% on 36% of failures; the rest go to a person |

Three things surprised me:

- The raw LLM is worse than always saying "rerun". Calibration was the single biggest improvement: 63% to 84%.
- More context didn't help. I added the failing test's own history, the same error on other branches, GitHub incidents, and whether the error sits in changed lines. AUROC (how well the model ranks cases that a rerun fixes above ones it doesn't; 0.5 is chance) moved from 0.729 to 0.736. Splitting the question into two yes/no questions made both models worse.
- Whether it is worth running depends on what a person's time costs. If looking into a failure takes 15 minutes, "always rerun" is already close to the best and the model saves about 3%. At 5 minutes it saves about 9%. It helps on repos where failures have mixed causes (airflow, sveltejs/kit, ruff, sentry-python) and adds little where nearly everything is a flake.

The first version of my labels was wrong. I labelled by whether the whole run passed on rerun, but a run can stay red because of one job while another job passed. Labelling each job by its own last attempt fixed 23 cases.

## Alerts

The same cascade decides whether an alert should wake someone up. There are two ways to run it:

- `ops-judge alerts watch` polls Prometheus (a metrics and alerting system), with no Alertmanager needed.
- `ops-judge alerts serve` receives webhooks from Alertmanager (Prometheus's alert router).

For each new alert it works out the facts first: the service's request error ratio and p95 latency (the time under which 95% of requests finish), available replicas, which other alerts are firing, and how recently it was deployed. Then it routes the alert. It pages now, puts it in the digest, or mutes it because an alert on the same service already paged and is still firing. If no model can decide, it pages anyway. Set `OPSJUDGE_PAGE_WEBHOOK` to a Slack-compatible URL to get each page posted there.

I tested it on a k3s cluster (small Kubernetes) at home. It ran a small "shop" service with synthetic traffic and seven alert rules, and I injected eight kinds of faults, twice. This is the live output from the second round:

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

Over both rounds there were 14 alerts, 8 of them during faults that affected users:

| | pages | pages with no user impact | incidents missed |
|---|---|---|---|
| Page on every alert | 14 | 6 | 0 |
| Page on `severity: critical` only | 6 | 0 | 2 (both slow-response incidents) |
| ops-judge | 8 | 0 | 0 |

Eight incidents is a small sample. Zero misses out of eight still allows a true miss rate of up to about 37%. The slow-response alert paged because the LLM wasn't sure and the gate fails closed, not because the model got it right.

The lab (`k8s/alert-lab.yaml`) and the scripts that inject faults and score the results (`scripts/alert_lab.py`, `scripts/eval_alerts.py`) are in the repo. You can rerun the test on any cluster that has kube-prometheus-stack.

The first version muted any second page for a service within 30 minutes. In the lab that swallowed four separate incidents, because each started a few minutes after the previous one ended. Muting now applies only while the alert that paged is still firing.

## Where it fits

ops-judge is the triage step, not the repair step. Put it in front of something that fixes code, such as Copilot coding agent or a self-healing agent like [AIOps-v2](https://github.com/KJLavender/AIOps-v2). Triage filters out the failures that only needed a rerun, so the repair agent only works on ones that are really broken.

```
CI failure → ops-judge
               ├ transient   → rerun
               ├ environment → tell the platform team
               ├ code bug    → repair agent → tests, human review
               └ unsure      → human
```

## Use it on your repository

Copy [`examples/workflows/ops-judge.yml`](examples/workflows/ops-judge.yml) to `.github/workflows/`. When a watched workflow fails, it triages each failed job and acts on the decision:

- Transient (flaky test, infrastructure): rerun the failed jobs once.
- Environment or infrastructure: also post to `OPSJUDGE_NOTIFY_WEBHOOK` (Slack-compatible), or to the job summary if that is not set.
- Every case: one note on the pull request explaining the call, edited in place if a later triage knows more.
- A job it called transient that fails again after the rerun gets a follow-up saying it is probably a real problem.
- Runs from fork pull requests are never rerun automatically.

For the LLM tier, add a `CLAUDE_CODE_OAUTH_TOKEN` secret (create it with `claude setup-token`). Without it only the rule tier runs, and unclear cases are handed to a person, which is what you want from a gate that can't see well.

[KJLavender/ops-judge-demo](https://github.com/KJLavender/ops-judge-demo) shows it on two pull requests:

- In [#1](https://github.com/KJLavender/ops-judge-demo/pull/1), the first CI attempt hit a simulated registry outage. ops-judge called it infrastructure (0.81), reran it, and attempt 2 passed.
- In [#2](https://github.com/KJLavender/ops-judge-demo/pull/2), a change broke a test. It called it a code bug (0.78), explained why on the PR, and did not rerun. When #2 was rerun by hand and failed again, it stayed quiet, because it had never called that one a flake.

`ops-judge doctor` checks the gh login, the `claude` command and the decision-model endpoint.

## Run it

You need Python 3.11+.

```bash
pip install -e ".[dev]"
python -m pytest                      # 59 tests, no network

ops-judge triage <run-url>            # needs an authenticated gh CLI
ops-judge triage <run-url> --jev http://127.0.0.1:8094/v1/systemone   # add a local decision model
ops-judge triage <run-url> --no-history --no-context                  # faster
ops-judge triage <run-url> --dry-run                                  # show what --act would do
ops-judge triage <run-url> --act                                      # really rerun / comment / notify
ops-judge doctor
```

The LLM tier calls the local `claude` command, so it runs on a Claude subscription with no API key. The decision-model tier speaks the System One API. TypeSafe's hosted Jev and llama.cpp's `llama-server` (`/v1/systemone`) both implement it, and Kev-4B, Laya and Julia-1 run locally through `llama-server`.

| Variable | Default | |
|---|---|---|
| `TYPESAFE_API_KEY` | | hosted Jev; not needed for a local llama-server |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1/systemone` | |
| `JEV_TIMEOUT` | `10` | seconds; raise it for CPU-only local models |
| `OPSJUDGE_LLM_MODEL` | `claude-sonnet-5-5` | |
| `OPSJUDGE_LLM_EFFORT` | `medium` | |
| `OPSJUDGE_NOTIFY_WEBHOOK` | | Slack-compatible URL for CI notifications |
| `OPSJUDGE_PAGE_WEBHOOK` | | Slack-compatible URL for alert pages |

To reproduce the evaluation (`data/` is git-ignored, so these scripts create it):

```bash
python scripts/collect_reruns.py apache/airflow denoland/deno ... --pages 30   # list the repos you want
python scripts/collect_context.py && python scripts/collect_history.py && python scripts/collect_context.py
python scripts/build_dataset.py data/reruns.jsonl
python scripts/eval_reruns.py --judges llm,kev@http://127.0.0.1:8094/v1/systemone
python scripts/experiments.py
python scripts/build_site.py
```

## Limits

- I haven't run the hosted Jev tier, because I don't have an API key. Both `https://api.typesafe.ai/v1/systemone` (the default, and the endpoint in TypeSafe's documentation) and `jevtypesafeai.com` answer with "API key required". A `jv_live_…` key belongs to the second one. `ops-judge doctor` tells you which you have. Kev-4B stands in locally.
- 287 CI cases and 14 lab alerts are small samples, and a few repos dominate the CI set.
- The alert lab injects faults with `kubectl` rather than Chaos Mesh, and its rules assume a service exposes `http_requests_total` and `http_request_duration_seconds`.

## Layout

```
judge_core/           judges (rule, Jev/System One, LLM, human queue), policies, cascade, audit store, calibration, evaluation
ops_judge/ci/         log extraction, facts, context facts, GitHub access, actions and their execution
ops_judge/alerts/     Prometheus client, alert facts, routing, watch / webhook service
ops_judge/triage.py   the CI command
action.yml, examples/workflows/   the GitHub Action
k8s/alert-lab.yaml    the alert lab
policies/             YAML policies
scripts/              data collection, evaluation, experiments, site build
site/, docs/          case explorer template and the built page
```
