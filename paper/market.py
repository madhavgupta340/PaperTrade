"""Market data behind one cached, rate-limited facade.

Providers:
  * TwelveDataProvider - https://twelvedata.com (free "Basic" plan: 8 credits/min, 800/day).
    The API key is sent from the server in an Authorization header and never reaches the browser.
  * DemoMarket - fictional, deterministic prices for when no key is configured.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .demo import UNIVERSE, DemoMarket
from .timeutil import NY, iso_utc

log = logging.getLogger(__name__)

RANGES = ("1D", "1W", "1M", "6M", "1Y", "5Y")
HISTORY_TTL = {"1D": 120, "1W": 600, "1M": 3600, "6M": 3600, "1Y": 3600, "5Y": 6 * 3600}
SEARCH_TTL = 24 * 3600


class MarketDataError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


class RateLimiter:
    """Sliding one-minute window plus a per-UTC-day counter, matching Twelve Data's credit model."""

    def __init__(self, per_minute: int, per_day: int):
        self.per_minute = per_minute
        self.per_day = per_day
        self._window = deque()
        self._day = None
        self._day_count = 0
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._window and now - self._window[0] >= 60:
            self._window.popleft()
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day, self._day_count = today, 0

    def acquire_up_to(self, n: int) -> int:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            granted = max(0, min(n, self.per_minute - len(self._window), self.per_day - self._day_count))
            self._window.extend([now] * granted)
            self._day_count += granted
            return granted

    def snapshot(self) -> dict:
        with self._lock:
            self._prune(time.monotonic())
            return {
                "per_minute": self.per_minute,
                "used_last_minute": len(self._window),
                "per_day": self.per_day,
                "used_today": self._day_count,
            }


class TTLCache:
    def __init__(self, max_items: int = 3000):
        self._data = {}
        self._max = max_items
        self._lock = threading.Lock()

    def get(self, key):
        """Returns (value, age_seconds, stored_at_epoch) or None."""
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return None
        value, stored = item
        return value, time.time() - stored, stored

    def set(self, key, value) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._data[key] = (value, time.time())
            while len(self._data) > self._max:
                self._data.pop(next(iter(self._data)))


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TwelveDataProvider:
    name = "Twelve Data"
    BASE = "https://api.twelvedata.com"
    HISTORY_SPECS = {
        "1D": ("5min", 200),
        "1W": ("30min", 100),
        "1M": ("1day", 23),
        "6M": ("1day", 128),
        "1Y": ("1day", 253),
        "5Y": ("1week", 262),
    }

    def __init__(self, api_key: str, timeout: float = 10.0):
        self._api_key = api_key
        self._timeout = timeout

    def _get(self, endpoint: str, params: dict):
        req = Request(
            f"{self.BASE}/{endpoint}?{urlencode(params)}",
            headers={
                "Authorization": f"apikey {self._api_key}",
                "Accept": "application/json",
                "User-Agent": "paper-trader/1.0",
            },
        )
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                raise MarketDataError(f"Market data provider returned HTTP {exc.code}.", 502)
        except ValueError:
            raise MarketDataError("Market data provider returned an unreadable response.", 502)
        except (URLError, OSError):
            raise MarketDataError("Could not reach the market data provider. Check your internet connection.", 503)
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise self._error(payload)
        return payload

    @staticmethod
    def _error(payload: dict) -> MarketDataError:
        code = payload.get("code")
        message = str(payload.get("message") or "Market data request failed.")
        if code == 429:
            return MarketDataError(
                "Market data rate limit reached (Twelve Data free tier: 8 requests/minute, 800/day). "
                "Try again in a minute.", 429)
        if code == 401:
            return MarketDataError("The Twelve Data API key was rejected. Check TWELVE_DATA_API_KEY in .env.", 502)
        if code == 404 or (code == 400 and "symbol" in message.lower()):
            return MarketDataError("Symbol not found.", 404)
        if code == 403:
            return MarketDataError("This symbol or data is not included in your Twelve Data plan.", 403)
        return MarketDataError(f"Market data error: {message[:200]}", 502)

    def quotes(self, symbols: list):
        payload = self._get("quote", {"symbol": ",".join(symbols)})
        if len(symbols) == 1:
            payload = {symbols[0]: payload}
        out, errors = {}, {}
        for symbol in symbols:
            item = payload.get(symbol) if isinstance(payload, dict) else None
            if not isinstance(item, dict):
                errors[symbol] = MarketDataError("No quote returned for this symbol.", 404)
            elif item.get("status") == "error":
                errors[symbol] = self._error(item)
            else:
                try:
                    out[symbol] = self._parse_quote(symbol, item)
                except (KeyError, TypeError, ValueError):
                    errors[symbol] = MarketDataError("Quote data from the provider was incomplete.", 502)
        return out, errors

    @staticmethod
    def _parse_quote(symbol: str, item: dict) -> dict:
        price = float(item["close"])
        prev = _num(item.get("previous_close"))
        change = _num(item.get("change"))
        pct = _num(item.get("percent_change"))
        if prev is None:
            prev = price - (change or 0.0)
        if change is None:
            change = price - prev
        if pct is None:
            pct = (change / prev * 100) if prev else 0.0
        ts = item.get("last_quote_at") or item.get("timestamp")
        as_of = iso_utc(datetime.fromtimestamp(int(ts), timezone.utc)) if ts else None
        week = item.get("fifty_two_week") or {}
        volume = _num(item.get("volume"))
        return {
            "symbol": symbol,
            "name": item.get("name") or symbol,
            "exchange": item.get("exchange"),
            "currency": item.get("currency") or "USD",
            "price": price,
            "previous_close": prev,
            "change": change,
            "change_percent": pct,
            "open": _num(item.get("open")),
            "high": _num(item.get("high")),
            "low": _num(item.get("low")),
            "volume": int(volume) if volume is not None else None,
            "week52_high": _num(week.get("high")),
            "week52_low": _num(week.get("low")),
            "is_market_open": bool(item.get("is_market_open")),
            "as_of": as_of,
        }

    def search(self, query: str) -> list:
        payload = self._get("symbol_search", {"symbol": query, "outputsize": 30})
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        results, seen = [], set()
        for row in rows:
            symbol = row.get("symbol")
            if not symbol or symbol in seen:
                continue
            if row.get("country") not in (None, "United States") or row.get("currency") not in (None, "USD"):
                continue
            seen.add(symbol)
            results.append({
                "symbol": symbol,
                "name": row.get("instrument_name") or symbol,
                "exchange": row.get("exchange"),
                "type": row.get("instrument_type"),
            })
        return results[:12]

    def bars(self, symbol: str, start: datetime, end: datetime) -> list:
        """OHLC bars covering [start, end), finest interval that fits in one request."""
        span = (end - start).total_seconds()
        interval, seconds = ("5min", 300) if span <= 3 * 86400 else ("1h", 3600) if span <= 120 * 86400 else ("1day", 86400)
        try:
            payload = self._get("time_series", {
                "symbol": symbol, "interval": interval, "timezone": "UTC", "order": "asc", "outputsize": 5000,
                "start_date": start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except MarketDataError as exc:
            if "no data" in exc.message.lower():
                return []  # e.g. the market was closed the whole time
            raise
        out = []
        for row in payload.get("values") or []:
            raw = row.get("datetime", "")
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S" if " " in raw else "%Y-%m-%d").replace(tzinfo=timezone.utc)
                out.append({"t": int(dt.timestamp() * 1000), "o": float(row["open"]), "h": float(row["high"]),
                            "l": float(row["low"]), "c": float(row["close"]), "seconds": seconds})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda b: b["t"])
        start_ms = start.timestamp() * 1000
        return [b for b in out if b["t"] >= start_ms]

    def history(self, symbol: str, rng: str) -> list:
        interval, size = self.HISTORY_SPECS[rng]
        payload = self._get("time_series", {"symbol": symbol, "interval": interval, "outputsize": size})
        meta = payload.get("meta") or {}
        try:
            tz = ZoneInfo(meta.get("exchange_timezone") or "America/New_York")
        except (ZoneInfoNotFoundError, ValueError):
            tz = NY
        points = []
        for row in reversed(payload.get("values") or []):
            raw = row.get("datetime", "")
            fmt = "%Y-%m-%d %H:%M:%S" if " " in raw else "%Y-%m-%d"
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=tz)
                if fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=16)
                points.append({"t": int(dt.timestamp() * 1000), "c": float(row["close"])})
            except (KeyError, TypeError, ValueError):
                continue
        if rng in ("1D", "1W") and points:
            def day_of(p):
                return datetime.fromtimestamp(p["t"] / 1000, tz).date()
            keep = sorted({day_of(p) for p in points})[-1 if rng == "1D" else -5:]
            points = [p for p in points if day_of(p) in keep]
        return points


class MarketData:
    """Chooses a provider, caches responses and spends the API budget carefully."""

    def __init__(self, config):
        self.demo = config.use_demo
        self.provider = DemoMarket() if self.demo else TwelveDataProvider(config.twelve_data_api_key)
        self.label = "demo" if self.demo else config.data_label
        self.quote_ttl = 10 if self.demo else config.quote_cache_seconds
        self.limiter = None if self.demo else RateLimiter(config.rate_limit_per_minute, config.rate_limit_per_day)
        self.cache = TTLCache()
        self.last_error = None

    # --- helpers --------------------------------------------------------------------------
    def _budget(self, n: int) -> int:
        return n if self.limiter is None else self.limiter.acquire_up_to(n)

    def _decorate(self, quote: dict, stored: float, stale: bool) -> dict:
        q = dict(quote)
        q["fetched_at"] = iso_utc(datetime.fromtimestamp(stored, timezone.utc))
        q["stale"] = stale
        q["source"] = "demo" if self.demo else ("delayed" if stale else self.label)
        return q

    def status(self) -> dict:
        return {
            "mode": "demo" if self.demo else "live",
            "provider": self.provider.name,
            "label": self.label,
            "quote_refresh_seconds": self.quote_ttl,
            "rate_limit": self.limiter.snapshot() if self.limiter else None,
            "last_error": self.last_error,
            "demo_symbols": [row[0] for row in UNIVERSE] if self.demo else None,
        }

    # --- quotes ---------------------------------------------------------------------------
    def get_quotes(self, symbols):
        """Returns (quotes, errors). Quotes past their TTL are served as stale when the budget is spent."""
        symbols = list(dict.fromkeys(symbols))
        out, errors, need = {}, {}, []
        for symbol in symbols:
            hit = self.cache.get(("q", symbol))
            if hit and hit[1] < self.quote_ttl:
                out[symbol] = self._decorate(hit[0], hit[2], stale=False)
            else:
                need.append(symbol)
        if not need:
            return out, errors

        if self.demo:
            for symbol in need:
                if self.provider.has(symbol):
                    quote = self.provider.quote(symbol)
                    self.cache.set(("q", symbol), quote)
                    out[symbol] = self._decorate(quote, time.time(), stale=False)
                else:
                    errors[symbol] = MarketDataError(
                        f"{symbol} is not in the demo data set. Demo mode covers {len(UNIVERSE)} popular US tickers; "
                        "add a Twelve Data API key for full coverage.", 404)
            return out, errors

        granted = self._budget(len(need))
        fetch, deferred = need[:granted], need[granted:]
        failed = {}
        if fetch:
            try:
                fresh, failed = self.provider.quotes(fetch)
                self.last_error = None
            except MarketDataError as exc:
                fresh, failed = {}, {s: exc for s in fetch}
                self.last_error = exc.message
                log.warning("Quote request failed: %s", exc.message)
            now = time.time()
            for symbol, quote in fresh.items():
                self.cache.set(("q", symbol), quote)
                out[symbol] = self._decorate(quote, now, stale=False)
        limit_error = MarketDataError(
            "Free-tier rate limit reached; this quote will refresh within a minute.", 429)
        for symbol in deferred + list(failed):
            hit = self.cache.get(("q", symbol))
            err = failed.get(symbol, limit_error)
            if hit and err.status != 404:
                out[symbol] = self._decorate(hit[0], hit[2], stale=True)
            else:
                errors[symbol] = err
        return out, errors

    def get_quote(self, symbol: str) -> dict:
        quotes, errors = self.get_quotes([symbol])
        if symbol in quotes:
            return quotes[symbol]
        raise errors.get(symbol) or MarketDataError("Quote unavailable.", 502)

    # --- history & search ---------------------------------------------------------------
    def history(self, symbol: str, rng: str) -> dict:
        if rng not in RANGES:
            raise MarketDataError(f"Range must be one of {', '.join(RANGES)}.", 400)
        if self.demo:
            if not self.provider.has(symbol):
                raise MarketDataError(f"{symbol} is not in the demo data set.", 404)
            return {"symbol": symbol, "range": rng, "points": self.provider.history(symbol, rng),
                    "fetched_at": iso_utc(), "stale": False, "source": "demo"}
        key = ("h", symbol, rng)
        hit = self.cache.get(key)
        if hit and hit[1] < HISTORY_TTL[rng]:
            return self._history_payload(symbol, rng, hit, stale=False)
        if self._budget(1):
            try:
                points = self.provider.history(symbol, rng)
                self.cache.set(key, points)
                return self._history_payload(symbol, rng, self.cache.get(key), stale=False)
            except MarketDataError as exc:
                if not hit or exc.status == 404:
                    raise
                self.last_error = exc.message
        if hit:
            return self._history_payload(symbol, rng, hit, stale=True)
        raise MarketDataError(
            "Chart data is temporarily unavailable: the free-tier rate limit (8 requests/minute) is used up. "
            "Try again in a minute.", 429)

    def _history_payload(self, symbol, rng, hit, stale: bool) -> dict:
        return {"symbol": symbol, "range": rng, "points": hit[0], "stale": stale,
                "fetched_at": iso_utc(datetime.fromtimestamp(hit[2], timezone.utc)),
                "source": "delayed" if stale else self.label}

    def bars(self, symbol: str, start: datetime, end: datetime) -> list:
        """Price bars since `start`, used to settle limit orders after the server was asleep."""
        if self.demo:
            if not self.provider.has(symbol):
                raise MarketDataError(f"{symbol} is not in the demo data set.", 404)
            return self.provider.bars(symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        if not self._budget(1):
            raise MarketDataError("Rate limit reached; will catch up on the next check.", 429)
        return self.provider.bars(symbol, start, end)

    def search(self, query: str) -> list:
        query = query.strip()[:40]
        if not query:
            return []
        if self.demo:
            return self.provider.search(query)
        key = ("s", query.lower())
        hit = self.cache.get(key)
        if hit and hit[1] < SEARCH_TTL:
            return hit[0]
        if not self._budget(1):
            raise MarketDataError("Search is paused for a moment: the free-tier rate limit is used up. "
                                  "Try again in a minute.", 429)
        results = self.provider.search(query)
        self.cache.set(key, results)
        return results
