# Scoping Brief — BTC-USD Volatility Spike Classifier

**Course:** 45-886 Responsible AI in Production  
**Student:** Individual Assignment  
**Date:** April 2026

---

## Use Case

Real-time volatility alerting for the BTC-USD perpetual market on Coinbase.  
A trader or automated risk system needs advance warning — within a single
60-second window — that market volatility is about to spike, so they can
widen quotes, reduce position size, or hedge exposure before the move occurs.

---

## Prediction Goal

> At any time **T**, predict whether the **forward 60-second realized
> volatility** of BTC-USD mid-price log-returns will exceed a threshold τ.

Formally:

```
σ_future(T) = std( log(midprice_{t+1}/midprice_t) for t ∈ [T, T+60s] )
label(T)    = 1  if  σ_future(T) ≥ τ  else  0
```

The threshold τ will be set empirically at the **92nd–95th percentile** of
`σ_future` on the training window (~90-day rolling), ensuring roughly 5–10%
positive labels and making the problem tractable for imbalanced classifiers.

---

## Input Features (computed at time T from lookback data)

| Feature | Window | Description |
|---|---|---|
| `midprice` | instantaneous | (best_bid + best_ask) / 2 |
| `spread_pct` | instantaneous | (ask − bid) / midprice |
| `return_1s` | 1 s | log(midprice_t / midprice_{t−1}) |
| `rolling_vol_30s` | 30 s | std of `return_1s` |
| `rolling_vol_60s` | 60 s | std of `return_1s` |
| `trade_intensity` | 30 s | count of trades received |
| `bid_ask_imbalance` | instantaneous | (bid_size − ask_size) / (bid_size + ask_size) |

All features are derived **exclusively from data at or before time T** —
no look-ahead.

---

## Data Source

- **Provider:** Coinbase Advanced Trade WebSocket API (public, no auth)
- **Channel:** `ticker`
- **Pairs:** BTC-USD
- **Collection:** `scripts/ws_ingest.py` streams ticks → Kafka topic `ticks.raw`

---

## Success Metric

| Metric | Target | Rationale |
|---|---|---|
| **PR-AUC** | ≥ 0.55 | Precision-Recall AUC on the held-out test set; class-imbalance-robust |
| Baseline | ~0.08–0.10 | Naive classifier at class prevalence |

PR-AUC is preferred over ROC-AUC because the positive class (~8%) is rare;
accuracy and ROC-AUC are misleading metrics here.

---

## Risk Assumptions

| Risk | Mitigation |
|---|---|
| **Data latency** | WebSocket keeps ~millisecond round-trip; Kafka buffer handles bursts |
| **Class imbalance** | `class_weight='balanced'` in sklearn; PR-AUC as primary metric |
| **Look-ahead bias** | Forward labels computed only during replay/training; never at inference |
| **Single regime** | Model card will document training period; periodic retraining planned |
| **Market microstructure noise** | Spread / imbalance features smooth over 30 s windows |

---

## Out of Scope

- Automated trade execution or order placement  
- Multi-asset or cross-exchange arbitrage signals  
- Sub-second (HFT) prediction horizons  
- Prediction of spike *direction* (only spike *occurrence*)
