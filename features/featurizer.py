"""
features/featurizer.py — Stateful Kafka consumer that reads raw ticks,
computes windowed features, publishes them to `ticks.features`, and
appends rows to a Parquet file.

Usage:
    python features/featurizer.py
    python features/featurizer.py --config config.yaml --output data/processed/features.parquet
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from confluent_kafka import Consumer, KafkaError, Producer
from dotenv import load_dotenv

from features.core import TickBuffer, normalise_tick

load_dotenv()

FEATURE_COLS = [
    "time", "product_id",
    "midprice", "spread", "spread_pct",
    "return_1s", "rolling_vol_30s", "rolling_vol_60s",
    "trade_intensity", "bid_ask_imbalance",
]


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def delivery_report(err, msg):
    if err:
        print(f"[kafka] delivery error: {err}", file=sys.stderr)


def make_producer(broker: str) -> Producer:
    return Producer({"bootstrap.servers": broker, "linger.ms": 10})


def make_consumer(broker: str, topic: str) -> Consumer:
    return Consumer({
        "bootstrap.servers": broker,
        "group.id": "featurizer",
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    })


def append_parquet(rows: list[dict], output_path: str) -> None:
    """Append a batch of feature rows to a Parquet file."""
    df_new = pd.DataFrame(rows, columns=FEATURE_COLS)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        df_existing = pd.read_parquet(path)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_combined = df_new
    df_combined.to_parquet(path, index=False)


def run(config: dict, output_path: str, flush_every: int = 200) -> None:
    broker = os.getenv("KAFKA_BROKER", config["kafka"]["broker"])
    topic_raw = config["kafka"]["topic_raw"]
    topic_features = config["kafka"]["topic_features"]
    window_s = config["features"]["window_seconds"]

    consumer = make_consumer(broker, topic_raw)
    producer = make_producer(broker)
    consumer.subscribe([topic_raw])

    buffer = TickBuffer(window_seconds=window_s)
    batch: list[dict] = []
    msg_count = 0

    print(f"[featurizer] consuming '{topic_raw}' → publishing '{topic_features}' | output: {output_path}")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    print(f"[featurizer] error: {msg.error()}", file=sys.stderr)
                continue

            try:
                raw = json.loads(msg.value().decode())
            except (json.JSONDecodeError, AttributeError):
                continue

            tick = normalise_tick(raw)
            if tick is None:
                continue

            buffer.add(tick)
            row = buffer.compute_features(tick)
            if row is None:
                continue

            # Publish feature row (no label — label is forward-looking)
            feature_payload = json.dumps({k: row[k] for k in FEATURE_COLS})
            producer.produce(
                topic_features,
                key=(row.get("product_id", "") or "").encode(),
                value=feature_payload.encode(),
                callback=delivery_report,
            )
            producer.poll(0)

            batch.append(row)
            msg_count += 1

            if msg_count % flush_every == 0:
                append_parquet(batch, output_path)
                batch.clear()
                print(f"[featurizer] {msg_count} feature rows written")

    except KeyboardInterrupt:
        print("\n[featurizer] shutting down …")
    finally:
        if batch:
            append_parquet(batch, output_path)
            print(f"[featurizer] flushed final {len(batch)} rows")
        producer.flush()
        consumer.close()
        print(f"[featurizer] done — total: {msg_count} rows")


def parse_args():
    p = argparse.ArgumentParser(description="Kafka featurizer — ticks.raw → ticks.features")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--output", default="data/processed/features.parquet")
    p.add_argument("--flush-every", type=int, default=200,
                   help="Write to Parquet every N feature rows (default: 200)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = load_config(args.config)
    run(cfg, args.output, args.flush_every)
