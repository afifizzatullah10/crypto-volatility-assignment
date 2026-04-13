"""
models/infer.py — Load a logged MLflow model and score a feature Parquet file.

Usage:
    python models/infer.py --run-id <mlflow_run_id> --features data/processed/features.parquet
    python models/infer.py --run-id <id> --features data/processed/features.parquet --output predictions.csv
"""

import argparse
import os
import sys
import time
from pathlib import Path

import mlflow.sklearn
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.metrics import average_precision_score

load_dotenv()

FEATURE_COLS = [
    "spread_pct",
    "return_1s",
    "rolling_vol_30s",
    "rolling_vol_60s",
    "trade_intensity",
    "bid_ask_imbalance",
]


def load_run_id_from_file(path: str = "models/artifacts/run_ids.txt") -> str:
    """Fall back to reading the run ID saved by train.py."""
    p = Path(path)
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith("lr_run_id="):
            return line.split("=", 1)[1].strip()
    return None


def parse_args():
    p = argparse.ArgumentParser(description="Score feature rows with a logged MLflow model")
    p.add_argument("--run-id",      default=None,
                   help="MLflow run ID (default: reads from models/artifacts/run_ids.txt)")
    p.add_argument("--features",    default="data/processed/features.parquet")
    p.add_argument("--output",      default="predictions.csv")
    p.add_argument("--threshold",   type=float, default=None,
                   help="Decision threshold for binary label (default: 0.5)")
    p.add_argument("--tracking-uri", default=None)
    return p.parse_args()


def main():
    args = parse_args()

    tracking_uri = (
        args.tracking_uri
        or os.getenv("MLFLOW_TRACKING_URI")
        or "http://localhost:5001"
    )
    mlflow.set_tracking_uri(tracking_uri)

    run_id = args.run_id or load_run_id_from_file()
    if not run_id:
        print("[infer] ERROR: no --run-id provided and models/artifacts/run_ids.txt not found.\n"
              "        Run models/train.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"[infer] loading model from run: {run_id}")
    model_uri = f"runs:/{run_id}/model"
    pipe = mlflow.sklearn.load_model(model_uri)
    print("[infer] model loaded")

    df = pd.read_parquet(args.features).sort_values("time").reset_index(drop=True)
    X = df[FEATURE_COLS].fillna(0)

    threshold = args.threshold or 0.5

    t0 = time.perf_counter()
    prob = pipe.predict_proba(X)[:, 1]
    elapsed = time.perf_counter() - t0

    pred_label = (prob >= threshold).astype(int)

    results = pd.DataFrame({
        "time":       df["time"],
        "label_true": df["label"] if "label" in df.columns else np.nan,
        "label_pred": pred_label,
        "pred_prob":  prob,
    })
    results.to_csv(args.output, index=False)

    data_span = float(df["time"].max() - df["time"].min())
    print(f"[infer] scored {len(df):,} rows in {elapsed:.3f}s  "
          f"(data span: {data_span:.0f}s)")
    print(f"[infer] latency check: {elapsed:.3f}s < {2 * data_span:.0f}s  "
          f"{'✓ PASS' if elapsed < 2 * data_span else '✗ FAIL'}")

    if "label" in df.columns:
        valid = results.dropna(subset=["label_true"])
        prauc = average_precision_score(valid["label_true"], valid["pred_prob"])
        print(f"[infer] PR-AUC on this split: {prauc:.4f}")

    print(f"[infer] predictions saved → {args.output}")


if __name__ == "__main__":
    main()
