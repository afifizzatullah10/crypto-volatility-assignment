"""
ws_ingest.py — Connect to the Coinbase Advanced Trade WebSocket, publish raw
ticker events to a Kafka topic, and optionally mirror them to an NDJSON file.

Usage:
    python scripts/ws_ingest.py --pair BTC-USD --minutes 15
    python scripts/ws_ingest.py --pair BTC-USD --minutes 15 --output-dir data/raw
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from confluent_kafka import Producer
from dotenv import load_dotenv
import websocket

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── Kafka helpers ─────────────────────────────────────────────────────────────

def make_producer(broker: str) -> Producer:
    return Producer({"bootstrap.servers": broker, "linger.ms": 10})


def delivery_report(err, msg):
    if err:
        print(f"[kafka] delivery error: {err}", file=sys.stderr)


# ── WebSocket callbacks ───────────────────────────────────────────────────────

class Ingestor:
    def __init__(self, pair: str, config: dict, producer: Producer,
                 duration_seconds: int, output_file=None):
        self.pair = pair
        self.topic = config["kafka"]["topic_raw"]
        self.ws_url = config["coinbase"]["ws_url"]
        self.producer = producer
        self.deadline = time.time() + duration_seconds
        self.output_file = output_file
        self.msg_count = 0
        self._ws = None

    def _subscribe_payload(self) -> str:
        return json.dumps({
            "type": "subscribe",
            "product_ids": [self.pair],
            "channels": ["ticker"],
        })

    def on_open(self, ws):
        print(f"[ws] connected → subscribing to ticker:{self.pair}")
        ws.send(self._subscribe_payload())

    def on_message(self, ws, raw: str):
        if time.time() >= self.deadline:
            print("[ingestor] duration elapsed — closing")
            ws.close()
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg["ingested_at"] = datetime.now(timezone.utc).isoformat()
        enriched = json.dumps(msg)

        # Publish to Kafka
        product_id = msg.get("product_id", self.pair)
        self.producer.produce(
            self.topic,
            key=product_id.encode(),
            value=enriched.encode(),
            callback=delivery_report,
        )
        self.producer.poll(0)

        # Mirror to NDJSON
        if self.output_file:
            self.output_file.write(enriched + "\n")
            self.output_file.flush()

        self.msg_count += 1
        if self.msg_count % 100 == 0:
            print(f"[ingestor] {self.msg_count} messages published")

    def on_error(self, ws, error):
        print(f"[ws] error: {error}", file=sys.stderr)

    def on_close(self, ws, close_status_code, close_msg):
        print(f"[ws] closed (code={close_status_code})")


# ── Main reconnect loop ───────────────────────────────────────────────────────

def run(pair: str, config: dict, minutes: int, output_dir: str | None):
    broker = os.getenv("KAFKA_BROKER", config["kafka"]["broker"])
    producer = make_producer(broker)
    duration = minutes * 60

    output_file = None
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        fname = datetime.now(timezone.utc).strftime("ticks_%Y%m%d_%H%M%S.ndjson")
        fpath = Path(output_dir) / fname
        output_file = open(fpath, "w")
        print(f"[ingestor] mirroring ticks → {fpath}")

    ingestor = Ingestor(pair, config, producer, duration, output_file)
    ws_url = config["coinbase"]["ws_url"]
    delay = config["ingestor"]["reconnect_base_delay"]
    max_delay = config["ingestor"]["reconnect_max_delay"]

    try:
        while time.time() < ingestor.deadline:
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=ingestor.on_open,
                on_message=ingestor.on_message,
                on_error=ingestor.on_error,
                on_close=ingestor.on_close,
            )
            ingestor._ws = ws
            ws.run_forever()

            if time.time() >= ingestor.deadline:
                break

            print(f"[ingestor] reconnecting in {delay}s …")
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    finally:
        producer.flush()
        if output_file:
            output_file.close()
        print(f"[ingestor] done — total messages: {ingestor.msg_count}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Coinbase WebSocket → Kafka ingestor")
    p.add_argument("--pair", default="BTC-USD", help="Trading pair (default: BTC-USD)")
    p.add_argument("--minutes", type=int, default=15, help="Run duration in minutes")
    p.add_argument("--output-dir", default=None,
                   help="Directory to mirror NDJSON files (optional)")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = load_config(args.config)
    run(args.pair, cfg, args.minutes, args.output_dir)
