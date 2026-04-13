"""
scripts/export_dashboard_json.py — Build dashboard/data/dashboard.json from
MLflow artifacts, the feature Parquet, and the predictions CSV.

Usage:
    python scripts/export_dashboard_json.py
    python scripts/export_dashboard_json.py \\
        --predictions predictions_test.csv \\
        --features data/processed/features.parquet \\
        --lr-run-id 1514e479bdaa405abce2ccad3cf797c4 \\
        --baseline-run-id 245515f354224e9f84a096561faefd6e \\
        --out dashboard/data/dashboard.json
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.metrics import average_precision_score, f1_score, precision_recall_curve

load_dotenv()

FEATURE_COLS = [
    "spread_pct", "return_1s", "rolling_vol_30s",
    "rolling_vol_60s", "trade_intensity", "bid_ask_imbalance",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def ts_to_iso(unix_float: float) -> str:
    return datetime.fromtimestamp(unix_float, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def safe(v):
    """Convert numpy scalars / NaN to plain Python for JSON."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


def clamp(v, lo=0.05, hi=0.95):
    return max(lo, min(hi, float(v)))


def load_run_ids(path="models/artifacts/run_ids.txt"):
    ids = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                ids[k.strip()] = v.strip()
    return ids


def compute_zscore_scores(df: pd.DataFrame, roll_window: int = 300) -> pd.Series:
    import scipy.stats
    rolling_mean = df["rolling_vol_60s"].rolling(roll_window, min_periods=10).mean()
    rolling_std = (
        df["rolling_vol_60s"].rolling(roll_window, min_periods=10).std().replace(0, np.nan)
    )
    z = (df["rolling_vol_60s"] - rolling_mean) / rolling_std
    z = z.fillna(0.0)
    return pd.Series(scipy.stats.norm.cdf(z.values), index=df.index)


# ── price scenarios ──────────────────────────────────────────────────────────

def directional_prob(returns):
    filtered = [r for r in returns if math.isfinite(r)]
    if not filtered:
        return 0.5
    short = filtered[-30:]
    medium = filtered[-120:]
    mu_s, sigma_s = np.mean(short), max(np.std(short), 1e-8)
    mu_m, sigma_m = np.mean(medium), max(np.std(medium), 1e-8)
    score = 0.65 * (mu_s / sigma_s) + 0.35 * (mu_m / sigma_m)
    return clamp(0.5 + 0.22 * math.tanh(1.75 * score), 0.25, 0.75)


def projected_move(price, realized_vol, horizon_s, turb_prob):
    floor_frac = 0.0015 if horizon_s <= 3600 else 0.004
    cap_frac = 0.04 if horizon_s <= 3600 else 0.12
    scale = 0.7 + 0.9 * turb_prob
    raw = price * max(realized_vol or 0, 1e-6) * math.sqrt(horizon_s) * scale
    return min(price * cap_frac, max(price * floor_frac, raw))


def build_price_scenario(pair, prices, realized_vols, latest_prob):
    prices_clean = [p for p in prices if p and math.isfinite(p) and p > 0]
    if len(prices_clean) < 3:
        return None
    returns = [math.log(prices_clean[i] / prices_clean[i - 1])
               for i in range(1, len(prices_clean))]
    current = prices_clean[-1]
    realized_vol = realized_vols[-1] if realized_vols else 1e-5
    up_p = directional_prob(returns)
    down_p = 1 - up_p
    hour_move = projected_move(current, realized_vol, 3600, latest_prob)
    day_move = projected_move(current, realized_vol, 86400, latest_prob)
    bias = "MIXED"
    if up_p >= 0.57:
        bias = "UP BIAS"
    if up_p <= 0.43:
        bias = "DOWN BIAS"
    return {
        "current_price": safe(current),
        "bias_label": bias,
        "next_hour": {
            "up_probability": safe(up_p), "down_probability": safe(down_p),
            "up_move_usd": safe(hour_move), "down_move_usd": safe(hour_move),
            "up_target": safe(current + hour_move), "down_target": safe(current - hour_move),
        },
        "next_day": {
            "up_probability": safe(up_p), "down_probability": safe(down_p),
            "up_move_usd": safe(day_move), "down_move_usd": safe(day_move),
            "up_target": safe(current + day_move), "down_target": safe(current - day_move),
        },
    }


def build_probability_outlook(pair, probs, realized_vols):
    if not probs:
        return None
    latest = clamp(np.mean(probs[-10:]))
    sorted_v = sorted([v for v in realized_vols if v and math.isfinite(v)])
    if sorted_v:
        last_vol = realized_vols[-1] if realized_vols[-1] else sorted_v[-1]
        rank = sum(1 for v in sorted_v if v <= last_vol)
        vol_pct = clamp(rank / len(sorted_v))
    else:
        vol_pct = 0.5

    recent = [v for v in realized_vols[-60:] if v and math.isfinite(v)]
    prev = [v for v in realized_vols[-120:-60] if v and math.isfinite(v)]
    prev_mean = max(np.mean(prev) if prev else np.mean(recent) if recent else 1e-9, 1e-9)
    trend = clamp(0.5 + 0.35 * ((np.mean(recent) - prev_mean) / prev_mean)) if recent else 0.5
    spikes = sum(1 for p in probs if p and p > 0.5)
    sess_pressure = clamp(spikes / max(len(probs), 1))

    min_up = clamp(0.85 * latest + 0.15 * vol_pct)
    hour_up = clamp(0.55 * latest + 0.25 * vol_pct + 0.20 * trend)
    day_up = clamp(0.30 * latest + 0.35 * vol_pct + 0.20 * sess_pressure + 0.15 * trend)

    return {
        "next_minute": {"higher_turbulence": safe(min_up), "calmer_conditions": safe(1 - min_up)},
        "next_hour": {"higher_turbulence": safe(hour_up), "calmer_conditions": safe(1 - hour_up)},
        "next_day": {"higher_turbulence": safe(day_up), "calmer_conditions": safe(1 - day_up)},
        "student_summary": (
            f"{pair} shows a {int(hour_up*100)}% chance of rougher-than-normal trading next hour "
            f"and {int(day_up*100)}% chance choppy conditions persist into the next day. "
            "These are turbulence probabilities, not price-direction forecasts."
        ),
    }


# ── metrics helpers ──────────────────────────────────────────────────────────

def best_f1_threshold(y_true, y_score):
    prec, rec, thresholds = precision_recall_curve(y_true, y_score)
    f1s = 2 * prec * rec / (prec + rec + 1e-9)
    idx = int(np.argmax(f1s[:-1]))
    return float(thresholds[idx]), float(f1s[idx])


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Export dashboard.json from model artifacts")
    p.add_argument("--predictions", default="predictions_test.csv")
    p.add_argument("--features", default="data/processed/features.parquet")
    p.add_argument("--lr-run-id", default=None)
    p.add_argument("--baseline-run-id", default=None)
    p.add_argument("--tracking-uri", default=None)
    p.add_argument("--out", default="dashboard/data/dashboard.json")
    return p.parse_args()


def main():
    args = parse_args()

    tracking_uri = args.tracking_uri or os.getenv("MLFLOW_TRACKING_URI") or "mlruns"
    mlflow.set_tracking_uri(tracking_uri)

    # ── Resolve run IDs ───────────────────────────────────────────────────────
    saved = load_run_ids()
    lr_run_id = args.lr_run_id or saved.get("lr_run_id")
    bs_run_id = args.baseline_run_id or saved.get("baseline_run_id")
    best_threshold = float(saved.get("lr_best_threshold", 0.5))

    if not lr_run_id:
        print("[export] ERROR: no lr_run_id — run models/train.py first", file=sys.stderr)
        sys.exit(1)

    # ── Load features & predictions ──────────────────────────────────────────
    print("[export] loading features...")
    df = pd.read_parquet(args.features).sort_values("time").reset_index(drop=True)
    df = df.dropna(subset=FEATURE_COLS + ["label"]).copy()
    n = len(df)

    print("[export] loading predictions...")
    pred = pd.read_csv(args.predictions).sort_values("time").reset_index(drop=True)

    # Positional alignment — both files come from the same features.parquet sorted by time
    merged = df.copy()
    if len(pred) == n:
        merged["label_pred"] = pred["label_pred"].values
        merged["pred_prob"]  = pred["pred_prob"].values
    else:
        # Fallback: merge on time (may produce duplicates with float keys)
        print(f"[export] WARNING: pred rows ({len(pred)}) != feature rows ({n}), falling back to merge")
        merged = df.merge(pred[["time", "label_pred", "pred_prob"]], on="time", how="left")

    # Time-based split sizes (mirrors train.py 60/20/20)
    train_end = int(n * 0.60)
    val_end   = int(n * 0.80)
    train_df  = merged.iloc[:train_end]
    val_df    = merged.iloc[train_end:val_end]
    test_df   = merged.iloc[val_end:]

    print(f"[export] train={len(train_df):,}  val={len(val_df):,}  test={len(test_df):,}")

    # ── LR model metrics ─────────────────────────────────────────────────────
    lr_probs = merged["pred_prob"].fillna(0).values
    lr_probs_test = test_df["pred_prob"].fillna(0).values
    y_test = test_df["label"].values
    y_all = merged["label"].values

    lr_prauc_test = safe(average_precision_score(y_all, lr_probs))
    lr_thresh, lr_f1_test = best_f1_threshold(y_all, lr_probs)
    lr_preds = (lr_probs >= lr_thresh).astype(int)
    lr_pos_rate = safe(float(lr_preds.mean()))

    # Try to get from MLflow
    try:
        lr_run = mlflow.get_run(lr_run_id)
        mlflow_lr_prauc = lr_run.data.metrics.get("pr_auc_test", lr_prauc_test)
        lr_prauc_test = safe(mlflow_lr_prauc) if mlflow_lr_prauc and mlflow_lr_prauc > 0 else lr_prauc_test
        best_threshold = float(lr_run.data.params.get("best_threshold", best_threshold))
    except Exception as e:
        print(f"[export] MLflow LR read failed: {e}")

    # ── Baseline metrics ─────────────────────────────────────────────────────
    bs_scores = compute_zscore_scores(merged).values
    bs_prauc = safe(average_precision_score(y_all, bs_scores))
    bs_thresh, bs_f1 = best_f1_threshold(y_all, bs_scores)
    bs_preds = (bs_scores >= bs_thresh).astype(int)
    bs_pos_rate = safe(float(bs_preds.mean()))

    try:
        if bs_run_id:
            bs_run = mlflow.get_run(bs_run_id)
            mlflow_bs_prauc = bs_run.data.metrics.get("pr_auc_test", bs_prauc)
            bs_prauc = safe(mlflow_bs_prauc) if mlflow_bs_prauc and mlflow_bs_prauc > 0 else bs_prauc
    except Exception as e:
        print(f"[export] MLflow baseline read failed: {e}")

    print(f"[export] LR     PR-AUC={lr_prauc_test:.4f}  F1={lr_f1_test:.4f}  threshold={best_threshold:.4f}")
    print(f"[export] Baseline PR-AUC={bs_prauc:.4f}  F1={bs_f1:.4f}")

    # ── Price summary ─────────────────────────────────────────────────────────
    pairs = merged["product_id"].unique().tolist()
    price_summary = {}
    for pair in pairs:
        pdata = merged[merged["product_id"] == pair]["midprice"].dropna()
        if len(pdata) >= 2:
            first, last = float(pdata.iloc[0]), float(pdata.iloc[-1])
            delta_pct = safe((last - first) / first * 100)
            price_summary[pair] = {"last": safe(last), "delta_pct": delta_pct}

    # Session info
    t_start = float(merged["time"].min())
    t_end = float(merged["time"].max())
    session_start = ts_to_iso(t_start)
    session_end = ts_to_iso(t_end)
    session_minutes = safe((t_end - t_start) / 60)

    # ── Chart series (downsample to ≤ 1500 rows per pair) ────────────────────
    chart_series = {}
    for pair in pairs:
        pdata = merged[merged["product_id"] == pair].copy()
        step = max(1, len(pdata) // 1500)
        sampled = pdata.iloc[::step].copy()
        chart_series[pair] = [
            {
                "window_end_ts": ts_to_iso(float(row["time"])),
                "midprice": safe(row["midprice"]),
                "realized_vol_60s": safe(row["rolling_vol_60s"]),
                "predicted_spike": int(row["pred_prob"] >= best_threshold) if pd.notna(row.get("pred_prob")) else 0,
            }
            for _, row in sampled.iterrows()
        ]

    # ── Predictions (last 20 rows with spikes in test set shown) ─────────────
    test_rows = merged.copy()
    test_rows["predicted_label"] = (test_rows["pred_prob"].fillna(0) >= best_threshold).astype(int)
    test_rows["baseline_score"] = compute_zscore_scores(merged).values
    recent_20 = test_rows.tail(20)
    predictions_out = [
        {
            "window_end_ts": ts_to_iso(float(r["time"])),
            "product_id": str(r.get("product_id", "BTC-USD")),
            "label": int(r["label"]),
            "predicted_label": int(r["predicted_label"]),
            "logistic_probability": safe(r["pred_prob"]),
            "baseline_score": safe(r["baseline_score"]),
            "realized_vol_60s": safe(r["rolling_vol_60s"]),
        }
        for _, r in recent_20.iterrows()
    ]

    # ── Recent spikes ──────────────────────────────────────────────────────────
    spike_rows = test_rows[test_rows["predicted_label"] == 1].tail(12)
    recent_spikes = [
        {
            "window_end_ts": ts_to_iso(float(r["time"])),
            "product_id": str(r.get("product_id", "BTC-USD")),
            "midprice": safe(r["midprice"]),
            "realized_vol_60s": safe(r["rolling_vol_60s"]),
            "logistic_probability": safe(r["pred_prob"]),
        }
        for _, r in spike_rows.iterrows()
    ]

    # ── Probability outlook per pair ──────────────────────────────────────────
    probability_outlook = {}
    price_scenarios = {}
    for pair in pairs:
        pdata = merged[merged["product_id"] == pair]
        probs_list = pdata["pred_prob"].fillna(0).tolist()
        vols_list = pdata["rolling_vol_60s"].fillna(0).tolist()
        prices_list = pdata["midprice"].tolist()
        probability_outlook[pair] = build_probability_outlook(pair, probs_list, vols_list)
        latest_prob_val = clamp(float(np.mean(probs_list[-10:]))) if probs_list else 0.5
        price_scenarios[pair] = build_price_scenario(pair, prices_list, vols_list, latest_prob_val)

    # ── Assemble payload ──────────────────────────────────────────────────────
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_start": session_start,
        "session_end": session_end,
        "session_minutes": session_minutes,
        "feature_rows": n,
        "label_rate": safe(float(df["label"].mean())),
        "price_summary": price_summary,
        "metrics": {
            "train_rows": len(train_df),
            "validation_rows": len(val_df),
            "test_rows": len(test_df),
            "logistic_regression": {
                "pr_auc": safe(lr_prauc_test),
                "f1_at_threshold": safe(lr_f1_test),
                "positive_rate": safe(lr_pos_rate),
                "threshold": safe(best_threshold),
            },
            "baseline": {
                "pr_auc": safe(bs_prauc),
                "f1_at_threshold": safe(bs_f1),
                "positive_rate": safe(bs_pos_rate),
            },
        },
        "chart_series": chart_series,
        "predictions": predictions_out,
        "recent_spikes": recent_spikes,
        "probability_outlook": probability_outlook,
        "price_scenarios": price_scenarios,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[export] written → {out_path}  ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
