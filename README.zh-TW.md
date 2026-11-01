# ops-judge

[English](README.md) | **繁體中文**

給維運用的判斷層，信心經過校準、每次決策都可稽核。用在兩件事：**CI 失敗分類**和**告警降噪**。

多數 CI／告警 triage bot 會把每個事件都丟給 LLM。ops-judge 先用便宜、毫秒級的判斷器處理，只有灰色地帶才往上送：

```
RuleJudge（regex）─▶ JevJudge（System One 模型）─▶ LLMJudge ─▶ HumanQueue
```

- 每一層只把**還沒決定的問題**合成**一次請求**送出。
- 比對閾值前，先依（policy, 後端）**校準**信心。
- 每個決策都寫進 **SQLite 稽核紀錄**：輸入雜湊、各層答案、延遲、成本、走了哪條路。
- 人工標註會回收成校準和訓練資料。

## 進度

| 部分 | 狀態 |
|---|---|
| `judge_core`：四個後端、YAML policy、串接、稽核紀錄、人工佇列 | 完成 |
| Demo：案例瀏覽頁（`docs/index.html`）與 `ops-judge triage <run 網址>` | 完成 |
| 校準（isotonic／Platt）與評估（閾值掃描、ECE、含錯誤相關性的 compare、learning curve） | 完成 |
| CI 前處理：清理 log、擷取錯誤段落、堆疊與修改檔案交集、基礎設施／環境訊號、Drain3 模板 | 完成 |
| CI 動作：重跑／留言／通知；fork PR 不自動重跑；重跑仍失敗時 flaky 改判 code_bug | 完成（只有邏輯，尚未接上 GitHub） |
| 重跑標註收集＋真實資料評估（`collect_reruns.py`、`build_dataset.py`、`eval_reruns.py`） | 4 個 repo 共 220 筆失敗；LLM 加上 main 歷史 AUROC 0.74、本機 Kev-4B 0.69（見 `plans/01-ops-judge-plan.md` 第 11 節） |
| Jev 實測 | 等 API key（`TYPESAFE_API_KEY`） |
| 告警流程（Alertmanager webhook、K8s + Chaos Mesh） | 規劃中（見 `plans/01-ops-judge-plan.md`） |

## Demo

- **案例瀏覽頁**：**https://kjlavender.github.io/ops-judge/**（`docs/index.html`，不需要後端）：103 筆真實失敗，每一筆都能看到判斷器看到的事實、每個模型的機率、串接路徑，以及實際結果。用 `python scripts/build_site.py` 重新產生。
- **現場分析**任何公開 repo 的 GitHub Actions run：

```bash
ops-judge triage https://github.com/sveltejs/kit/actions/runs/40762220138
ops-judge triage <run 網址> --jev http://127.0.0.1:8094/v1/systemone   # 加上本機 decision model 這一層
ops-judge triage <run 網址> --no-history --json                         # 較快／輸出 JSON
```

會印出擷取的事實、每一層的答案（採用或往上送）、決策、預定動作；如果那個 run 後來有重跑，也會揭曉實際結果。

## 快速開始

```bash
pip install -e ".[dev]"
python -m pytest                       # 38 個測試，不連網
python scripts/toy_eval.py             # 20 筆 CI 玩具資料 → examples/out/{sweep,calibration}.png、compare.json、audit.sqlite
python scripts/collect_reruns.py owner/repo --pages 3   # 從 GitHub Actions 的重跑紀錄取得 flaky 標註（需要 gh 登入）
```

## 後端與設定

| 後端 | 說明 | 環境變數 |
|---|---|---|
| `RuleJudge` | policy YAML 裡的 regex 規則，也可以另外註冊 Python 規則 | — |
| `JevJudge` | `POST /v1/systemone`；非 ASCII 字元以 UTF-8 原文送出，不轉成 `\u` 跳脫 | `TYPESAFE_API_KEY`、`JEV_MODEL`（預設 `jev-latest`）、`JEV_BASE_URL` |
| `LLMJudge` | 本機 `claude -p` CLI（走訂閱，不需 API key）；機率分布由模型自己回報，所以和其他後端一樣要校準 | `OPSJUDGE_LLM_MODEL`（預設 `claude-sonnet-5-5`）、`OPSJUDGE_LLM_EFFORT`（預設 `medium`） |
| `HumanQueue` | SQLite 待審佇列；已經有標註的輸入直接回答 | — |

## Policy

見 `policies/ci.yaml` 和 `policies/alerts.yaml`。判斷一直沒有結果時，各角色的處理方式：

| 角色 | 灰色地帶 | 所有層都失敗／仍不確定 |
|---|---|---|
| routing | 往上送，最後進人工佇列 | `unrouted`／進佇列 |
| gating | 往上送；灰色答案絕不放行 | 採 `fail_decision`（例如照常叫人） |
| ranking | 不設閾值，直接採用分數 | `unscored` |
| extraction | 依用途設閾值 | `unprocessed` |

## 目錄結構

```
judge_core/   型別、policy、串接、store（稽核＋佇列）、校準、評估、judges/
ops_judge/ci/ extract（log → 段落＋訊號）、facts（算好的 state）、templates（Drain3）、actions
policies/     YAML policy
scripts/      toy_eval.py、collect_reruns.py
examples/     toy_ci.jsonl
plans/        專案計畫
```
