"""Deterministic simulated market used when no API key is configured.

Prices are FICTIONAL. They follow a seeded random walk so they are stable across restarts:
daily closes come from one walk per symbol, and each day's intraday path is a Brownian
bridge from the previous close to that day's close, so there are no jumps at midnight.
The demo market trades 24/7 on New York calendar days.
"""
from __future__ import annotations

import hashlib
import math
import random
import threading
from array import array
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

from .timeutil import NY, iso_utc, utc_now

# symbol, name, exchange, reference price, daily volatility
UNIVERSE = [
    ("AAPL", "Apple Inc.", "NASDAQ", 228.0, 0.016),
    ("MSFT", "Microsoft Corporation", "NASDAQ", 425.0, 0.015),
    ("NVDA", "NVIDIA Corporation", "NASDAQ", 135.0, 0.030),
    ("GOOGL", "Alphabet Inc. Class A", "NASDAQ", 170.0, 0.018),
    ("AMZN", "Amazon.com, Inc.", "NASDAQ", 190.0, 0.020),
    ("META", "Meta Platforms, Inc.", "NASDAQ", 560.0, 0.022),
    ("TSLA", "Tesla, Inc.", "NASDAQ", 250.0, 0.035),
    ("BRK.B", "Berkshire Hathaway Inc. Class B", "NYSE", 460.0, 0.010),
    ("JPM", "JPMorgan Chase & Co.", "NYSE", 220.0, 0.014),
    ("V", "Visa Inc.", "NYSE", 285.0, 0.012),
    ("MA", "Mastercard Incorporated", "NYSE", 500.0, 0.013),
    ("UNH", "UnitedHealth Group Incorporated", "NYSE", 560.0, 0.015),
    ("JNJ", "Johnson & Johnson", "NYSE", 160.0, 0.010),
    ("XOM", "Exxon Mobil Corporation", "NYSE", 118.0, 0.016),
    ("WMT", "Walmart Inc.", "NYSE", 82.0, 0.011),
    ("PG", "The Procter & Gamble Company", "NYSE", 168.0, 0.009),
    ("HD", "The Home Depot, Inc.", "NYSE", 395.0, 0.013),
    ("KO", "The Coca-Cola Company", "NYSE", 70.0, 0.009),
    ("PEP", "PepsiCo, Inc.", "NASDAQ", 168.0, 0.010),
    ("COST", "Costco Wholesale Corporation", "NASDAQ", 890.0, 0.012),
    ("DIS", "The Walt Disney Company", "NYSE", 95.0, 0.017),
    ("NFLX", "Netflix, Inc.", "NASDAQ", 720.0, 0.024),
    ("AMD", "Advanced Micro Devices, Inc.", "NASDAQ", 150.0, 0.030),
    ("INTC", "Intel Corporation", "NASDAQ", 22.0, 0.025),
    ("CRM", "Salesforce, Inc.", "NYSE", 280.0, 0.020),
    ("ADBE", "Adobe Inc.", "NASDAQ", 500.0, 0.019),
    ("ORCL", "Oracle Corporation", "NYSE", 165.0, 0.018),
    ("CSCO", "Cisco Systems, Inc.", "NASDAQ", 55.0, 0.012),
    ("PFE", "Pfizer Inc.", "NYSE", 29.0, 0.014),
    ("BA", "The Boeing Company", "NYSE", 155.0, 0.022),
    ("NKE", "NIKE, Inc.", "NYSE", 80.0, 0.018),
    ("MCD", "McDonald's Corporation", "NYSE", 295.0, 0.010),
    ("SBUX", "Starbucks Corporation", "NASDAQ", 95.0, 0.017),
    ("SPY", "SPDR S&P 500 ETF Trust", "NYSE Arca", 575.0, 0.009),
    ("QQQ", "Invesco QQQ Trust", "NASDAQ", 490.0, 0.012),
    ("VOO", "Vanguard S&P 500 ETF", "NYSE Arca", 530.0, 0.009),
]
BY_SYMBOL = {row[0]: row for row in UNIVERSE}

START = date(2019, 1, 1)
REFERENCE_DAY = date(2026, 1, 2)  # the walk is scaled so this day's close is the reference price
STEP_SECONDS = 15
STEPS = 86400 // STEP_SECONDS


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16)


def _midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=NY).timestamp() * 1000)


class DemoMarket:
    name = "Demo data (simulated)"

    def __init__(self):
        self.lock = threading.Lock()
        self._daily = {}  # symbol -> (rng, array of cumulative log returns since START)
        self._bridges = OrderedDict()  # (symbol, ordinal) -> array of prices per 15s step

    # --- price model -------------------------------------------------------------------
    def _logcum(self, symbol: str, day: date) -> float:
        idx = max(0, (day - START).days)
        state = self._daily.get(symbol)
        if state is None:
            state = (random.Random(_seed(symbol, "daily")), array("d", [0.0]))
            self._daily[symbol] = state
        rng, arr = state
        sigma = BY_SYMBOL[symbol][4]
        drift = 0.0003 - 0.5 * sigma * sigma
        while len(arr) <= idx:
            arr.append(arr[-1] + rng.gauss(drift, sigma))
        return arr[idx]

    def _close(self, symbol: str, day: date) -> float:
        ref_price = BY_SYMBOL[symbol][3]
        return ref_price * math.exp(self._logcum(symbol, day) - self._logcum(symbol, REFERENCE_DAY))

    def _bridge(self, symbol: str, day: date) -> array:
        key = (symbol, day.toordinal())
        cached = self._bridges.get(key)
        if cached is not None:
            self._bridges.move_to_end(key)
            return cached
        rng = random.Random(_seed(symbol, "intraday", day.toordinal()))
        walk = array("d", [0.0]) * (STEPS + 1)
        acc = 0.0
        for i in range(1, STEPS + 1):
            acc += rng.gauss(0.0, 1.0)
            walk[i] = acc
        prev = self._close(symbol, day - timedelta(days=1))
        target = self._close(symbol, day)
        drift = math.log(target / prev)
        scale = BY_SYMBOL[symbol][4] / math.sqrt(STEPS)
        end = walk[STEPS]
        path = array("d", (
            prev * math.exp(drift * i / STEPS + scale * (walk[i] - end * i / STEPS))
            for i in range(STEPS + 1)
        ))
        self._bridges[key] = path
        while len(self._bridges) > 150:
            self._bridges.popitem(last=False)
        return path

    @staticmethod
    def _now():
        ny = utc_now().astimezone(NY)
        seconds = ny.hour * 3600 + ny.minute * 60 + ny.second
        return ny.date(), min(STEPS, seconds // STEP_SECONDS)

    # --- public API -----------------------------------------------------------------------
    def has(self, symbol: str) -> bool:
        return symbol in BY_SYMBOL

    def quote(self, symbol: str) -> dict:
        _, name, exchange, _, _ = BY_SYMBOL[symbol]
        with self.lock:
            day, i = self._now()
            path = self._bridge(symbol, day)
            seg = path[: i + 1]
            price, prev = path[i], path[0]
            closes = [self._close(symbol, day - timedelta(days=k)) for k in range(1, 366)]
        vol_rng = random.Random(_seed(symbol, "volume", day.toordinal()))
        volume = int(vol_rng.uniform(2e6, 6e7) * (i / STEPS))
        as_of = datetime.fromtimestamp((_midnight_ms(day) + i * STEP_SECONDS * 1000) / 1000, timezone.utc)
        change = round(price, 2) - round(prev, 2)
        return {
            "symbol": symbol,
            "name": name,
            "exchange": exchange,
            "currency": "USD",
            "price": round(price, 2),
            "previous_close": round(prev, 2),
            "change": round(change, 2),
            "change_percent": round(change / round(prev, 2) * 100, 4),
            "open": round(path[0], 2),
            "high": round(max(seg), 2),
            "low": round(min(seg), 2),
            "volume": volume,
            "week52_high": round(max(closes + [price]), 2),
            "week52_low": round(min(closes + [price]), 2),
            "is_market_open": True,
            "as_of": iso_utc(as_of),
        }

    def history(self, symbol: str, rng: str) -> list:
        with self.lock:
            day, i = self._now()
            now_point = {"t": _midnight_ms(day) + i * STEP_SECONDS * 1000, "c": round(self._bridge(symbol, day)[i], 2)}
            points = []
            if rng in ("1D", "1W"):
                every = 20 if rng == "1D" else 240  # 5 minutes / 1 hour
                days = [day] if rng == "1D" else [day - timedelta(days=k) for k in range(6, -1, -1)]
                for d in days:
                    path = self._bridge(symbol, d)
                    last = i if d == day else STEPS
                    base = _midnight_ms(d)
                    for k in range(0, last + 1, every):
                        points.append({"t": base + k * STEP_SECONDS * 1000, "c": round(path[k], 2)})
            else:
                span, step = {"1M": (30, 1), "6M": (182, 1), "1Y": (365, 1), "5Y": (1826, 7)}[rng]
                for k in range(span, 0, -step):
                    d = day - timedelta(days=k)
                    points.append({"t": _midnight_ms(d) + 16 * 3600 * 1000, "c": round(self._close(symbol, d), 2)})
            if not points or points[-1]["t"] != now_point["t"]:
                points.append(now_point)
            return points

    def bars(self, symbol: str, start_ms: int, end_ms: int) -> list:
        """OHLC bars between two times: 5-minute bars for the last 40 days, daily bars before that."""
        out = []
        with self.lock:
            today, now_i = self._now()
            d = datetime.fromtimestamp(start_ms / 1000, NY).date()
            last_day = min(datetime.fromtimestamp(end_ms / 1000, NY).date(), today)
            while d <= last_day:
                base = _midnight_ms(d)
                if (today - d).days > 40:
                    o, c = self._close(symbol, d - timedelta(days=1)), self._close(symbol, d)
                    if start_ms <= base < end_ms:
                        out.append({"t": base, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "seconds": 86400})
                else:
                    path = self._bridge(symbol, d)
                    last = now_i if d == today else STEPS
                    for k in range(0, last, 20):
                        t = base + k * STEP_SECONDS * 1000
                        if t < start_ms or t >= end_ms:
                            continue
                        seg = path[k:min(k + 21, last + 1)]
                        out.append({"t": t, "o": seg[0], "h": max(seg), "l": min(seg), "c": seg[-1], "seconds": 300})
                d += timedelta(days=1)
        return out

    def search(self, query: str) -> list:
        q = query.strip().lower()
        scored = []
        for symbol, name, exchange, _, _ in UNIVERSE:
            s, n = symbol.lower(), name.lower()
            if s == q:
                score = 0
            elif s.startswith(q):
                score = 1
            elif any(word.startswith(q) for word in n.replace(",", " ").split()):
                score = 2
            elif q in n:
                score = 3
            else:
                continue
            kind = "ETF" if "ETF" in name or symbol in ("QQQ",) else "Common Stock"
            scored.append((score, symbol, {"symbol": symbol, "name": name, "exchange": exchange, "type": kind}))
        scored.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in scored[:12]]
