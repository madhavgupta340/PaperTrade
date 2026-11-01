"""Engine tests. Run: python3 -m unittest discover -s tests"""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper.db import Database  # noqa: E402
from paper.demo import DemoMarket  # noqa: E402
from paper.engine import AWAY_FILL_NOTE, Engine, TradingError, parse_order  # noqa: E402
from paper.timeutil import iso_utc  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from paper.market import MarketDataError, RateLimiter  # noqa: E402


class FakeMarket:
    """Quotes we control, so fills are deterministic."""

    def __init__(self):
        self.prices = {}
        self.stale = set()
        self.history = {}  # symbol -> list of OHLC bars returned by bars()
        self.label = "demo"

    def bars(self, symbol, start, end):
        return [b for b in self.history.get(symbol, []) if start.timestamp() * 1000 <= b["t"] < end.timestamp() * 1000]

    def set(self, symbol, price, prev=None):
        self.prices[symbol] = (price, prev if prev is not None else price)

    def _quote(self, symbol):
        price, prev = self.prices[symbol]
        return {"symbol": symbol, "name": f"{symbol} Inc.", "price": price, "previous_close": prev,
                "change": price - prev, "change_percent": 0.0, "currency": "USD", "is_market_open": True,
                "stale": symbol in self.stale, "source": "demo", "fetched_at": "2026-01-01T00:00:00.000Z"}

    def get_quote(self, symbol):
        if symbol not in self.prices:
            raise MarketDataError("Symbol not found.", 404)
        return self._quote(symbol)

    def get_quotes(self, symbols):
        out, errors = {}, {}
        for s in symbols:
            if s in self.prices:
                out[s] = self._quote(s)
            else:
                errors[s] = MarketDataError("Symbol not found.", 404)
        return out, errors


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.initialize(10_000_00, [])  # $10,000
        self.market = FakeMarket()
        self.market.set("AAPL", 100.0)
        self.market.set("MSFT", 250.0)
        self.engine = Engine(self.db, self.market)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def order(self, **kw):
        base = {"symbol": "AAPL", "side": "buy", "type": "market", "quantity": 10}
        base.update(kw)
        return self.engine.place(base)

    def test_market_buy_updates_cash_holdings_and_history(self):
        result = self.order()
        self.assertTrue(result["filled"])
        s = self.engine.summary()
        self.assertEqual(s["cash_cents"], 9_000_00)
        self.assertEqual(s["holdings"][0]["shares"], 10)
        self.assertEqual(s["portfolio_value_cents"], 10_000_00)
        self.assertEqual(len(self.engine.transactions()), 1)

    def test_average_cost_and_realized_pl(self):
        self.order(quantity=10)
        self.market.set("AAPL", 120.0)
        self.order(quantity=10)
        s = self.engine.summary()
        self.assertAlmostEqual(s["holdings"][0]["avg_cost"], 110.0)
        self.market.set("AAPL", 130.0)
        sell = self.order(side="sell", quantity=5)
        self.assertEqual(sell["transaction"]["realized_pl_cents"], 100_00)  # (130-110)*5
        s = self.engine.summary()
        self.assertEqual(s["holdings"][0]["shares"], 15)
        self.assertEqual(s["realized_pl_cents"], 100_00)
        self.assertEqual(s["unrealized_pl_cents"], 300_00)  # (130-110)*15
        self.assertEqual(s["total_pl_cents"], 400_00)

    def test_selling_everything_removes_holding(self):
        self.order(quantity=3)
        self.order(side="sell", quantity=3)
        self.assertEqual(self.engine.summary()["holdings"], [])
        self.assertEqual(self.engine.summary()["cash_cents"], 10_000_00)

    def test_rejects_insufficient_cash(self):
        with self.assertRaises(TradingError) as ctx:
            self.order(quantity=101)  # $10,100
        self.assertIn("buying power", ctx.exception.message)
        self.assertEqual(self.engine.summary()["cash_cents"], 10_000_00)

    def test_rejects_overselling_and_selling_unowned(self):
        with self.assertRaises(TradingError):
            self.order(side="sell", quantity=1)
        self.order(quantity=5)
        with self.assertRaises(TradingError):
            self.order(side="sell", quantity=6)

    def test_limit_buy_reserves_cash_then_fills(self):
        res = self.order(type="limit", limit_price="90", quantity=50)  # reserves $4,500
        self.assertFalse(res["filled"])
        s = self.engine.summary()
        self.assertEqual(s["reserved_cash_cents"], 4_500_00)
        self.assertEqual(s["available_cash_cents"], 5_500_00)
        with self.assertRaises(TradingError):  # $6,000 > $5,500 available
            self.order(quantity=60)
        self.market.set("AAPL", 89.5)
        filled = self.engine.process_open_orders()
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["price"], 89.5)  # fills at the better market price
        s = self.engine.summary()
        self.assertEqual(s["reserved_cash_cents"], 0)
        self.assertEqual(s["cash_cents"], 10_000_00 - 4_475_00)

    def test_limit_sell_reserves_shares(self):
        self.order(quantity=10)
        self.order(side="sell", type="limit", limit_price="150", quantity=8)
        with self.assertRaises(TradingError) as ctx:
            self.order(side="sell", quantity=3)
        self.assertIn("at most 2", ctx.exception.message)
        self.market.set("AAPL", 151.0)
        self.engine.process_open_orders()
        self.assertEqual(self.engine.summary()["holdings"][0]["shares"], 2)

    def test_marketable_limit_fills_immediately(self):
        res = self.order(type="limit", limit_price="105")
        self.assertTrue(res["filled"])
        self.assertEqual(res["transaction"]["price"], 100.0)

    def test_cancel_releases_reservation(self):
        res = self.order(type="limit", limit_price="50", quantity=10)
        self.engine.cancel(res["order"]["id"])
        self.assertEqual(self.engine.summary()["reserved_cash_cents"], 0)
        with self.assertRaises(TradingError):
            self.engine.cancel(res["order"]["id"])

    def test_stale_quote_blocks_market_orders_and_fills(self):
        self.market.stale.add("AAPL")
        with self.assertRaises(TradingError):
            self.order()
        res = self.order(type="limit", limit_price="110")  # marketable but stale: rests
        self.assertFalse(res["filled"])
        self.assertEqual(self.engine.process_open_orders(), [])
        self.market.stale.clear()
        self.assertEqual(len(self.engine.process_open_orders()), 1)

    def test_price_moved_since_preview(self):
        with self.assertRaises(TradingError) as ctx:
            self.order(expected_price=90)
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(self.order(expected_price=99.5)["filled"])

    def test_preview_reports_errors_without_side_effects(self):
        p = self.engine.preview({"symbol": "AAPL", "side": "buy", "type": "market", "quantity": 1000})
        self.assertFalse(p["can_submit"])
        self.assertEqual(p["estimate"]["total_cents"], 100_000_00)
        self.assertEqual(self.engine.orders(), [])

    def test_day_change_accounts_for_todays_trades(self):
        self.market.set("AAPL", 100.0, prev=95.0)
        self.order(quantity=10)  # bought today at 100
        self.market.set("AAPL", 102.0, prev=95.0)
        s = self.engine.summary()
        self.assertEqual(s["day_change_cents"], 20_00)  # only the move since purchase
        self.assertEqual(self.engine.stock("AAPL")["position"]["day_change_cents"], 20_00)

    def test_reset(self):
        self.order(quantity=10)
        self.engine.reset({"starting_balance": "25000", "confirm": True})
        s = self.engine.summary()
        self.assertEqual(s["cash_cents"], 25_000_00)
        self.assertEqual(s["starting_balance_cents"], 25_000_00)
        self.assertEqual(s["holdings"], [])
        self.assertEqual(self.engine.transactions(), [])
        with self.assertRaises(TradingError):
            self.engine.reset({"starting_balance": "5", "confirm": True})
        with self.assertRaises(TradingError):
            self.engine.reset({"starting_balance": "5000"})

    def test_order_validation(self):
        bad = [
            {"symbol": "AAPL", "side": "hold", "type": "market", "quantity": 1},
            {"symbol": "AAPL", "side": "buy", "type": "stop", "quantity": 1},
            {"symbol": "AAPL", "side": "buy", "type": "market", "quantity": 1.5},
            {"symbol": "AAPL", "side": "buy", "type": "market", "quantity": 0},
            {"symbol": "AAPL", "side": "buy", "type": "market", "quantity": True},
            {"symbol": "AAPL", "side": "buy", "type": "market", "quantity": "1e9"},
            {"symbol": "AAPL", "side": "buy", "type": "limit", "quantity": 1},
            {"symbol": "AAPL", "side": "buy", "type": "limit", "quantity": 1, "limit_price": "-1"},
            {"symbol": "AAPL", "side": "buy", "type": "limit", "quantity": 1, "limit_price": "1.123456"},
            {"symbol": "AAPL", "side": "buy", "type": "limit", "quantity": 1, "limit_price": "NaN"},
            {"symbol": "<script>", "side": "buy", "type": "market", "quantity": 1},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(TradingError):
                parse_order(payload)
        ok = parse_order({"symbol": " brk.b ", "side": "BUY", "type": "limit", "quantity": "3", "limit_price": "1.25"})
        self.assertEqual((ok["symbol"], ok["quantity"], ok["limit_price"]), ("BRK.B", 3, Decimal("1.25")))


class CatchUpTest(EngineTest):
    """Limit orders settle from price history when the server was asleep."""

    def _age_order(self, order_id, hours=2):
        past = iso_utc(datetime.now(timezone.utc) - timedelta(hours=hours))
        with self.db.tx() as c:
            c.execute("UPDATE orders SET created_at = ?, checked_at = NULL WHERE id = ?", (past, order_id))

    def _bar(self, minutes_ago, o, h, l, c):
        t = int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp() * 1000)
        return {"t": t, "o": o, "h": h, "l": l, "c": c, "seconds": 300}

    def test_fills_at_limit_from_history(self):
        oid = self.order(type="limit", limit_price="90", quantity=10)["order"]["id"]
        self._age_order(oid)
        self.market.history["AAPL"] = [self._bar(90, 99, 100, 95, 96), self._bar(60, 95, 96, 88, 92)]
        filled = self.engine.process_open_orders()
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["price"], 90.0)
        self.assertEqual(filled[0]["note"], AWAY_FILL_NOTE)
        self.assertLess(filled[0]["created_at"], iso_utc())  # recorded at the bar's time
        self.assertEqual(self.engine.summary()["cash_cents"], 10_000_00 - 900_00)

    def test_gap_fills_at_better_open(self):
        oid = self.order(side="buy", type="limit", limit_price="90", quantity=1)["order"]["id"]
        self._age_order(oid)
        self.market.history["AAPL"] = [self._bar(30, 85, 86, 84, 85)]
        self.assertEqual(self.engine.process_open_orders()[0]["price"], 85.0)

    def test_sell_from_history_and_no_cross(self):
        self.order(quantity=10)
        oid = self.order(side="sell", type="limit", limit_price="120", quantity=10)["order"]["id"]
        self._age_order(oid)
        self.market.history["AAPL"] = [self._bar(30, 100, 119, 99, 110)]
        self.assertEqual(self.engine.process_open_orders(), [])
        with self.db.read() as c:
            self.assertIsNotNone(c.execute("SELECT checked_at FROM orders WHERE id = ?", (oid,)).fetchone()[0])
        self._age_order(oid)
        self.market.history["AAPL"] = [self._bar(20, 118, 125, 117, 124)]
        self.assertEqual(self.engine.process_open_orders()[0]["price"], 120.0)

    def test_away_report(self):
        self.assertIsNone(self.engine.summary(mark_seen=True)["away"])  # first visit
        oid = self.order(type="limit", limit_price="90", quantity=10)["order"]["id"]
        self._age_order(oid)
        self.market.history["AAPL"] = [self._bar(30, 92, 93, 89, 91)]
        with self.db.tx() as c:
            c.execute("UPDATE settings SET value = ? WHERE key = 'last_seen_at'",
                      (iso_utc(datetime.now(timezone.utc) - timedelta(hours=3)),))
        self.market.set("AAPL", 95.0)
        away = self.engine.summary(mark_seen=True)["away"]
        self.assertEqual(len(away["fills"]), 1)
        self.assertEqual(away["value_change_cents"], 50_00)  # 10 shares bought at 90, now 95
        self.assertIsNone(self.engine.summary(mark_seen=True)["away"])  # shown once


class DemoMarketTest(unittest.TestCase):
    def test_deterministic_and_continuous(self):
        a, b = DemoMarket(), DemoMarket()
        self.assertEqual(a.quote("AAPL")["previous_close"], b.quote("AAPL")["previous_close"])
        for rng in ("1D", "1W", "1M", "6M", "1Y", "5Y"):
            pts = a.history("MSFT", rng)
            self.assertGreater(len(pts), 1, rng)
            self.assertTrue(all(p["c"] > 0 for p in pts))
            self.assertEqual(pts, sorted(pts, key=lambda p: p["t"]))

    def test_bars_cover_window(self):
        m = DemoMarket()
        end = datetime.now(timezone.utc)
        bars = m.bars("AAPL", int((end - timedelta(hours=2)).timestamp() * 1000), int(end.timestamp() * 1000))
        self.assertGreaterEqual(len(bars), 8)  # demo days are 24h, so DST-change days leave a 1-hour gap
        self.assertTrue(all(b["l"] <= min(b["o"], b["c"]) and b["h"] >= max(b["o"], b["c"]) for b in bars))
        old = m.bars("AAPL", int((end - timedelta(days=60)).timestamp() * 1000),
                     int((end - timedelta(days=50)).timestamp() * 1000))
        self.assertTrue(old and all(b["seconds"] == 86400 for b in old))

    def test_search(self):
        m = DemoMarket()
        self.assertEqual(m.search("aapl")[0]["symbol"], "AAPL")
        self.assertIn("MSFT", [r["symbol"] for r in m.search("micro")])
        self.assertEqual(m.search("zzzz"), [])


class RateLimiterTest(unittest.TestCase):
    def test_budget(self):
        rl = RateLimiter(per_minute=8, per_day=10)
        self.assertEqual(rl.acquire_up_to(5), 5)
        self.assertEqual(rl.acquire_up_to(5), 3)
        self.assertEqual(rl.acquire_up_to(1), 0)
        self.assertEqual(rl.snapshot()["used_today"], 8)


if __name__ == "__main__":
    unittest.main()
