"""
kafka_consume_check.py — Poll a Kafka topic and validate that the expected
number of messages have been produced.

Usage:
    python scripts/kafka_consume_check.py --topic ticks.raw --min 100 --timeout 60

Exit codes:
    0  — threshold met
    1  — timed out before reaching the minimum message count
"""

import argparse
import json
import os
import sys
import time

import yaml
from confluent_kafka import Consumer, KafkaError
from dotenv import load_dotenv

load_dotenv()


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_args():
    p = argparse.ArgumentParser(description="Kafka consumer validation check")
    p.add_argument("--topic", default="ticks.raw", help="Kafka topic to read from")
    p.add_argument("--min", type=int, default=100, dest="min_messages",
                   help="Minimum messages expected (default: 100)")
    p.add_argument("--timeout", type=int, default=60,
                   help="Max seconds to wait (default: 60)")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)
    broker = os.getenv("KAFKA_BROKER", cfg["kafka"]["broker"])

    consumer = Consumer({
        "bootstrap.servers": broker,
        "group.id": "validator",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([args.topic])

    count = 0
    first_ts = None
    last_ts = None
    sample = None
    deadline = time.time() + args.timeout

    print(f"[check] polling topic '{args.topic}' for at least {args.min_messages} "
          f"messages (timeout: {args.timeout}s) …")

    try:
        while time.time() < deadline:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"[check] consumer error: {msg.error()}", file=sys.stderr)
                continue

            count += 1
            try:
                payload = json.loads(msg.value().decode())
                ts = payload.get("ingested_at") or payload.get("time", "")
                if first_ts is None:
                    first_ts = ts
                    sample = payload
                last_ts = ts
            except (json.JSONDecodeError, AttributeError):
                pass

            if count >= args.min_messages:
                break
    finally:
        consumer.close()

    print(f"\n{'─'*50}")
    print(f"  Messages received : {count}")
    print(f"  First timestamp   : {first_ts or 'n/a'}")
    print(f"  Last  timestamp   : {last_ts or 'n/a'}")
    if sample:
        keys = list(sample.keys())[:8]
        print(f"  Sample keys       : {keys}")
    print(f"{'─'*50}")

    if count >= args.min_messages:
        print(f"[check] ✓ threshold met ({count} >= {args.min_messages})")
        sys.exit(0)
    else:
        print(f"[check] ✗ only {count} messages received (needed {args.min_messages})",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
