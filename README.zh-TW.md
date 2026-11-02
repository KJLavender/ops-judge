# ops-judge

[English](README.md) | **繁體中文**

CI 的 job 紅了，總得有人去搞清楚：是這次的修改弄壞的，還是偶發的、重跑就會好？ops-judge 負責做這個判斷：該重跑、該找人看，還是該交給修程式的工具。它自己不改程式，也不開 PR。

它會先試便宜的方法。regex 就看得出磁碟滿了；在自己電腦上跑的小模型，常常就看得出是 npm registry 掛了。只有看不清楚的才交給 LLM，LLM 也沒把握的再交給人。

287 筆評估案例都可以在這裡逐筆看：https://kjlavender.github.io/ops-judge/

## 實際長什麼樣子

指定任何一個公開的 GitHub Actions run 就能分析。下面這個 run 第一次失敗、重跑後通過，ops-judge 只看得到第一次的資料。

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

LLM 沒把握（最高只有 0.30），所以它沒有硬猜，而是交給人。實際上重跑就好了。門檻設低一點就能接住這一筆，至於門檻要怎麼設，正是下面評估主要在處理的問題。

## 怎麼判斷

```
規則（regex）→ decision model（本機的 Kev-4B 或 Jev）→ LLM → 人工佇列
```

每一層回傳的都是每個答案的機率，不只是一個標籤。門檻寫在 YAML（`policies/ci.yaml`）：

- `flaky_test` 要 0.85 才採用，因為它會觸發自動重跑
- `unknown` 一律往上一層送
- fork PR 永遠不自動重跑，因為任何人都能在 fork 的 log 裡塞文字

每個決策都會寫進 SQLite 稽核紀錄，包含每一層的答案、花了多久、花了多少錢。

模型不會直接看三千行的原始 log。前處理會做三件事：

- 在失敗的步驟截斷 log，後面都是 cleanup 雜訊
- 保留 GitHub 的 `##[error]` 標註
- 先把事實算好：錯誤堆疊有沒有碰到這個 PR 改的檔案、這個 job 前兩週在 `main` 上失敗過幾次、在那邊重跑有沒有救回來

小模型不太會數數、也不太會比較日期，所以直接給它這些問題的答案，而不是原始資料。

## 有用嗎？

我拿 GitHub 自己的重跑紀錄當答案：一個 job 失敗後，有人按了重跑，如果過了，就代表重跑可以解決；如果又失敗，就代表不行。這樣從 14 個專案收到 447 個失敗的 job，去重後剩 287 題，專案包括 airflow、deno、sveltejs/kit、vue、cpython、ruff、sentry-python、k6、pydantic、electron/forge、superset、TypeScript、transformers、langchain。判斷時只看得到重跑之前就存在的資料。

| | 準確率 |
|---|---|
| 一律回答「重跑」 | 80.5% |
| LLM，直接用它給的機率 | 63.1% |
| LLM，校準後 | 84.3% |
| Kev-4B（在自己電腦上跑），校準後 | 80.8% |
| LLM 校準後，把握 ≥ 90% 才回答 | 處理 36% 的失敗、答對 91.3%，其餘交給人 |

有三件事出乎我意料：

- **直接用 LLM 給的機率，比一律回答「重跑」還差。** 用已知結果去修正它的把握程度，再拿修正時沒看過的題目檢查，這是效果最大的一步：63% 變 84%。
- **多給線索沒有幫助。** 加上失敗測試自己的歷史、其他分支有沒有同樣錯誤、GitHub 當時有沒有故障、錯誤是不是落在修改的程式行，AUROC 只從 0.729 變 0.736。把問題拆成兩個是非題，兩個模型反而都變差。
- **值不值得跑，要看人的時間多貴。** 如果查一次失敗要 15 分鐘，「一律重跑」已經很接近最佳，模型只省約 3%；查一次 5 分鐘時省約 9%。在失敗原因比較雜的專案（airflow、sveltejs/kit、ruff、sentry-python）有幫助；幾乎都是偶發失敗的專案加分有限。

第一版的答案其實標錯了。我原本看「整個 run 重跑後有沒有過」，但一個 run 可能因為某個 job 一直紅，而另一個 job 其實重跑就好。改成看每個 job 自己最後一次的結果之後，修正了 23 題。完整的過程，包括沒成功的嘗試，都寫在 [`plans/01-ops-judge-plan.md`](plans/01-ops-judge-plan.md) 第 11 節。

## 告警

同一套判斷流程也用來決定告警要不要叫醒人。有兩種用法：

- `ops-judge alerts watch`：直接輪詢 Prometheus，不需要 Alertmanager。
- `ops-judge alerts serve`：接收 Alertmanager 的 webhook。

每來一個新告警，它會先把事實算好：服務的請求錯誤率和 p95 延遲、可用的副本數、還有哪些告警正在響、距離上次部署多久。接著決定去向：

- 立刻叫人
- 放進摘要
- 靜音：同一個服務已經有叫過人、而且還在響的告警時

如果沒有任何模型能做出判斷，它還是會叫人。

我在家裡的 k3s 叢集上測試：建了一個小的「商店」服務、模擬流量和七條告警規則，注入八種故障，各跑兩輪。下面是第二輪的即時輸出：

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

兩輪合計 14 個告警，其中 8 個發生在使用者真的受影響的故障期間：

| | 叫人次數 | 使用者沒受影響卻叫人 | 漏掉的事故 |
|---|---|---|---|
| 每個告警都叫人 | 14 | 6 | 0 |
| 只有 `severity: critical` 才叫 | 6 | 0 | 2（兩次都是回應變慢） |
| ops-judge | 8 | 0 | 0 |

8 個事故是很小的樣本：8 次都沒漏，真實的漏叫率仍可能高到約 37%。回應變慢那個告警之所以有叫人，是因為 LLM 沒把握、而關卡在不確定時預設叫人，不是模型判斷對了。

測試環境（`k8s/alert-lab.yaml`）、注入故障和計分的腳本（`scripts/alert_lab.py`、`scripts/eval_alerts.py`）都在 repo 裡，任何裝了 kube-prometheus-stack 的叢集都能重跑。

第一版的規則是：同一個服務 30 分鐘內，第二次以後都靜音。在測試環境裡，這把四個不同的事故都吞掉了，因為它們都在上一個結束後幾分鐘就開始。現在改成只有在叫過人的那個告警還在響時才靜音。

## 在整個流程中的位置

ops-judge 負責分診，不負責修。它可以接在 Copilot coding agent，或 [AIOps-v2](https://github.com/KJLavender/AIOps-v2) 這類自動修復 agent 的前面：分診先過濾掉只需要重跑的失敗，修復 agent 只要處理真的壞掉的。

```
CI 失敗 → ops-judge
           ├ 偶發     → 重跑
           ├ 環境問題 → 通知平台團隊
           ├ 程式錯誤 → 修復 agent → 測試、人工審查
           └ 不確定   → 交給人
```

## 用在你的 repo

把 [`examples/workflows/ops-judge.yml`](examples/workflows/ops-judge.yml) 複製到 `.github/workflows/`。被監看的 workflow 失敗時，它會分析每個失敗的 job，然後照判斷結果動手：

- 偶發（flaky test、基礎設施）：把失敗的 job 重跑一次
- 環境／基礎設施問題：另外發到 `OPSJUDGE_NOTIFY_WEBHOOK`（Slack 相容），沒設定就寫進 job summary
- 每一種情況：在 PR 上留一則說明；之後如果有更完整的判斷，會直接改寫那一則
- 判成偶發、重跑後卻又失敗的，會補一則留言，說這大概是真的問題
- 來自 fork 的 PR 永遠不自動重跑

要用 LLM 那一層，請加一個 `CLAUDE_CODE_OAUTH_TOKEN` secret（用 `claude setup-token` 產生）。沒有的話只會跑規則那一層，看不清楚的就交給人。對一個看不清楚的關卡來說，這正是該有的行為。

[KJLavender/ops-judge-demo](https://github.com/KJLavender/ops-judge-demo) 用兩個 PR 示範：

- [#1](https://github.com/KJLavender/ops-judge-demo/pull/1)：CI 第一次執行時碰到模擬的 registry 故障。ops-judge 判為基礎設施問題（0.81），重跑後第二次就過了。
- [#2](https://github.com/KJLavender/ops-judge-demo/pull/2)：修改把測試弄壞了。ops-judge 判為程式錯誤（0.78），在 PR 上說明原因，沒有重跑。之後有人手動重跑、又失敗了，它也沒有多留言，因為它從來沒說過這是偶發。

`ops-judge doctor` 會檢查 gh 是否登入、claude CLI 在不在，以及 decision model 連不連得上。

## 怎麼跑

```bash
pip install -e ".[dev]"
python -m pytest                      # 46 個測試，不連網

ops-judge triage <run 網址>            # 需要已登入的 gh CLI
ops-judge triage <run 網址> --jev http://127.0.0.1:8094/v1/systemone   # 加上本機的 decision model
ops-judge triage <run 網址> --no-history --no-context                  # 比較快
ops-judge triage <run 網址> --dry-run                                  # 只顯示 --act 會做什麼
ops-judge doctor
```

LLM 這一層呼叫本機的 `claude` CLI，走 Claude 訂閱，不需要 API key。

decision model 這一層用的是 System One API，TypeSafe 雲端的 Jev 和 llama.cpp 的 `llama-server`（`/v1/systemone`）都支援。Kev-4B、Laya、Julia-1 就是用 `llama-server` 在本機跑的。

| 環境變數 | 預設 | |
|---|---|---|
| `TYPESAFE_API_KEY` | | 雲端 Jev 用；本機 llama-server 不需要 |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1/systemone` | |
| `JEV_TIMEOUT` | `10` | 秒；只用 CPU 跑本機模型時要調大 |
| `OPSJUDGE_LLM_MODEL` | `claude-sonnet-5-5` | |
| `OPSJUDGE_LLM_EFFORT` | `medium` | |

重現評估：

```bash
python scripts/collect_reruns.py apache/airflow denoland/deno ... --pages 30
python scripts/collect_context.py && python scripts/collect_history.py && python scripts/collect_context.py
python scripts/build_dataset.py data/reruns.jsonl
python scripts/eval_reruns.py --judges llm,kev@http://127.0.0.1:8094/v1/systemone
python scripts/experiments.py
python scripts/build_site.py
```

## 限制

- 雲端的 Jev 還沒跑過，因為我沒有 API key。目前用本機的 Kev-4B 代替。
  - 有兩個網址都會回應「需要 API key」：`https://api.typesafe.ai/v1/systemone` 是預設值，也是 TypeSafe 文件上的網址；另一個是 `jevtypesafeai.com`。
  - `jv_live_…` 開頭的 key 屬於第二個。
  - 不確定手上的 key 是哪一個，就跑 `ops-judge doctor`。
- 287 題 CI 和 14 個實驗告警都是小樣本，而且 CI 資料大部分來自少數幾個專案。
- 告警實驗用 `kubectl` 注入故障，沒有用 Chaos Mesh。告警規則也假設服務有提供 `http_requests_total` 和 `http_request_duration_seconds`。

## 目錄

```
judge_core/     各層判斷器（規則、Jev/System One、LLM、人工佇列）、policy、串接、稽核紀錄、校準、評估
ops_judge/ci/   log 擷取、事實、額外線索、GitHub 存取、動作和執行
ops_judge/alerts/     Prometheus 客戶端、告警事實、分流、監看／webhook 服務
ops_judge/triage.py   CI 指令
action.yml, examples/workflows/   GitHub Action
k8s/alert-lab.yaml    告警實驗環境
policies/       YAML policy
scripts/        資料收集、評估、實驗、網頁產生
site/, docs/    案例瀏覽頁的模板和產生出來的網頁
plans/          計畫與筆記
```
