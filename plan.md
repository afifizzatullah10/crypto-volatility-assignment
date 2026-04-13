# Crypto Volatility Detector — Project Plan

**Course:** 45-886 | **Student:** Individual Assignment  
**Stack:** Python 3.10+, Kafka (KRaft), MLflow, Evidently, Docker Compose  
**Prediction Goal:** Classify whether 60-second forward volatility (rolling std of midprice returns) exceeds threshold τ

---

## Repository Layout (set this up first)

```
crypto-volatility/
├── data/
│   ├── raw/                  # NDJSON ticks from WebSocket
│   └── processed/            # Parquet feature files
├── docker/
│   ├── compose.yaml
│   └── Dockerfile.ingestor
├── docs/
│   ├── scoping_brief.pdf
│   ├── feature_spec.md
│   ├── model_card_v1.md
│   └── genai_appendix.md
├── features/
│   └── featurizer.py
├── handoff/                  # Team handoff folder
├── mlruns/                   # MLflow artifact store
├── models/
│   ├── train.py
│   ├── infer.py
│   └── artifacts/
├── notebooks/
│   └── eda.ipynb
├── reports/
│   ├── evidently/
│   └── model_eval.pdf
├── scripts/
│   ├── ws_ingest.py
│   ├── kafka_consume_check.py
│   └── replay.py
├── config.yaml
├── requirements.txt
├── .env                      # Never commit — add to .gitignore
├── .env.example
└── README.md
```

**First action:** `git init`, create `.gitignore` with `.env`, `mlruns/`, `data/`, `__pycache__/`.

---

## Milestone 1 — Streaming Setup & Scoping

**Due:** Week 1 | **Goal:** Get Kafka + MLflow running, ingest live ticks, define the ML problem.

---

### Files to Create

| File | Purpose |
|---|---|
| `docker/compose.yaml` | Spin up Kafka (KRaft) + MLflow together |
| `docker/Dockerfile.ingestor` | Containerize the WebSocket ingestor |
| `scripts/ws_ingest.py` | Connect to Coinbase WS, publish to Kafka |
| `scripts/kafka_consume_check.py` | Validate messages are arriving in topic |
| `docs/scoping_brief.pdf` | One-page problem statement |
| `config.yaml` | Pair names, Kafka broker address, topic names |
| `.env.example` | Template showing which env vars are needed |
| `requirements.txt` | Pin all Python deps |

---

### Step-by-Step Logic

#### Step 1 — Docker Compose (`docker/compose.yaml`)

```yaml
services:
  kafka:
    image: apache/kafka:3.7.0          # KRaft mode, no Zookeeper
    container_name: kafka
    ports:
      - "9092:9092"
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://localhost:9092
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"

  mlflow:
    image: ghcr.io/mlflow/mlflow:v2.13.0
    container_name: mlflow
    ports:
      - "5000:5000"
    command: >
      mlflow server
      --host 0.0.0.0
      --port 5000
      --backend-store-uri sqlite:///mlflow.db
      --default-artifact-root /mlflow/artifacts
    volumes:
      - ./mlruns:/mlflow/artifacts
```

> **Why KRaft?** No Zookeeper dependency — simpler single-node setup for a course project.

#### Step 2 — WebSocket Ingestor (`scripts/ws_ingest.py`)

The script must:

1. **Parse args:** `--pair BTC-USD`, `--minutes 15`, optional `--output-dir data/raw/`
2. **Load config** from `config.yaml` (Kafka broker, topic name `ticks.raw`)
3. **Connect** to `wss://advanced-trade-api.coinbase.com` — no auth needed for public ticker channel
4. **Subscribe** with this payload on open:
   ```json
   {
     "type": "subscribe",
     "product_ids": ["BTC-USD"],
     "channel": "ticker"
   }
   ```
5. **Heartbeat / reconnect loop:** wrap `websocket.run_forever()` in a `while True` with exponential backoff (2ˢ seconds, cap at 60s). On reconnect, re-send the subscribe payload.
6. **On each message:** parse JSON, add `ingested_at` (UTC ISO timestamp), serialize as NDJSON line.
7. **Publish** the raw JSON string to Kafka topic `ticks.raw` with key = `product_id`.
8. **Optionally mirror** to `data/raw/ticks_YYYYMMDD_HHMMSS.ndjson` (one file per run).
9. **Graceful shutdown** after `--minutes` elapsed; flush Kafka producer before exit.

Key fields in a Coinbase ticker event you'll use later:
- `best_bid`, `best_ask` → midprice, spread
- `price` → last trade price  
- `time` → exchange timestamp

#### Step 3 — Kafka Consumer Validation (`scripts/kafka_consume_check.py`)

1. **Parse args:** `--topic ticks.raw`, `--min 100` (minimum messages expected), `--timeout 60`
2. **Connect** Kafka consumer to group `validator`
3. **Poll** until `--min` messages received or `--timeout` exceeded
4. **Print** message count, first/last timestamp, sample payload
5. **Exit code 0** if threshold met, exit code 1 otherwise (useful for CI checks)

#### Step 4 — Dockerfile.ingestor

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/ws_ingest.py scripts/
COPY config.yaml .
CMD ["python", "scripts/ws_ingest.py", "--pair", "BTC-USD", "--minutes", "60"]
```

Build: `docker build -f docker/Dockerfile.ingestor -t ws-ingestor .`

#### Step 5 — Scoping Brief (`docs/scoping_brief.md` → export to PDF)

Write one page covering:
- **Use case:** Alert traders to imminent volatility spikes in BTC-USD
- **Prediction goal:** At time T, predict whether σ(midprice returns, T to T+60s) ≥ τ
- **Success metric:** PR-AUC ≥ 0.55 (baseline is random ≈ class imbalance rate)
- **Risk assumptions:** Data latency, class imbalance (spikes are rare), no look-ahead bias

---

### Verification Steps

| Check | How to Verify |
|---|---|
| All services up | `docker compose ps` — status should be `running` for `kafka` and `mlflow` |
| MLflow UI accessible | Open `http://localhost:5000` in browser |
| Ingestor produces data | Run `python scripts/ws_ingest.py --pair BTC-USD --minutes 15` then check `data/raw/` |
| Kafka has messages | `python scripts/kafka_consume_check.py --topic ticks.raw --min 100` exits with code 0 |
| Container builds | `docker build -f docker/Dockerfile.ingestor -t ws-ingestor .` completes with no errors |
| No secrets committed | `git log --all -p \| grep -i "api_key\|secret\|password"` returns nothing |

---

## Milestone 2 — Feature Engineering, EDA & Evidently

**Due:** Week 2 | **Goal:** Build a clean, reproducible feature pipeline; explore data; generate drift report.

---

### Files to Create

| File | Purpose |
|---|---|
| `features/featurizer.py` | Kafka consumer → compute windowed features → publish + save |
| `scripts/replay.py` | Replay saved NDJSON through featurizer for reproducibility |
| `notebooks/eda.ipynb` | Exploratory analysis, threshold selection via percentile plots |
| `data/processed/features.parquet` | Output feature table |
| `docs/feature_spec.md` | Feature definitions, target definition, threshold justification |
| `reports/evidently/drift_report.html` | Evidently report comparing early vs. late data windows |

---

### Step-by-Step Logic

#### Step 1 — Featurizer Design (`features/featurizer.py`)

The featurizer is a **stateful Kafka consumer**. It maintains a rolling buffer of recent ticks and emits a feature row for every new tick (or every N seconds).

**Input:** Kafka topic `ticks.raw`  
**Output:** Kafka topic `ticks.features` + append to `data/processed/features.parquet`

**Features to compute** (using a configurable lookback window, e.g., `W=60s`):

| Feature Name | Formula | Why |
|---|---|---|
| `midprice` | `(best_bid + best_ask) / 2` | Clean price signal |
| `spread` | `best_ask - best_bid` | Market liquidity proxy |
| `spread_pct` | `spread / midprice` | Normalized spread |
| `return_1s` | `log(midprice_t / midprice_{t-1})` | Instantaneous return |
| `rolling_vol_30s` | `std(return_1s, last 30s)` | Short-term realized vol |
| `rolling_vol_60s` | `std(return_1s, last 60s)` | Medium-term realized vol |
| `trade_intensity` | `count(trades, last 30s)` | Activity proxy |
| `bid_ask_imbalance` | `(best_bid_size - best_ask_size) / (best_bid_size + best_ask_size)` | Order pressure (optional) |

**Label construction** (requires forward-looking data — generate at replay/training time, not live):
```
σ_future = std(midprice_returns over [T, T+60s])
label = 1 if σ_future >= τ else 0
```

> **Important:** The label uses *future* data, so it can only be generated in replay mode or training. At inference time, you predict the label for the next 60 seconds.

**Implementation pattern:**
```python
from collections import deque
import time

class TickBuffer:
    def __init__(self, window_seconds=120):
        self.ticks = deque()
        self.window = window_seconds

    def add(self, tick):
        self.ticks.append(tick)
        cutoff = tick['time'] - self.window
        while self.ticks and self.ticks[0]['time'] < cutoff:
            self.ticks.popleft()

    def compute_features(self, tick):
        # ... compute all features from self.ticks
```

#### Step 2 — Replay Script (`scripts/replay.py`)

1. **Args:** `--raw data/raw/*.ndjson`, `--out data/processed/features.parquet`
2. **Load** all NDJSON files, sort by timestamp (critical — files may overlap)
3. **Feed ticks one by one** through the same `TickBuffer` and feature logic as `featurizer.py`
4. **After processing all ticks**, compute forward labels (since we have all future data)
5. **Save** to Parquet with `pandas.to_parquet()`

**Reproducibility contract:** `replay.py` and the Kafka consumer must import the same `compute_features()` function from a shared module (e.g., `features/core.py`). Never duplicate the logic.

#### Step 3 — EDA Notebook (`notebooks/eda.ipynb`)

Structure your notebook with these sections:

1. **Load data** — read `features.parquet`, check shape, dtypes, missing values
2. **Distribution plots** — histograms of `rolling_vol_60s`, spread, return
3. **Percentile analysis** — plot `np.percentile(rolling_vol_future, range(80, 100, 1))` to choose τ
   - Rule of thumb: pick τ at ~90th–95th percentile so ~5–10% of windows are labeled `1`
4. **Time series plot** — rolling vol over time to eyeball regime changes
5. **Class balance** — `df['label'].value_counts()` — document imbalance ratio
6. **Feature correlation** — heatmap to spot redundant features

**Document your threshold choice in `docs/feature_spec.md`:**
```markdown
## Target Definition
- Horizon: 60 seconds
- Volatility proxy: rolling std of midprice log-returns over [T, T+60s]
- Label: 1 if σ_future >= τ; else 0
- Threshold τ: 0.00042  (92nd percentile of training window σ_future)
- Class balance: ~8% positive
```

#### Step 4 — Evidently Report (`reports/evidently/`)

Split your feature data into two halves (e.g., first 50% = reference, last 50% = current):

```python
from evidently.report import Report
from evidently.metric_preset import DataDriftPreset, DataQualityPreset

report = Report(metrics=[DataDriftPreset(), DataQualityPreset()])
report.run(reference_data=df_early, current_data=df_late)
report.save_html("reports/evidently/drift_report.html")
```

The report should include: per-feature drift scores, missing value rates, distribution comparisons.

---

### Verification Steps

| Check | How to Verify |
|---|---|
| Feature replay consistency | Run `replay.py` on saved NDJSON → compare output to live consumer output on same data. Row counts and feature values should match within floating-point tolerance. |
| No look-ahead in live features | Inspect `featurizer.py` — the label column must NOT be emitted to `ticks.features` |
| Parquet file exists and is valid | `import pandas as pd; pd.read_parquet("data/processed/features.parquet").info()` |
| EDA notebook runs end-to-end | `jupyter nbconvert --to notebook --execute notebooks/eda.ipynb` completes with no errors |
| Threshold is documented | `docs/feature_spec.md` exists and contains τ, justification plot reference, class balance |
| Evidently report opens | Open `reports/evidently/drift_report.html` in browser — drift section present |

---

## Milestone 3 — Modeling, Tracking & Evaluation

**Due:** Week 3 | **Goal:** Train baseline + ML model, log to MLflow, evaluate with PR-AUC, write model card.

---

### Files to Create

| File | Purpose |
|---|---|
| `models/train.py` | Train baseline + ML model, log to MLflow |
| `models/infer.py` | Load artifact, score new feature rows |
| `models/artifacts/` | MLflow-saved model files |
| `docs/model_card_v1.md` | Model card per standard template |
| `docs/genai_appendix.md` | Log of all GenAI tool usage |
| `reports/model_eval.pdf` | Evaluation report with PR curves |
| `reports/evidently/test_vs_train_report.html` | Fresh Evidently report: test vs. train distribution |
| `handoff/` | All files required for team handoff |

---

### Step-by-Step Logic

#### Step 1 — Data Splits (`models/train.py`)

**Always use time-based splits — never random shuffle** (would cause data leakage).

```python
df = pd.read_parquet("data/processed/features.parquet").sort_values("time")
n = len(df)
train = df.iloc[:int(n * 0.70)]
val   = df.iloc[int(n * 0.70):int(n * 0.85)]
test  = df.iloc[int(n * 0.85):]
```

Drop rows where the forward label could not be computed (last 60 seconds of data).

#### Step 2 — Baseline Model (z-score rule)

```python
# "Spike incoming if current vol is Z standard deviations above rolling mean"
threshold_z = 2.0  # tune on validation set
df['z_score'] = (df['rolling_vol_60s'] - df['rolling_vol_60s'].rolling(300).mean()) \
                / df['rolling_vol_60s'].rolling(300).std()
df['baseline_pred_prob'] = scipy.stats.norm.cdf(df['z_score'])  # soft score
df['baseline_pred'] = (df['z_score'] > threshold_z).astype(int)
```

Log to MLflow:
```python
import mlflow

with mlflow.start_run(run_name="baseline_zscore"):
    mlflow.log_param("threshold_z", threshold_z)
    mlflow.log_metric("pr_auc_val", pr_auc_val)
    mlflow.log_metric("pr_auc_test", pr_auc_test)
```

#### Step 3 — ML Model (Logistic Regression or XGBoost)

```python
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

feature_cols = ['spread_pct', 'rolling_vol_30s', 'rolling_vol_60s',
                'trade_intensity', 'return_1s', 'bid_ask_imbalance']

pipe = Pipeline([
    ('scaler', StandardScaler()),
    ('clf', LogisticRegression(class_weight='balanced', max_iter=1000))
])
pipe.fit(train[feature_cols], train['label'])
```

Log to MLflow:
```python
with mlflow.start_run(run_name="logistic_regression_v1"):
    mlflow.log_params(pipe.named_steps['clf'].get_params())
    mlflow.log_metric("pr_auc_val", pr_auc_val)
    mlflow.log_metric("pr_auc_test", pr_auc_test)
    mlflow.sklearn.log_model(pipe, "model")
    mlflow.log_artifact("reports/model_eval.pdf")
```

**PR-AUC calculation:**
```python
from sklearn.metrics import average_precision_score
pr_auc = average_precision_score(y_true, y_pred_prob)
```

#### Step 4 — Inference Script (`models/infer.py`)

```
Args: --features data/processed/features_test.parquet
      --run-id <mlflow_run_id>   (load model from MLflow registry)
      --output predictions.csv
```

Logic:
1. Load model via `mlflow.sklearn.load_model(f"runs:/{run_id}/model")`
2. Score rows, output `[timestamp, label_true, label_pred, pred_prob]`
3. Report wall-clock time and assert `total_time < 2 * data_time_span`

#### Step 5 — Evaluation Report

Generate a PDF or markdown report containing:
- **PR curve** plot for both models on the test set
- **PR-AUC table** (baseline vs. ML, train/val/test)
- **Confusion matrix** at optimal F1 threshold
- **Class balance** reminder (important for interpreting metrics)
- **Short narrative:** which model wins and why

#### Step 6 — Model Card (`docs/model_card_v1.md`)

```markdown
# Model Card v1 — BTC-USD Volatility Spike Classifier

## Model Details
- Type: Logistic Regression (sklearn Pipeline with StandardScaler)
- Version: 1.0 | Date: <date>

## Intended Use
- Short-term (60s) volatility spike alerting for BTC-USD
- NOT intended for automated trading execution

## Training Data
- Source: Coinbase Advanced Trade WebSocket (public ticker)
- Period: <start> to <end> | Pairs: BTC-USD
- Label: σ_future >= τ=0.00042 (92nd percentile)

## Evaluation
| Split | PR-AUC |
|---|---|
| Validation | X.XX |
| Test | X.XX |

## Limitations & Risks
- Model trained on one market regime; may degrade in low-vol environments
- Class imbalance (~8% positive) means raw accuracy is misleading
- No position sizing or execution logic included

## Ethical Considerations
- Uses only public market data
- No user data or PII involved
```

#### Step 7 — Refreshed Evidently Report

```python
report = Report(metrics=[DataDriftPreset(), DataQualityPreset()])
report.run(reference_data=train[feature_cols], current_data=test[feature_cols])
report.save_html("reports/evidently/test_vs_train_report.html")
```

#### Step 8 — Team Handoff (`handoff/`)

Copy in:
- `docker/compose.yaml`, `docker/Dockerfile.ingestor`, `.env.example`
- `docs/feature_spec.md`, `docs/model_card_v1.md`
- `models/artifacts/` (MLflow model files)
- `requirements.txt`
- A 10-minute raw slice: `data/raw/slice_10min.ndjson`
- Its features: `data/processed/features_10min.parquet`
- `reports/model_eval.pdf`, `reports/evidently/test_vs_train_report.html`
- `predictions_test.csv`
- `handoff/README.md` — note "Selected-base" OR "Composite" with exact integration steps

---

### Verification Steps

| Check | How to Verify |
|---|---|
| MLflow has ≥ 2 runs | Open `http://localhost:5000` — confirm baseline and ML runs both visible with logged metrics |
| PR-AUC is logged | In MLflow UI, both runs show `pr_auc_test` metric |
| Inference is fast enough | Run `time python models/infer.py --features data/processed/features_test.parquet` — elapsed time < 2× the data window duration |
| Model artifact loadable | `mlflow.sklearn.load_model(f"runs:/{run_id}/model")` works from a fresh Python session |
| Evidently test report | Open `reports/evidently/test_vs_train_report.html` — includes drift section comparing test vs. train |
| Model card complete | `docs/model_card_v1.md` has all sections: details, intended use, training data, evaluation table, limitations |
| GenAI appendix exists | `docs/genai_appendix.md` lists all prompts used, files affected, and verification steps |
| Handoff folder complete | `ls handoff/` matches all required files in the assignment spec |

---

## Milestone 4 — Dashboard

**Due:** Week 4 | **Goal:** Ship a self-contained, neobrutalist HTML dashboard that works in two modes: **static** (reads a pre-exported `dashboard.json` snapshot) and **live** (streams real-time ticks from an SSE server). No framework — plain HTML + CSS + JS + Chart.js.

---

### Design System

The dashboard follows a **neobrutalist** style inspired by "PGH Transit Atlas × CoinMarketCap":

| Token | Value | Usage |
|---|---|---|
| `--paper` | `#F2F0E9` | Page background |
| `--paper-dark` | `#E8E5DA` | Card hover / subtler areas |
| `--ink` | `#1A1A1A` | Text, borders, shadows |
| `--blueprint` | `#2B4CFF` | Primary accent (BTC, prices) |
| `--safety-orange` | `#FF4D00` | Alerts, spike flags, down deltas |
| `--terminal-green` | `#00B884` | Up deltas, live badge, winner border |
| `--muted` | `#6B6B6B` | Labels, subtitles |
| Font | JetBrains Mono (Google Fonts) | All text, monospace throughout |
| Borders | `3px solid var(--ink)` | Hard neobrutalist edges, no radius |
| Shadows | `6px 6px 0px var(--ink)` | Offset box-shadows (no blur) |

**Rules:**
- Zero border-radius everywhere (`--radius: 0px`)
- All caps labels with `letter-spacing: 0.12–0.18em`
- Chart background `#0E0E0E` (dark inset on light page)
- No transitions longer than `0.2s`

---

### Files to Create

| File | Purpose |
|---|---|
| `dashboard/index.html` | Full page structure — all panels, tables, chart canvas |
| `dashboard/style.css` | Complete neobrutalist design system |
| `dashboard/app.js` | Dual-mode JS: static JSON load + live SSE stream |
| `dashboard/data/dashboard.json` | Static snapshot (generated by export script) |
| `scripts/export_dashboard_json.py` | Reads MLflow artifacts + predictions → writes `dashboard.json` |
| `scripts/dashboard_server.py` | SSE server on `:8766` — forwards Kafka `ticks.features` to browser |

---

### Step-by-Step Logic

#### Step 1 — `dashboard/data/dashboard.json` Schema

The static snapshot must contain all data the dashboard needs to render without a backend. Generate it with `scripts/export_dashboard_json.py` after Milestone 3 training is complete.

```json
{
  "generated_at": "2026-04-01T03:25:00Z",
  "feature_rows": 37435,
  "label_rate": 0.082,
  "price_summary": {
    "BTC-USD": { "last": 83210.50, "delta_pct": -1.24 },
    "ETH-USD": { "last": 1583.20, "delta_pct": -2.01 }
  },
  "metrics": {
    "train_rows": 22460,
    "validation_rows": 7485,
    "test_rows": 7490,
    "logistic_regression": {
      "pr_auc": 0.9123,
      "f1_at_threshold": 0.7841,
      "positive_rate": 0.082,
      "threshold": 0.4312
    },
    "baseline": {
      "pr_auc": 0.8260,
      "f1_at_threshold": 0.6530,
      "positive_rate": 0.082
    }
  },
  "chart_series": {
    "BTC-USD": [
      { "window_end_ts": "2026-04-01T02:33:01Z", "midprice": 83100.00,
        "realized_vol_60s": 0.000078, "predicted_spike": 0 }
    ],
    "ETH-USD": []
  },
  "predictions": [
    { "window_end_ts": "...", "product_id": "BTC-USD",
      "label": 1, "predicted_label": 1, "logistic_probability": 0.87,
      "baseline_score": 1.2, "realized_vol_60s": 0.000091 }
  ],
  "recent_spikes": [
    { "window_end_ts": "...", "product_id": "BTC-USD",
      "midprice": 83210.50, "realized_vol_60s": 0.000091,
      "logistic_probability": 0.87 }
  ],
  "probability_outlook": {
    "BTC-USD": {
      "next_minute": { "higher_turbulence": 0.72, "calmer_conditions": 0.28 },
      "next_hour":   { "higher_turbulence": 0.61, "calmer_conditions": 0.39 },
      "next_day":    { "higher_turbulence": 0.48, "calmer_conditions": 0.52 },
      "student_summary": "BTC-USD currently shows a 61% chance of rougher-than-normal trading in the next hour..."
    },
    "ETH-USD": {}
  },
  "price_scenarios": {
    "BTC-USD": {
      "current_price": 83210.50,
      "bias_label": "DOWN BIAS",
      "next_hour": {
        "up_probability": 0.41, "down_probability": 0.59,
        "up_move_usd": 245.00, "down_move_usd": 245.00,
        "up_target": 83455.50, "down_target": 82965.50
      },
      "next_day": {
        "up_probability": 0.41, "down_probability": 0.59,
        "up_move_usd": 1250.00, "down_move_usd": 1250.00,
        "up_target": 84460.50, "down_target": 81960.50
      }
    },
    "ETH-USD": {}
  }
}
```

#### Step 2 — Export Script (`scripts/export_dashboard_json.py`)

```python
"""
Usage: python scripts/export_dashboard_json.py \
       --predictions predictions_test.csv \
       --features data/processed/features.parquet \
       --run-id <mlflow_run_id> \
       --out dashboard/data/dashboard.json
"""
```

Logic:
1. Load `predictions_test.csv` (from `models/infer.py` output)
2. Load `features.parquet` for price/vol time-series
3. Load MLflow run metrics via `mlflow.get_run(run_id)`
4. Load baseline metrics from its MLflow run
5. Identify spike rows (`predicted_label == 1`) for `recent_spikes`
6. Downsample `chart_series` to ≤ 3000 rows per pair (step = `len // 3000`)
7. Compute `probability_outlook` from last 60 rows of `logistic_probability`
8. Compute `price_scenarios` using realized vol and directional momentum
9. Serialize to JSON and write to `--out`

#### Step 3 — Dashboard HTML Structure (`dashboard/index.html`)

The page is divided into these sections, top to bottom:

```
┌─────────────────────────────────────────────────────────────────┐
│  TOP TICKER BAR  (sticky, black, scrolling price ticker)         │
├─────────────────────────────────────────────────────────────────┤
│  SITE HEADER                                                     │
│  ┌─ kicker label ──┐  ┌─── PRICE BOARD (dark card) ───────────┐ │
│  │ // FUNDAMENTAL  │  │ BTC-USD  $83,210.50  -1.24%           │ │
│  │ CRYPTO VOL INTEL│  │ ETH-USD  $1,583.20   -2.01%           │ │
│  │ h1 title        │  │ SESSION info · ⬤ LIVE toggle          │ │
│  │ lede text       │  └───────────────────────────────────────┘ │
│  └─────────────────┘                                             │
├─────────────────────────────────────────────────────────────────┤
│  KPI ROW  (5 cards: FEATURE BARS · LABEL RATE · PR-AUC · F1 ·   │
│            TRAIN/VAL/TEST)                                       │
├─────────────────────────────────────────────────────────────────┤
│  MARKET LIVE ROW  (2 cards: BTC-USD | ETH-USD)                  │
│  Each card: big price, vol pressure line, 2×2 scenario grid     │
│  (NEXT HOUR UP / DOWN, NEXT DAY UP / DOWN with prob + move)     │
├─────────────────────────────────────────────────────────────────┤
│  WEEK 4 API PANEL  (left) │ REPLAY SAMPLE PANEL  (right)        │
├─────────────────────────────────────────────────────────────────┤
│  VOLATILITY TIMELINE CHART (full width)                          │
│  BTC-USD | ETH-USD tabs · dual axis: price left, vol×10⁻⁴ right │
│  orange dots = model spike flags                                 │
├─────────────────────────────────────────────────────────────────┤
│  SCORECARD  (left)       │  SCORECARD  (right, winner border)   │
│  BASELINE · Z-SCORE      │  LOGISTIC REGRESSION  ↑ WINNER       │
│  PR-AUC / F1 / POS RATE  │  PR-AUC / F1 / POS RATE             │
├─────────────────────────────────────────────────────────────────┤
│  DELTA PANEL  (LOGISTIC vs BASELINE — improvement bars)          │
├─────────────────────────────────────────────────────────────────┤
│  SPIKE RADAR  (left)     │  TURBULENCE OUTLOOK  (right)         │
│  orange dot event list   │  next-minute/hour/day percentages    │
├─────────────────────────────────────────────────────────────────┤
│  RECENT PREDICTIONS TABLE  (last 20 test rows)                   │
├─────────────────────────────────────────────────────────────────┤
│  METHODS LIST  (numbered steps 01–06) + artifact links           │
├─────────────────────────────────────────────────────────────────┤
│  FOOTER  (team names, course, data source)                       │
└─────────────────────────────────────────────────────────────────┘
```

Key `id` attributes required by `app.js`:

| Element | ID |
|---|---|
| Top ticker scrolling text | `ticker-scroll` |
| LIVE/STATIC toggle badge | `live-badge` |
| BTC price in price board | `btc-price` |
| BTC delta in price board | `btc-delta` |
| ETH price in price board | `eth-price` |
| ETH delta in price board | `eth-delta` |
| Mode toggle button | `mode-toggle-btn` |
| KPI: feature rows | `kpi-bars` |
| KPI: label rate | `kpi-label-rate` |
| KPI: PR-AUC | `kpi-prauc` |
| KPI: F1 | `kpi-f1` |
| KPI: train/val/test | `kpi-split` |
| Market card BTC bias badge | `market-btc-bias` |
| Market card BTC price | `market-btc-price` |
| Market card BTC vol text | `market-btc-vol` |
| Market card BTC next-hour up prob/move/price | `market-btc-hour-up-prob` / `-move` / `-price` |
| Market card BTC next-hour down prob/move/price | `market-btc-hour-down-prob` / `-move` / `-price` |
| Market card BTC next-day up/down (same pattern) | `market-btc-day-up-*` / `market-btc-day-down-*` |
| (ETH mirrors BTC, prefix `market-eth-*`) | |
| Week 4 API badge | `w4-api-badge` |
| Week 4 service/version/designation | `w4-service-name` / `w4-service-version` / `w4-designation` |
| Week 4 replay rows/cursor/threshold | `w4-replay-rows` / `w4-replay-cursor` / `w4-threshold` |
| Week 4 request count/pred rows/latency | `w4-request-count` / `w4-pred-row-count` / `w4-last-latency` |
| Week 4 open-docs button | `w4-launch-btn` |
| Week 4 status text | `w4-api-copy` |
| Replay run button | `w4-replay-btn` |
| Replay table | `w4-replay-table` |
| Replay copy text | `w4-replay-copy` |
| Chart canvas | `vol-chart` |
| Pair tab buttons | `.pair-tabs .tab` with `data-pair="BTC-USD"` / `data-pair="ETH-USD"` |
| Baseline PR-AUC / F1 / pos rate | `b-prauc` / `b-f1` / `b-pos` |
| Logistic PR-AUC / F1 / pos rate | `lr-prauc` / `lr-f1` / `lr-pos` |
| Delta PR-AUC bar + value | `delta-prauc-bar` / `delta-prauc-val` |
| Delta F1 bar + value | `delta-f1-bar` / `delta-f1-val` |
| Spike status badge | `spike-status` |
| Spike list container | `spike-radar-list` |
| Outlook pair label | `outlook-pair` |
| Outlook minute/hour/day higher turbulence % | `outlook-minute-up` / `outlook-hour-up` / `outlook-day-up` |
| Outlook minute/hour/day calmer % | `outlook-minute-down` / `outlook-hour-down` / `outlook-day-down` |
| Outlook student summary | `student-summary` |
| Price board container (for spike flash) | `price-board` |
| Predictions table | `pred-table` |

#### Step 4 — JavaScript Logic (`dashboard/app.js`)

**Constants:**
```js
const SSE_URL = 'http://localhost:8766/stream';
const W4_API_BASE = 'http://localhost:8000';
const CHART_MAX_POINTS = 300;   // rolling window in live mode
const W4_REPLAY_COUNT = 12;
const W4_REFRESH_MS = 10_000;
const VOL_SCALE = 10_000;       // realized_vol_60s × 10⁴ for readable axis
```

**Boot sequence (`init()`):**
1. `loadDashboard()` — `fetch('data/dashboard.json')` → parse
2. `renderTicker(data)` — build scrolling items string → set `innerHTML` of `#ticker-scroll`
3. `renderPriceBoard(data)` — fill `#btc-price`, `#eth-price`, `#btc-delta`, `#eth-delta`
4. `renderKPIs(data)` — fill 5 KPI card values
5. `renderScorecard(data)` — fill baseline + logistic metric blocks
6. `renderDeltaBars(data)` — animate progress bar widths via `setTimeout(..., 200)`
7. `renderPredictions(data)` — last 20 rows reversed into `#pred-table tbody`
8. `renderSpikeRadar(data)` — interleaved by pair, up to 8 rows in `#spike-radar-list`
9. `renderOutlook(data, activePair)` — turbulence outlook cards
10. `renderMarketOutlook(data)` — price scenario cards for both pairs
11. `bindPairTabs()` — click listener, calls `buildChart()` or `rebuildLiveChart()` depending on mode
12. `bindModeToggle()` — click on `#live-badge` and `#mode-toggle-btn`
13. `buildChart(activePair)` — construct Chart.js dual-axis chart from static data
14. `initW4Module()` — poll `/health`, `/version`, `/metrics` from W4 API
15. `trySSE()` — probe `:8766/status` with a 1.5s timeout
16. If SSE server is up → `connectSSE()` → live mode; else stay static

**Chart design (Chart.js):**
- Type: `'line'`
- Dataset 0: absolute price USD → left y-axis (`yPrice`)
- Dataset 1: `realized_vol_60s × 10⁴`, dashed line → right y-axis (`yVol`)
- Dataset 2: spike markers (orange filled circles, `showLine: false`) → right y-axis
- No animation (`duration: 0`) for performance
- Dark background chart area (`#0E0E0E` div behind canvas)
- Tooltip: dark `#111111` background, blueprint border, JetBrains Mono font

**Live SSE mode:**
- `EventSource` connects to `http://localhost:8766/stream`
- Each SSE message is a JSON object with fields: `product_id`, `ts`, `midprice`, `spread_bps`, `realized_vol_60s`, `predicted_spike`, `logistic_prob`
- `pushLiveTick(event)` appends to `liveBuffers[pid]` and trims to `CHART_MAX_POINTS`
- `updateLiveChart()` sets `chartInstance.data.*` and calls `chartInstance.update('none')`
- On spike (`event.predicted_spike == true`): flash price board border orange for 800ms, push to `liveSpikeEvents`, re-render spike radar

**Mode toggle logic:**
- `forcedStatic = true` → ignore incoming SSE, show `○ STATIC` badge (dashed border)
- `isLive = true` → show `● LIVE` badge (pulsing green)
- Clicking the badge when live → `switchToStatic()` (closes EventSource, re-renders static chart)
- Clicking when static → `switchToLive()` (re-probes SSE, reconnects)

**Week 4 API module:**
- `refreshW4Status()` — parallel `Promise.all([/health, /version, /metrics])`
- Parse Prometheus text metrics for `crypto_api_requests_total`, `crypto_api_prediction_rows_total`, `crypto_api_inference_seconds_*`
- `loadW4ReplaySample()` — POST to `/predict` with `{ replay_count: 12, replay_start_index: cursor }`
- Auto-advances cursor so each button click scores the next 12 rows
- Refresh status every `W4_REFRESH_MS` via `setInterval`

#### Step 5 — SSE Server (`scripts/dashboard_server.py`)

```python
"""
Lightweight HTTP server that:
  1. Consumes Kafka topic ticks.features
  2. Serves GET /stream as text/event-stream (SSE)
  3. Serves GET /status → {"ok": true}
  4. CORS header: Access-Control-Allow-Origin: *

Usage: python scripts/dashboard_server.py --port 8766
"""
```

Key implementation notes:
- Use `http.server.BaseHTTPRequestHandler` or FastAPI with `StreamingResponse`
- Each Kafka message is JSON-serialized and written as `data: {...}\n\n`
- Keep alive: send `data: {"ping":true}\n\n` every 15s if no new tick
- The server must already have `logistic_prob` and `predicted_spike` fields — the featurizer or a scoring step must add them before publishing to `ticks.features`

#### Step 6 — Adding to Docker Compose

Add the dashboard server as a service:
```yaml
  dashboard-server:
    build:
      context: .
      dockerfile: docker/Dockerfile.ingestor   # reuse python image
    command: python scripts/dashboard_server.py --port 8766
    ports:
      - "8766:8766"
    depends_on:
      - kafka
    environment:
      KAFKA_BOOTSTRAP_SERVERS: kafka:9092
```

Serve the static dashboard files with a one-liner (no extra container needed for development):
```bash
python -m http.server 8080 --directory dashboard/
# then open http://localhost:8080
```

---

### Layout & CSS Key Patterns

**Grid layouts:**
```css
/* KPI row — 5 equal columns, hard borders between cells */
.kpi-row { display: grid; grid-template-columns: repeat(5, 1fr); border: 3px solid #1A1A1A; }
.kpi-card { border-right: 3px solid #1A1A1A; }
.kpi-card:last-child { border-right: none; }

/* Market live row — 2 columns */
.market-live-row { display: grid; grid-template-columns: repeat(2, 1fr); }

/* Scorecard 2-col */
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }

/* Metric grid inside scorecard — 3 columns */
.metric-grid { display: grid; grid-template-columns: repeat(3, 1fr); }
```

**Offset shadows (neobrutalist signature):**
```css
/* Panel */
box-shadow: 6px 6px 0px #1A1A1A;

/* Winner panel override */
border-color: #00B884;
box-shadow: 6px 6px 0px #00B884;

/* Header price board */
border-color: #2B4CFF;
box-shadow: 6px 6px 0px #2B4CFF;
```

**Responsive breakpoints:**
- `≤ 1024px`: KPI row collapses to 3 columns
- `≤ 768px`: everything single column, chart height reduces to 260px

---

### Verification Steps

| Check | How to Verify |
|---|---|
| Export script runs | `python scripts/export_dashboard_json.py --predictions predictions_test.csv --features data/processed/features.parquet --run-id <id> --out dashboard/data/dashboard.json` exits 0 and file is valid JSON |
| Static mode renders | Open `http://localhost:8080` — all panels show real numbers, no `—` placeholders in KPI row |
| Chart loads | Volatility timeline renders with price line, dashed vol line, and at least some orange spike dots |
| PR-AUC delta bar animates | Reload page — green bar animates from 0% to its width within ~1s |
| Spike radar populated | `recent_spikes` array in `dashboard.json` has ≥ 1 entry → radar panel shows it |
| Week 4 API panel offline gracefully | With W4 API not running, panel shows `OFFLINE` badge and copy text (no JS errors in console) |
| Live mode activates | Start `dashboard_server.py` and reload — badge switches to `● LIVE`, prices update in real time |
| Live chart updates | New SSE ticks appear on the right edge of the chart, rolling window trims from the left |
| Spike flash works | When `predicted_spike: true` arrives over SSE, price board border flashes orange briefly |
| Mobile layout | Resize browser to 768px — panels stack vertically, no horizontal overflow |

---

## Cross-Cutting Reminders

**Avoid data leakage — the #1 silent killer of ML projects:**
- Never use future data to compute lookback features
- Always sort by time before splitting
- The forward label must only be computed on historical data during training/replay

**Secrets hygiene:**
- `.env` in `.gitignore` from day one
- Use `python-dotenv` to load: `from dotenv import load_dotenv; load_dotenv()`
- Provide `.env.example` with dummy values and comments

**Kafka topic naming:**
- `ticks.raw` — raw WebSocket events
- `ticks.features` — featurized rows (no label)

**MLflow experiment naming:**
- Use one experiment per milestone: `volatility-m1`, `volatility-m3`
- Tag runs with `mlflow.set_tag("milestone", "3")`
