"""
scripts/run_w4_api.py — Week 4 FastAPI replay service on port 8000.

Endpoints:
  GET  /health   → service health + replay state
  GET  /version  → model version, designation, threshold
  GET  /metrics  → Prometheus text format
  POST /predict  → { replay_count, replay_start_index } → scores

Usage:
    python scripts/run_w4_api.py
    python scripts/run_w4_api.py --port 8000 --features data/processed/features.parquet
"""

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import mlflow.sklearn
import numpy as np
import pandas as pd
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

load_dotenv()

FEATURE_COLS = [
    "spread_pct", "return_1s", "rolling_vol_30s",
    "rolling_vol_60s", "trade_intensity", "bid_ask_imbalance",
]

# ── App state (populated at startup) ─────────────────────────────────────────

app = FastAPI(
    title="Crypto Volatility Intelligence — Week 4 API",
    description="Replay-mode logistic regression inference service",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
_pipe         = None
_threshold    = 0.5
_features_df: pd.DataFrame = None
_replay_cursor: int = 0
_run_id        = None
_tracking_uri  = None

# Prometheus counters (simple dict)
_metrics = {
    "requests_total": 0,
    "prediction_rows_total": 0,
    "inference_seconds_sum": 0.0,
    "inference_seconds_count": 0,
}


# ── Pydantic models ───────────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    replay_count: int = 12
    replay_start_index: int | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "service":        "crypto-vol-intel",
        "model_loaded":   _pipe is not None,
        "replay_rows":    len(_features_df) if _features_df is not None else 0,
        "replay_cursor":  _replay_cursor,
        "status":         "ok",
    }


@app.get("/version")
def version():
    return {
        "version":      "1.0.0",
        "designation":  "LOGISTIC-REGRESSION-V1",
        "threshold":    _threshold,
        "run_id":       _run_id or "—",
        "features":     FEATURE_COLS,
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    lines = [
        "# HELP crypto_api_requests_total Total POST /predict calls",
        "# TYPE crypto_api_requests_total counter",
        f"crypto_api_requests_total {_metrics['requests_total']}",
        "# HELP crypto_api_prediction_rows_total Total rows scored",
        "# TYPE crypto_api_prediction_rows_total counter",
        f"crypto_api_prediction_rows_total {_metrics['prediction_rows_total']}",
        "# HELP crypto_api_inference_seconds Inference latency",
        "# TYPE crypto_api_inference_seconds summary",
        f"crypto_api_inference_seconds_sum {_metrics['inference_seconds_sum']:.6f}",
        f"crypto_api_inference_seconds_count {_metrics['inference_seconds_count']}",
    ]
    return "\n".join(lines) + "\n"


@app.post("/predict")
def predict(req: PredictRequest):
    global _replay_cursor

    if _pipe is None or _features_df is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    total = len(_features_df)
    start = req.replay_start_index if req.replay_start_index is not None else _replay_cursor

    # Wrap around
    if start >= total:
        start = 0

    end = min(start + req.replay_count, total)
    rows = _features_df.iloc[start:end]

    t0 = time.perf_counter()
    X = rows[FEATURE_COLS].fillna(0)
    probs = _pipe.predict_proba(X)[:, 1].tolist()
    elapsed = time.perf_counter() - t0

    _replay_cursor = end if end < total else 0

    # Update Prometheus counters
    _metrics["requests_total"]          += 1
    _metrics["prediction_rows_total"]   += len(probs)
    _metrics["inference_seconds_sum"]   += elapsed
    _metrics["inference_seconds_count"] += 1

    return {
        "scores":              probs,
        "replay_start_index":  start,
        "replay_end_index":    end,
        "model_variant":       "logistic-regression-v1",
        "threshold":           _threshold,
        "ts":                  datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


# ── Startup ───────────────────────────────────────────────────────────────────

def load_state(features_path: str, tracking_uri: str, run_ids_path: str):
    global _pipe, _threshold, _features_df, _run_id, _tracking_uri

    _tracking_uri = tracking_uri
    mlflow.set_tracking_uri(tracking_uri)

    # Load run IDs
    ids = {}
    p = Path(run_ids_path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                ids[k.strip()] = v.strip()

    _run_id   = ids.get("lr_run_id")
    _threshold = float(ids.get("lr_best_threshold", 0.5))

    if _run_id:
        try:
            import mlflow.sklearn as mls
            _pipe = mls.load_model(f"runs:/{_run_id}/model")
            print(f"[w4api] Model loaded (run={_run_id}, threshold={_threshold:.4f})")
        except Exception as e:
            print(f"[w4api] Model load failed: {e}")

    # Load features
    fp = Path(features_path)
    if fp.exists():
        _features_df = pd.read_parquet(features_path).sort_values("time").reset_index(drop=True)
        _features_df = _features_df.dropna(subset=FEATURE_COLS).copy()
        print(f"[w4api] Features loaded: {len(_features_df):,} rows from {features_path}")
    else:
        print(f"[w4api] WARNING: {features_path} not found", flush=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Week 4 FastAPI replay service")
    p.add_argument("--port",         type=int, default=8000)
    p.add_argument("--host",         default="0.0.0.0")
    p.add_argument("--features",     default="data/processed/features.parquet")
    p.add_argument("--tracking-uri", default=None)
    p.add_argument("--run-ids",      default="models/artifacts/run_ids.txt")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    tracking_uri = args.tracking_uri or os.getenv("MLFLOW_TRACKING_URI") or "mlruns"
    load_state(args.features, tracking_uri, args.run_ids)
    print(f"[w4api] Starting on http://{args.host}:{args.port}")
    print(f"[w4api] Docs at http://localhost:{args.port}/docs")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
