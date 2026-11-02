# ops-judge

[English](README.md) | **繁體中文**

CI 的 job 紅了，總得有人去搞清楚：是這次的修改弄壞的，還是偶發的、重跑就會好？ops-judge 負責做這個判斷：該重跑、該找人看，還是該交給修程式的工具。它自己不改程式，也不開 PR。

它會先試便宜的方法。regex 就看得出磁碟滿了；在自己電腦上跑的小模型，常常就看得出是 npm registry 掛了。只有看不清楚的才交給 LLM，LLM 也沒把握的再交給人。

180 筆評估案例都可以在這裡逐筆看：https://kjlavender.github.io/ops-judge/

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

我拿 GitHub 自己的重跑紀錄當答案：一個 job 失敗後，有人按了重跑，如果過了，就代表重跑可以解決；如果又失敗，就代表不行。這樣從 8 個專案收到 316 個失敗的 job，去重後剩 180 題，專案包括 airflow、deno、sveltejs/kit、vue、cpython、ruff、TypeScript、transformers。判斷時只看得到重跑之前就存在的資料。

| | 準確率 |
|---|---|
| 一律回答「重跑」 | 77.2% |
| LLM，直接用它給的機率 | 67.2% |
| LLM，校準後 | 82.8% |
| Kev-4B（在自己電腦上跑），校準後 | 80.6% |
| LLM 校準後，把握 ≥ 85% 才回答 | 處理 48% 的失敗、答對 89.5%，其餘交給人 |

有三件事出乎我意料：

- **直接用 LLM 給的機率，比一律回答「重跑」還差。** 用已知結果去修正它的把握程度，再拿修正時沒看過的題目檢查，這是效果最大的一步：67% 變 83%。
- **多給線索沒有幫助。** 加上失敗測試自己的歷史、其他分支有沒有同樣錯誤、GitHub 當時有沒有故障、錯誤是不是落在修改的程式行，AUROC 只從 0.722 變 0.733。把問題拆成兩個是非題，兩個模型反而都變差。
- **值不值得跑，要看人的時間多貴。** 如果查一次失敗要 15 分鐘，「一律重跑」已經很接近最佳，模型只省約 4%；查一次 5 分鐘時省約 12%。在失敗原因比較雜的專案（airflow、sveltejs/kit、ruff）有幫助；幾乎都是偶發失敗的專案（deno、vue）就沒有加分。

第一版的答案其實標錯了。我原本看「整個 run 重跑後有沒有過」，但一個 run 可能因為某個 job 一直紅，而另一個 job 其實重跑就好。改成看每個 job 自己最後一次的結果之後，修正了 20 題。完整的過程，包括沒成功的嘗試，都寫在 [`plans/01-ops-judge-plan.md`](plans/01-ops-judge-plan.md) 第 11 節。

## 在整個流程中的位置

ops-judge 負責分診，不負責修。它可以接在 Copilot coding agent，或 [AIOps-v2](https://github.com/KJLavender/AIOps-v2) 這類自動修復 agent 的前面：分診先過濾掉只需要重跑的失敗，修復 agent 只要處理真的壞掉的。

```
CI 失敗 → ops-judge
           ├ 偶發     → 重跑
           ├ 環境問題 → 通知平台團隊
           ├ 程式錯誤 → 修復 agent → 測試、人工審查
           └ 不確定   → 交給人
```

## 怎麼跑

```bash
pip install -e ".[dev]"
python -m pytest                      # 46 個測試，不連網

ops-judge triage <run 網址>            # 需要已登入的 gh CLI
ops-judge triage <run 網址> --jev http://127.0.0.1:8094/v1/systemone   # 加上本機的 decision model
ops-judge triage <run 網址> --no-history --no-context                  # 比較快
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

## 還沒做的

- 重跑、PR 留言、通知這些動作已經會決定，但還沒有真的對 GitHub 執行。下一步是做成 GitHub Action。
- 雲端的 Jev 還沒測過，因為還沒有 API key。目前用本機的 Kev-4B 代替。
- 原本計畫裡的告警降噪那一半（Alertmanager、Kubernetes、Chaos Mesh）還沒開始。
- 180 題不算多，而且少數幾個專案佔了大部分。

## 目錄

```
judge_core/     各層判斷器（規則、Jev/System One、LLM、人工佇列）、policy、串接、稽核紀錄、校準、評估
ops_judge/ci/   log 擷取、事實、額外線索、GitHub 存取、動作
ops_judge/triage.py   CLI
policies/       YAML policy
scripts/        資料收集、評估、實驗、網頁產生
site/, docs/    案例瀏覽頁的模板和產生出來的網頁
plans/          計畫與筆記
```
