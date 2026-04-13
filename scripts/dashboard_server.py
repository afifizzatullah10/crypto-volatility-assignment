"""
scripts/dashboard_server.py — SSE server for live dashboard.

Two modes:
  --mode kafka   : consume from Kafka ticks.features topic (requires running infra)
  --mode replay  : stream rows from data/processed/features.parquet + model scores

The browser connects to GET /stream and receives Server-Sent Events.
Each event is a JSON object with fields:
  product_id, ts, midprice, spread_bps, realized_vol_60s, predicted_spike, logistic_prob

Usage:
    python scripts/dashboard_server.py                    # replay mode, port 8766
    python scripts/dashboard_server.py --mode kafka       # live Kafka mode
    python scripts/dashboard_server.py --port 9000        # custom port
    python scripts/dashboard_server.py --speed 20         # 20 rows/sec (replay)
"""

import argparse
import json
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import mlflow.sklearn
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

FEATURE_COLS = [
    "spread_pct", "return_1s", "rolling_vol_30s",
    "rolling_vol_60s", "trade_intensity", "bid_ask_imbalance",
]

# ── Connected SSE clients ─────────────────────────────────────────────────────
_clients: list = []
_clients_lock = threading.Lock()


def broadcast(payload: dict) -> None:
    data = "data: " + json.dumps(payload) + "\n\n"
    encoded = data.encode()
    dead = []
    with _clients_lock:
        for wfile in _clients:
            try:
                wfile.write(encoded)
                wfile.flush()
            except Exception:
                dead.append(wfile)
        for wfile in dead:
            _clients.remove(wfile)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default logging
        pass

    def do_GET(self):
        if self.path == "/stream":
            self._handle_stream()
        elif self.path == "/status":
            self._handle_status()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def _handle_status(self):
        body = json.dumps({"ok": True, "mode": "sse"}).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_stream(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.end_headers()

        with _clients_lock:
            _clients.append(self.wfile)

        # Keep the connection open until client disconnects
        try:
            while True:
                time.sleep(15)
                # Send keep-alive ping
                try:
                    self.wfile.write(b"data: {\"ping\":true}\n\n")
                    self.wfile.flush()
                except Exception:
                    break
        finally:
            with _clients_lock:
                if self.wfile in _clients:
                    _clients.remove(self.wfile)


# ── Replay producer (from features.parquet) ───────────────────────────────────

def load_model(tracking_uri: str, run_ids_path: str):
    """Load LR model from MLflow, return pipeline or None."""
    try:
        mlflow.set_tracking_uri(tracking_uri)
        ids = {}
        p = Path(run_ids_path)
        if p.exists():
            for line in p.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    ids[k.strip()] = v.strip()
        run_id = ids.get("lr_run_id")
        thresh = float(ids.get("lr_best_threshold", 0.5))
        if not run_id:
            print("[server] No lr_run_id found — using threshold=0.5 fallback", file=sys.stderr)
            return None, thresh
        pipe = mlflow.sklearn.load_model(f"runs:/{run_id}/model")
        print(f"[server] Model loaded (run={run_id}, threshold={thresh:.4f})")
        return pipe, thresh
    except Exception as e:
        print(f"[server] Model load failed: {e}", file=sys.stderr)
        return None, 0.5


def replay_producer(features_path: str, speed: float, tracking_uri: str, run_ids_path: str):
    """Stream feature rows, score with model, broadcast over SSE."""
    df = pd.read_parquet(features_path).sort_values("time").reset_index(drop=True)
    pipe, thresh = load_model(tracking_uri, run_ids_path)

    # Score all rows upfront
    if pipe is not None:
        X = df[FEATURE_COLS].fillna(0)
        probs = pipe.predict_proba(X)[:, 1]
    else:
        # Fallback: use normalized vol as crude proxy
        vol = df["rolling_vol_60s"].fillna(0)
        probs = (vol / (vol.max() + 1e-12)).values

    interval = 1.0 / speed

    print(f"[server] Replay mode: {len(df):,} rows at {speed:.1f} rows/sec (port serves :8766)")

    while True:
        for i, (_, row) in enumerate(df.iterrows()):
            from datetime import datetime, timezone
            ts_iso = datetime.fromtimestamp(float(row["time"]), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
            spread_bps = float(row["spread"] / row["midprice"] * 10000) if row.get("midprice") else None

            prob    = float(probs[i])
            is_spike = bool(prob >= thresh)

            payload = {
                "product_id":       str(row.get("product_id", "BTC-USD")),
                "ts":               ts_iso,
                "midprice":         float(row["midprice"]) if pd.notna(row.get("midprice")) else None,
                "spread_bps":       round(spread_bps, 4) if spread_bps else None,
                "realized_vol_60s": float(row["rolling_vol_60s"]) if pd.notna(row.get("rolling_vol_60s")) else None,
                "predicted_spike":  is_spike,
                "logistic_prob":    round(prob, 6),
            }

            with _clients_lock:
                has_clients = len(_clients) > 0

            if has_clients:
                broadcast(payload)

            time.sleep(interval)

        print("[server] Replay loop complete — restarting from beginning")


# ── Kafka producer ────────────────────────────────────────────────────────────

def kafka_producer(broker: str, topic: str, tracking_uri: str, run_ids_path: str):
    """Consume from Kafka ticks.features and broadcast as SSE."""
    try:
        from confluent_kafka import Consumer
    except ImportError:
        print("[server] confluent-kafka not installed. Falling back to replay mode.", file=sys.stderr)
        replay_producer("data/processed/features.parquet", 10, tracking_uri, run_ids_path)
        return

    pipe, thresh = load_model(tracking_uri, run_ids_path)
    consumer = Consumer({
        "bootstrap.servers": broker,
        "group.id":          "dashboard-sse",
        "auto.offset.reset": "latest",
    })
    consumer.subscribe([topic])
    print(f"[server] Kafka mode: subscribed to {topic} @ {broker}")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None or msg.error():
                continue
            try:
                row = json.loads(msg.value().decode())
            except Exception:
                continue

            feat = {k: row.get(k, 0) for k in FEATURE_COLS}
            if pipe is not None:
                X = pd.DataFrame([feat])
                prob = float(pipe.predict_proba(X)[0, 1])
            else:
                prob = float(feat.get("rolling_vol_60s", 0)) * 1e4

            row["logistic_prob"]    = round(prob, 6)
            row["predicted_spike"]  = bool(prob >= thresh)
            broadcast(row)
    finally:
        consumer.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Dashboard SSE server")
    p.add_argument("--port",         type=int,   default=8766)
    p.add_argument("--mode",         choices=["replay", "kafka"], default="replay")
    p.add_argument("--features",     default="data/processed/features.parquet")
    p.add_argument("--speed",        type=float, default=10.0, help="Rows/sec in replay mode")
    p.add_argument("--kafka-broker", default="localhost:9092")
    p.add_argument("--kafka-topic",  default="ticks.features")
    p.add_argument("--tracking-uri", default=None)
    p.add_argument("--run-ids",      default="models/artifacts/run_ids.txt")
    return p.parse_args()


def main():
    args = parse_args()
    tracking_uri = args.tracking_uri or os.getenv("MLFLOW_TRACKING_URI") or "mlruns"

    # Start data producer in background thread
    if args.mode == "kafka":
        producer_fn = lambda: kafka_producer(args.kafka_broker, args.kafka_topic, tracking_uri, args.run_ids)
    else:
        producer_fn = lambda: replay_producer(args.features, args.speed, tracking_uri, args.run_ids)

    t = threading.Thread(target=producer_fn, daemon=True)
    t.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[server] SSE server listening on http://localhost:{args.port}")
    print(f"[server]   GET /stream → text/event-stream")
    print(f"[server]   GET /status → {{\"ok\": true}}")
    print(f"[server] Mode: {args.mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutting down")


if __name__ == "__main__":
    main()
