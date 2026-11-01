"""Runs the engine against a real Postgres database. Skipped unless TEST_DATABASE_URL is set,
e.g. TEST_DATABASE_URL=postgres://... python3 -m unittest tests.test_postgres
WARNING: it deletes all rows in the target database; use an empty test database."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

URL = os.environ.get("TEST_DATABASE_URL", "")


@unittest.skipUnless(URL, "set TEST_DATABASE_URL to run Postgres tests")
class PostgresEngineTest(unittest.TestCase):
    def setUp(self):
        from paper.db import PostgresDatabase
        from test_engine import FakeMarket
        from paper.engine import Engine
        self.db = PostgresDatabase(URL)
        with self.db.tx() as c:
            for table in ("transactions", "orders", "holdings", "watchlist", "settings"):
                c.execute(f"DELETE FROM {table}")
        self.db.initialize(10_000_00, [("AAPL", "Apple Inc.")])
        self.market = FakeMarket()
        self.market.set("AAPL", 100.0)
        self.engine = Engine(self.db, self.market)

    def test_trade_cycle(self):
        self.engine.place({"symbol": "AAPL", "side": "buy", "type": "market", "quantity": 10})
        self.market.set("AAPL", 110.0)
        sell = self.engine.place({"symbol": "AAPL", "side": "sell", "type": "market", "quantity": 4})
        self.assertEqual(sell["transaction"]["realized_pl_cents"], 40_00)
        s = self.engine.summary(mark_seen=True)
        self.assertEqual(s["cash_cents"], 10_000_00 - 1000_00 + 440_00)
        self.assertEqual(s["realized_pl_cents"], 40_00)
        self.assertIsInstance(s["realized_pl_cents"], int)
        res = self.engine.place({"symbol": "AAPL", "side": "buy", "type": "limit", "quantity": 1, "limit_price": "50"})
        self.engine.cancel(res["order"]["id"])
        self.engine.watch("AAPL")  # already watched: ON CONFLICT DO NOTHING
        self.assertEqual(len(self.engine.watchlist()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
