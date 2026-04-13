"""
features/core.py — Shared tick buffer and feature computation logic.

Both the live Kafka featurizer and the offline replay script import from here,
ensuring a single source of truth for all feature definitions.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional


class TickBuffer:
    """Maintains a rolling window of ticks and computes features on demand."""

    def __init__(self, window_seconds: float = 120.0):
        self.ticks: deque[dict] = deque()
        self.window = window_seconds

    def add(self, tick: dict) -> None:
        """Append tick and evict entries outside the rolling window."""
        self.ticks.append(tick)
        cutoff = tick["time"] - self.window
        while self.ticks and self.ticks[0]["time"] < cutoff:
            self.ticks.popleft()

    def compute_features(self, tick: dict) -> Optional[dict]:
        """
        Return a feature row for the given tick, or None if there are fewer
        than 2 ticks in the buffer (insufficient data for returns).
        """
        if len(self.ticks) < 2:
            return None

        ticks = list(self.ticks)
        now = tick["time"]

        # ── Price fields ───────────────────────────────────────────────────
        try:
            best_bid = float(tick.get("best_bid", 0) or 0)
            best_ask = float(tick.get("best_ask", 0) or 0)
            best_bid_size = float(tick.get("best_bid_size") or tick.get("best_bid_quantity") or 0)
            best_ask_size = float(tick.get("best_ask_size") or tick.get("best_ask_quantity") or 0)
        except (TypeError, ValueError):
            return None

        if best_bid <= 0 or best_ask <= 0:
            return None

        midprice = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
        spread_pct = spread / midprice if midprice > 0 else 0.0

        # ── Log returns from ticks with valid midprice ─────────────────────
        returns: list[float] = []
        prev_mid: Optional[float] = None
        trade_count_30s = 0

        for t in ticks:
            try:
                b = float(t.get("best_bid", 0) or 0)
                a = float(t.get("best_ask", 0) or 0)
            except (TypeError, ValueError):
                continue
            if b <= 0 or a <= 0:
                continue
            mid = (b + a) / 2.0
            if prev_mid is not None and prev_mid > 0 and mid > 0:
                returns.append(math.log(mid / prev_mid))
            prev_mid = mid

            # Count trades in last 30 s
            t_time = t.get("time", 0)
            if isinstance(t_time, (int, float)) and (now - t_time) <= 30:
                trade_count_30s += 1

        # ── Instantaneous return (last two ticks) ──────────────────────────
        return_1s = returns[-1] if returns else 0.0

        # ── Rolling volatility ─────────────────────────────────────────────
        rolling_vol_30s = _std(returns_in_window(ticks, now, 30))
        rolling_vol_60s = _std(returns_in_window(ticks, now, 60))

        # ── Bid-ask imbalance ──────────────────────────────────────────────
        total_size = best_bid_size + best_ask_size
        bid_ask_imbalance = (
            (best_bid_size - best_ask_size) / total_size
            if total_size > 0 else 0.0
        )

        return {
            "time": now,
            "product_id": tick.get("product_id", ""),
            "midprice": midprice,
            "spread": spread,
            "spread_pct": spread_pct,
            "return_1s": return_1s,
            "rolling_vol_30s": rolling_vol_30s,
            "rolling_vol_60s": rolling_vol_60s,
            "trade_intensity": trade_count_30s,
            "bid_ask_imbalance": bid_ask_imbalance,
        }


# ── Helper utilities ──────────────────────────────────────────────────────────

def returns_in_window(ticks: list[dict], now: float, window_s: float) -> list[float]:
    """Compute log-returns for ticks within `window_s` seconds of `now`."""
    cutoff = now - window_s
    window_ticks = [t for t in ticks if t.get("time", 0) >= cutoff]
    returns: list[float] = []
    prev_mid: Optional[float] = None
    for t in window_ticks:
        try:
            b = float(t.get("best_bid", 0) or 0)
            a = float(t.get("best_ask", 0) or 0)
        except (TypeError, ValueError):
            continue
        if b <= 0 or a <= 0:
            continue
        mid = (b + a) / 2.0
        if prev_mid is not None and prev_mid > 0 and mid > 0:
            returns.append(math.log(mid / prev_mid))
        prev_mid = mid
    return returns


def _std(values: list[float]) -> float:
    """Population standard deviation; returns 0.0 for fewer than 2 values."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


def parse_tick_time(tick: dict) -> Optional[float]:
    """
    Extract a Unix timestamp (float seconds) from a Coinbase ticker event.
    Handles both ISO-8601 string and numeric epoch formats.
    """
    from datetime import datetime, timezone

    raw = tick.get("time") or tick.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def normalise_tick(raw: dict) -> Optional[dict]:
    """
    Convert a raw Coinbase WebSocket ticker event into a normalised dict
    with a float `time` field (Unix seconds). Returns None if the event
    is not a ticker update.

    Handles three formats:
    1. Coinbase Exchange (ws-feed.exchange.coinbase.com) — flat {"type":"ticker", ...}
    2. Coinbase Advanced Trade envelope — {"events": [{"type":"update","tickers":[...]}]}
    3. NDJSON lines with an added "ingested_at" field wrapping either of the above
    """
    # Unwrap NDJSON-enriched outer envelope (ingested_at added by ws_ingest.py)
    inner = raw.get("events")

    # Format 2: Advanced Trade envelope
    if inner:
        for event in inner:
            if event.get("type") == "update":
                for ticker in event.get("tickers", []):
                    t = dict(ticker)
                    ts = parse_tick_time(raw) or parse_tick_time(t)
                    if ts is None:
                        continue
                    t["time"] = ts
                    t.setdefault("product_id", raw.get("product_id", ""))
                    return t
        return None

    # Format 1: Coinbase Exchange flat ticker
    if raw.get("type") == "ticker":
        ts = parse_tick_time(raw)
        if ts is None:
            return None
        tick = dict(raw)
        tick["time"] = ts
        return tick

    return None
