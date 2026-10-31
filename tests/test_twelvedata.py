"""Twelve Data adapter tests with stubbed HTTP responses (no network, no key needed)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper.market import MarketDataError, TwelveDataProvider  # noqa: E402

QUOTE = {"symbol": "AAPL", "name": "Apple Inc", "exchange": "NASDAQ", "currency": "USD",
         "datetime": "2026-10-30", "timestamp": 1793390400, "open": "230.1", "high": "233.0",
         "low": "229.5", "close": "232.5", "volume": "41000000", "previous_close": "230.0",
         "change": "2.5", "percent_change": "1.08696", "is_market_open": False,
         "fifty_two_week": {"low": "180.0", "high": "260.0"}}


class StubProvider(TwelveDataProvider):
    def __init__(self, response):
        super().__init__("test-key")
        self.response = response
        self.calls = []

    def _get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if isinstance(self.response, dict) and self.response.get("status") == "error":
            raise self._error(self.response)
        return self.response


class TwelveDataTest(unittest.TestCase):
    def test_single_quote(self):
        p = StubProvider(QUOTE)
        out, errors = p.quotes(["AAPL"])
        q = out["AAPL"]
        self.assertEqual((q["price"], q["previous_close"], q["change"]), (232.5, 230.0, 2.5))
        self.assertFalse(q["is_market_open"])
        self.assertEqual(q["week52_high"], 260.0)
        self.assertEqual(errors, {})

    def test_batch_quote_with_per_symbol_error(self):
        p = StubProvider({"AAPL": QUOTE, "ZZZZ": {"code": 404, "message": "not found", "status": "error"}})
        out, errors = p.quotes(["AAPL", "ZZZZ"])
        self.assertIn("AAPL", out)
        self.assertEqual(errors["ZZZZ"].status, 404)
        self.assertEqual(p.calls[0][1]["symbol"], "AAPL,ZZZZ")

    def test_rate_limit_error(self):
        p = StubProvider({"code": 429, "message": "You have run out of API credits", "status": "error"})
        with self.assertRaises(MarketDataError) as ctx:
            p.quotes(["AAPL"])
        self.assertEqual(ctx.exception.status, 429)

    def test_history_is_ascending_and_trimmed_to_last_session(self):
        values = [{"datetime": "2026-10-30 15:55:00", "close": "232.5"},
                  {"datetime": "2026-10-30 15:50:00", "close": "232.0"},
                  {"datetime": "2026-10-29 15:55:00", "close": "229.0"}]
        p = StubProvider({"meta": {"exchange_timezone": "America/New_York"}, "values": values, "status": "ok"})
        pts = p.history("AAPL", "1D")
        self.assertEqual([x["c"] for x in pts], [232.0, 232.5])

    def test_search_filters_to_us_listings(self):
        p = StubProvider({"data": [
            {"symbol": "AAPL", "instrument_name": "Apple Inc", "exchange": "NASDAQ", "country": "United States", "currency": "USD", "instrument_type": "Common Stock"},
            {"symbol": "AAPL", "instrument_name": "Apple Inc", "exchange": "BMV", "country": "Mexico", "currency": "MXN"},
            {"symbol": "APC", "instrument_name": "Apple Inc", "exchange": "XETR", "country": "Germany", "currency": "EUR"},
        ]})
        self.assertEqual([r["symbol"] for r in p.search("apple")], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
