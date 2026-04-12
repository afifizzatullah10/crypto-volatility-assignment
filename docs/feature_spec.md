# Feature Specification — BTC-USD Volatility Spike Classifier

**Version:** 1.0 | **Milestone:** 2 | **Last updated:** April 2026

---

## 1. Raw Input

| Field | Source | Type | Description |
|---|---|---|---|
| `best_bid` | Coinbase WS ticker | float | Best bid price |
| `best_ask` | Coinbase WS ticker | float | Best ask price |
| `best_bid_quantity` | Coinbase WS ticker | float | Size at best bid |
| `best_ask_quantity` | Coinbase WS ticker | float | Size at best ask |
| `time` | Coinbase WS envelope | ISO-8601 / Unix float | Exchange event timestamp |
| `product_id` | Coinbase WS ticker | string | Trading pair (e.g. `BTC-USD`) |

All fields are sourced from the `ticker` channel of the Coinbase Advanced Trade WebSocket API. No authentication is required.

---

## 2. Feature Definitions

All features are computed from data at or before time **T**. No future data is used. The lookback buffer retains ticks for the past 120 seconds.

| Feature | Formula | Window | Rationale |
|---|---|---|---|
| `midprice` | `(best_bid + best_ask) / 2` | instantaneous | Cleaner price signal than last-trade price |
| `spread` | `best_ask − best_bid` | instantaneous | Absolute liquidity cost |
| `spread_pct` | `spread / midprice` | instantaneous | Normalized spread; scale-invariant across price levels |
| `return_1s` | `log(midprice_t / midprice_{t−1})` | last 2 ticks | Instantaneous log-return |
| `rolling_vol_30s` | `std(log-returns over last 30 s)` | 30 s | Short-term realized volatility |
| `rolling_vol_60s` | `std(log-returns over last 60 s)` | 60 s | Medium-term realized volatility; primary vol signal |
| `trade_intensity` | `count(ticks in last 30 s)` | 30 s | Proxy for order-flow activity |
| `bid_ask_imbalance` | `(bid_size − ask_size) / (bid_size + ask_size)` | instantaneous | Order-book pressure; positive = buy-side heavy |

### Implementation Note

`rolling_vol_30s` and `rolling_vol_60s` use **population standard deviation** (ddof=0), consistent with the forward label computation. The `trade_intensity` feature counts the number of WebSocket messages received in the last 30 seconds — a coarse proxy for order-flow intensity.

---

## 3. Target Definition

| Property | Value |
|---|---|
| **Prediction horizon** | 60 seconds |
| **Volatility proxy** | Rolling std of midprice log-returns over [T, T+60 s] |
| **Formula** | `σ_future(T) = std(log(mid_{t+1}/mid_t) for t ∈ [T, T+60s])` |
| **Label** | `1` if `σ_future(T) ≥ τ`, else `0` |
| **Threshold τ** | Set at the **92nd percentile** of `σ_future` on the training window |
| **Expected class balance** | ~8% positive (spike) |

### Threshold Selection Rationale

The 92nd percentile was chosen by inspecting the percentile plot of `σ_future` in `notebooks/eda.ipynb` (Section 3 — Percentile Analysis). Key considerations:

- **Rarity:** Volatility spikes are relatively rare events; a 5–10% positive rate is intentional.
- **Actionability:** The threshold corresponds to moves that are large enough to meaningfully affect spread/risk, but not so extreme as to be unforecastable.
- **Metric alignment:** PR-AUC is used as the primary metric (not accuracy) precisely because of this imbalance.

> **Important:** τ must be fixed at training time and held constant at inference. It should be stored alongside the model artifact.

---

## 4. Look-Ahead Bias Prevention

| Rule | Where Enforced |
|---|---|
| Live features use only data ≤ T | `features/core.py` — TickBuffer evicts future data |
| Forward label uses data > T | `scripts/replay.py` — computed only offline |
| Label column NOT published to `ticks.features` | `features/featurizer.py` — feature rows contain no label field |
| Training split is time-ordered | `models/train.py` — `iloc` slicing, never `shuffle=True` |

---

## 5. Feature Pipeline Architecture

```
Coinbase WS
    │
    ▼
scripts/ws_ingest.py  ──────────────► Kafka: ticks.raw
                                           │
                           ┌───────────────┘
                           ▼
                    features/featurizer.py
                    (TickBuffer in features/core.py)
                           │
              ┌────────────┴────────────────┐
              ▼                             ▼
    Kafka: ticks.features        data/processed/features.parquet
                                     (no label — inference-safe)

── Offline / Training ──────────────────────────────────────────────────────────

    data/raw/*.ndjson
          │
          ▼
    scripts/replay.py
    (same TickBuffer from features/core.py)
          │
          ▼
    data/processed/features.parquet  ← with forward label
```

---

## 6. Reproducibility Contract

`replay.py` and `featurizer.py` both import `TickBuffer` and `compute_features` from **`features/core.py`**. Feature logic is never duplicated. If you change a feature formula, change it only in `core.py` and re-run replay.
