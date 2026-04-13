# Model Card v1 — BTC-USD Volatility Spike Classifier

**Version:** 1.0 | **Date:** April 2026 | **Milestone:** 3

---

## Model Details

| Property | Value |
|---|---|
| **Model type** | Logistic Regression (`sklearn.linear_model.LogisticRegression`) |
| **Pipeline** | `StandardScaler` → `LogisticRegression(class_weight='balanced', max_iter=1000)` |
| **MLflow experiment** | `volatility-m3` |
| **Run name** | `logistic_regression_v1` |
| **Serialisation** | MLflow artifact (`mlflow.sklearn.log_model`) |
| **Primary metric** | PR-AUC (Precision-Recall AUC) |

---

## Intended Use

### Primary use case
Short-term (60-second) volatility spike alerting for the BTC-USD market.  
At any time **T**, the model outputs a probability score that realized volatility over [T, T+60s] will exceed threshold τ.

### Intended users
- Risk management systems that widen spreads or reduce exposure ahead of volatile periods
- Quantitative researchers studying microstructure volatility regimes

### Out-of-scope uses
- **Automated trade execution** — the model outputs a probability, not a trading signal
- **Other trading pairs** — trained only on BTC-USD; performance on other pairs is unknown
- **Long-horizon prediction** — the 60-second label horizon is hard-coded; do not interpret scores as forecasts beyond 60 seconds

---

## Training Data

| Property | Value |
|---|---|
| **Source** | Coinbase Exchange WebSocket (`wss://ws-feed.exchange.coinbase.com`) |
| **Channel** | `ticker` (public, no authentication required) |
| **Pairs** | BTC-USD |
| **Collection period** | See `data/raw/` NDJSON file timestamps |
| **Feature pipeline** | `scripts/replay.py` → `features/core.py` (TickBuffer, 120s rolling window) |

### Label construction
```
σ_future(T) = std( log(mid_{t+1}/mid_t) for t ∈ [T, T+60s] )
label(T) = 1  if  σ_future(T) ≥ τ  else  0
```
τ is set at the **92nd percentile** of `σ_future` on the full dataset, resulting in approximately **8% positive labels**.

### Data splits (time-ordered, no shuffle)
| Split | Fraction | Purpose |
|---|---|---|
| Train | 70% | Model fitting |
| Validation | 15% | Threshold tuning |
| Test | 15% | Final reported metrics |

---

## Features

| Feature | Description |
|---|---|
| `spread_pct` | Normalised bid-ask spread |
| `return_1s` | Log-return of midprice vs. previous tick |
| `rolling_vol_30s` | 30-second realized volatility |
| `rolling_vol_60s` | 60-second realized volatility |
| `trade_intensity` | Tick count in last 30 seconds |
| `bid_ask_imbalance` | (bid_size − ask_size) / (bid_size + ask_size) |

Full feature definitions in `docs/feature_spec.md`.

---

## Evaluation

| Split | PR-AUC |
|---|---|
| Validation | *(fill in after training)* |
| Test | *(fill in after training)* |

> Update this table with actual values from `mlflow.log_metric("pr_auc_test", ...)` after running `python models/train.py`.

### Baseline comparison

| Model | Test PR-AUC |
|---|---|
| Baseline (z-score rule) | *(fill in)* |
| Logistic Regression v1  | *(fill in)* |
| Chance (class prevalence ~8%) | ~0.08 |

**Success criterion:** PR-AUC ≥ 0.55 on the test set.

---

## Limitations & Risks

| Limitation | Details |
|---|---|
| **Single market regime** | Trained on a short data window; performance may degrade in low-volatility environments or during structural market changes |
| **Class imbalance** | ~8% positive rate — raw accuracy is misleading; always report PR-AUC |
| **No execution logic** | Model does not account for slippage, latency, or market impact |
| **Kafka dependency** | Live inference depends on Kafka pipeline uptime; a Kafka outage stops feature production |
| **Label is forward-looking** | The 60-second label cannot be verified in real-time; only retrospective accuracy is measurable |

---

## Ethical Considerations

- Uses only **public market data** from Coinbase's public WebSocket feed
- **No user data or PII** is collected or processed
- The model is intended as a **risk management tool**, not for autonomous decision-making
- Misuse for market manipulation is not an intended use and would likely be ineffective given the model's simplicity

---

## How to Reproduce

```bash
# 1. Collect data
python scripts/ws_ingest.py --pair BTC-USD --minutes 15 --output-dir data/raw

# 2. Build feature table with labels
python scripts/replay.py --raw "data/raw/*.ndjson" --out data/processed/features.parquet

# 3. Train and log to MLflow
python models/train.py

# 4. Run inference
python models/infer.py --features data/processed/features.parquet --output predictions.csv
```
