"""
scripts/replay.py — Replay saved NDJSON tick files through the shared
feature pipeline to produce a labelled Parquet dataset for training.

Usage:
    python scripts/replay.py --raw "data/raw/*.ndjson" --out data/processed/features.parquet
    python scripts/replay.py --raw data/raw/ticks_20260412_120000.ndjson --tau 0.00042
"""

import argparse
import glob
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))
from features.core import TickBuffer, normalise_tick

FEATURE_COLS = [
    "time", "product_id",
    "midprice", "spread", "spread_pct",
    "return_1s", "rolling_vol_30s", "rolling_vol_60s",
    "trade_intensity", "bid_ask_imbalance",
]


# ── Label construction ────────────────────────────────────────────────────────

def attach_forward_labels(df: pd.DataFrame, horizon_s: float = 60.0,
                          tau: float | None = None,
                          tau_percentile: float = 92.0) -> pd.DataFrame:
    """
    Compute forward realized volatility over [T, T+horizon_s] for every row,
    then threshold at tau to produce a binary label.

    tau=None → auto-set at tau_percentile of the computed sigma values
    (use this for training; fix tau at inference time).
    """
    df = df.sort_values("time").reset_index(drop=True)
    times = df["time"].values
    mids = df["midprice"].values

    sigma_future = np.zeros(len(df), dtype=float)

    for i in range(len(df)):
        t0 = times[i]
        mask = (times > t0) & (times <= t0 + horizon_s)
        future_mids = mids[mask]
        if len(future_mids) < 2:
            sigma_future[i] = np.nan
            continue
        log_rets = np.diff(np.log(future_mids[future_mids > 0]))
        sigma_future[i] = np.std(log_rets, ddof=0) if len(log_rets) >= 2 else np.nan

    df["sigma_future"] = sigma_future

    # Drop rows where forward label can't be computed (end of dataset)
    df = df.dropna(subset=["sigma_future"]).copy()

    if tau is None:
        tau = float(np.percentile(df["sigma_future"], tau_percentile))
        print(f"[replay] auto τ = {tau:.6f}  ({tau_percentile}th percentile)")

    df["label"] = (df["sigma_future"] >= tau).astype(int)
    df.attrs["tau"] = tau
    return df


# ── NDJSON loading ────────────────────────────────────────────────────────────

def load_ndjson_files(pattern: str) -> list[dict]:
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"[replay] no files matched: {pattern}", file=sys.stderr)
        sys.exit(1)
    print(f"[replay] loading {len(files)} file(s) …")
    raw_ticks: list[dict] = []
    for fpath in files:
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw_ticks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    print(f"[replay] loaded {len(raw_ticks):,} raw lines")
    return raw_ticks


# ── Main replay loop ──────────────────────────────────────────────────────────

def replay(raw_ticks: list[dict], window_s: float = 120.0) -> list[dict]:
    """Feed ticks one-by-one through TickBuffer and collect feature rows."""
    buffer = TickBuffer(window_seconds=window_s)
    rows: list[dict] = []
    skipped = 0

    # Sort by exchange timestamp (critical — files may overlap)
    normalised = []
    for raw in raw_ticks:
        tick = normalise_tick(raw)
        if tick is not None:
            normalised.append(tick)
        else:
            skipped += 1

    normalised.sort(key=lambda t: t["time"])
    print(f"[replay] {len(normalised):,} valid ticks ({skipped:,} skipped)")

    for tick in normalised:
        buffer.add(tick)
        row = buffer.compute_features(tick)
        if row is not None:
            rows.append(row)

    print(f"[replay] {len(rows):,} feature rows computed")
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Replay NDJSON ticks → labelled Parquet feature table")
    p.add_argument("--raw", required=True,
                   help='Glob pattern for NDJSON files, e.g. "data/raw/*.ndjson"')
    p.add_argument("--out", default="data/processed/features.parquet",
                   help="Output Parquet path (default: data/processed/features.parquet)")
    p.add_argument("--tau", type=float, default=None,
                   help="Fixed volatility threshold τ (default: auto at 92nd percentile)")
    p.add_argument("--tau-percentile", type=float, default=92.0,
                   help="Percentile used to auto-set τ (default: 92)")
    p.add_argument("--horizon", type=float, default=60.0,
                   help="Forward label horizon in seconds (default: 60)")
    p.add_argument("--window", type=float, default=120.0,
                   help="TickBuffer lookback window in seconds (default: 120)")
    return p.parse_args()


def main():
    args = parse_args()

    raw_ticks = load_ndjson_files(args.raw)
    rows = replay(raw_ticks, window_s=args.window)

    if not rows:
        print("[replay] no feature rows produced — check your NDJSON files", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(rows, columns=FEATURE_COLS)
    df = attach_forward_labels(df, horizon_s=args.horizon,
                                tau=args.tau,
                                tau_percentile=args.tau_percentile)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    tau = df.attrs.get("tau", args.tau)
    pos_rate = df["label"].mean() * 100
    print(f"\n[replay] saved {len(df):,} rows → {out_path}")
    print(f"         τ = {tau:.6f} | positive rate = {pos_rate:.1f}%")
    print(f"         columns: {list(df.columns)}")


if __name__ == "__main__":
    main()
