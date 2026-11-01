"""Simulated order engine and portfolio accounting.

Everything here is simulated: no brokerage is contacted and no real order is ever placed.
Money is held in integer cents; prices are Decimals rounded to at most 4 places.
Open limit orders reserve what they need (cash for buys, shares for sells), so the
account can never be over-committed.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .db import get_setting, set_setting
from .market import MarketDataError
from .timeutil import iso_utc, market_day_start_utc, utc_now

log = logging.getLogger(__name__)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MAX_QUANTITY = 1_000_000
MAX_PRICE = Decimal("1000000")
MIN_BALANCE = Decimal("1000")
MAX_BALANCE = Decimal("10000000")
PRICE_MOVE_TOLERANCE = Decimal("0.02")  # reject a market order if the price moved >2% since preview
CATCH_UP_AFTER_SECONDS = 180  # an open order unchecked for this long is settled from price history
AWAY_AFTER_SECONDS = 20 * 60  # show "while you were away" after this long without opening the dashboard
AUTO_FILL_NOTE = "Limit order filled automatically"
AWAY_FILL_NOTE = "Filled while the app was asleep"


class TradingError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


def cents(price, quantity=1) -> int:
    return int((Decimal(str(price)) * Decimal(quantity) * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def fmt_money(amount_cents: int) -> str:
    sign = "-" if amount_cents < 0 else ""
    return f"{sign}${abs(amount_cents) / 100:,.2f}"


def normalize_symbol(raw) -> str:
    symbol = str(raw or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise TradingError("Enter a valid ticker symbol (letters, digits, '.' or '-').", field="symbol")
    return symbol


def parse_quantity(raw) -> int:
    if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
        raise TradingError("Enter a quantity.", field="quantity")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise TradingError("Quantity must be a whole number of shares.", field="quantity")
    if not value.is_finite() or value != value.to_integral_value():
        raise TradingError("Quantity must be a whole number of shares.", field="quantity")
    qty = int(value)
    if qty < 1:
        raise TradingError("Quantity must be at least 1 share.", field="quantity")
    if qty > MAX_QUANTITY:
        raise TradingError(f"Quantity cannot exceed {MAX_QUANTITY:,} shares.", field="quantity")
    return qty


def parse_decimal(raw, field: str, label: str, places: int, lo: Decimal, hi: Decimal) -> Decimal:
    if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
        raise TradingError(f"Enter a {label.lower()}.", field=field)
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except InvalidOperation:
        raise TradingError(f"{label} must be a number.", field=field)
    if not value.is_finite():
        raise TradingError(f"{label} must be a number.", field=field)
    if value.as_tuple().exponent < -places:
        raise TradingError(f"{label} can have at most {places} decimal places.", field=field)
    if value < lo or value > hi:
        raise TradingError(f"{label} must be between ${lo:,} and ${hi:,}.", field=field)
    return value


def parse_order(payload) -> dict:
    if not isinstance(payload, dict):
        raise TradingError("Order must be a JSON object.")
    side = str(payload.get("side", "")).lower()
    if side not in ("buy", "sell"):
        raise TradingError("Side must be 'buy' or 'sell'.", field="side")
    order_type = str(payload.get("type", "")).lower()
    if order_type not in ("market", "limit"):
        raise TradingError("Order type must be 'market' or 'limit'.", field="type")
    order = {
        "symbol": normalize_symbol(payload.get("symbol")),
        "side": side,
        "type": order_type,
        "quantity": parse_quantity(payload.get("quantity")),
        "limit_price": None,
    }
    if order_type == "limit":
        order["limit_price"] = parse_decimal(payload.get("limit_price"), "limit_price", "Limit price",
                                             4, Decimal("0.0001"), MAX_PRICE)
    return order


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def bar_fill_price(side: str, limit: Decimal, bar: dict):
    """Price a resting limit order would have filled at within this bar, or None.

    If the bar opened beyond the limit (a gap), the order fills at the open, which is the
    better price; otherwise it fills at the limit price.
    """
    o, h, l = (Decimal(str(round(bar[k], 4))) for k in ("o", "h", "l"))
    if side == "buy" and l <= limit:
        return min(o, limit)
    if side == "sell" and h >= limit:
        return max(o, limit)
    return None


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


NO_FLOW = {"bought": 0, "sold": 0, "buy_cents": 0, "sell_cents": 0}


def _day_flows(transactions) -> dict:
    """Per-symbol shares bought/sold and cash spent/received in the given (today's) transactions."""
    flows = {}
    for t in transactions:
        f = flows.setdefault(t["symbol"], dict(NO_FLOW))
        if t["side"] == "buy":
            f["bought"] += t["quantity"]
            f["buy_cents"] += t["amount_cents"]
        else:
            f["sold"] += t["quantity"]
            f["sell_cents"] += t["amount_cents"]
    return flows


def _day_change(value_cents: int, shares_now: int, flow: dict, previous_close) -> tuple:
    """Today's P/L for one symbol: value now + today's sales - today's purchases - value at the previous close
    of the shares held at the start of the day. Returns (change_cents, start_value_cents)."""
    start_value = cents(previous_close, shares_now - flow["bought"] + flow["sold"])
    return value_cents + flow["sell_cents"] - flow["buy_cents"] - start_value, start_value


class Engine:
    def __init__(self, db, market):
        self.db = db
        self.market = market
        self._processing = threading.Lock()

    # --- account state ----------------------------------------------------------------
    @staticmethod
    def _state(c) -> dict:
        holdings = {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM holdings")}
        open_orders = [dict(r) for r in c.execute("SELECT * FROM orders WHERE status = 'open' ORDER BY id")]
        reserved_shares = {}
        for o in open_orders:
            if o["side"] == "sell":
                reserved_shares[o["symbol"]] = reserved_shares.get(o["symbol"], 0) + o["quantity"]
        cash = int(get_setting(c, "cash_cents"))
        reserved_cash = sum(o["reserved_cents"] for o in open_orders if o["side"] == "buy")
        return {
            "cash": cash,
            "reserved_cash": reserved_cash,
            "available_cash": cash - reserved_cash,
            "starting": int(get_setting(c, "starting_balance_cents")),
            "reset_at": get_setting(c, "reset_at"),
            "holdings": holdings,
            "open_orders": open_orders,
            "reserved_shares": reserved_shares,
        }

    @staticmethod
    def _available_shares(state: dict, symbol: str) -> int:
        held = state["holdings"].get(symbol, {}).get("shares", 0)
        return held - state["reserved_shares"].get(symbol, 0)

    # --- order evaluation -------------------------------------------------------------
    def _evaluate(self, order: dict, quote: dict, state: dict) -> dict:
        symbol, qty, side = order["symbol"], order["quantity"], order["side"]
        price = Decimal(str(quote["price"]))
        fresh = not quote.get("stale")
        errors, warnings, notes = [], [], []

        if order["type"] == "market":
            marketable, fills_now, est = True, fresh, price
        else:
            limit = order["limit_price"]
            marketable = price <= limit if side == "buy" else price >= limit
            fills_now = marketable and fresh
            est = price if fills_now else limit
        total = cents(est, qty)

        if (quote.get("currency") or "USD") != "USD":
            errors.append("Only US-dollar securities can be traded in this simulator.")

        available_shares = self._available_shares(state, symbol)
        held = state["holdings"].get(symbol, {}).get("shares", 0)
        if side == "buy":
            if total > state["available_cash"]:
                errors.append(f"Not enough buying power. This order needs {fmt_money(total)}, "
                              f"but you have {fmt_money(state['available_cash'])} available.")
            cash_after = state["cash"] - total if fills_now else state["cash"]
            available_cash_after = state["available_cash"] - total
            shares_after = held + qty if fills_now else held
        else:
            if available_shares <= 0:
                if held > 0:
                    errors.append(f"All of your {symbol} shares are already reserved by open sell orders.")
                else:
                    errors.append(f"You don't own any {symbol} shares to sell.")
            elif qty > available_shares:
                errors.append(f"You can sell at most {available_shares:,} share{'s' if available_shares != 1 else ''} "
                              f"of {symbol}.")
            cash_after = state["cash"] + total if fills_now else state["cash"]
            available_cash_after = state["available_cash"] + (total if fills_now else 0)
            shares_after = held - qty if fills_now else held

        if order["type"] == "market" and not fresh:
            errors.append("A fresh price isn't available right now (data rate limit). "
                          "Wait a minute, or place a limit order instead.")
        if order["type"] == "limit" and marketable and not fresh:
            warnings.append("Prices are cached right now, so this limit order will wait for the next fresh price.")
        if quote.get("source") != "demo" and not quote.get("is_market_open"):
            warnings.append("The market is closed. In this simulator, market orders fill right away at the last "
                            "available price; a real broker would wait for the next open.")
        if order["type"] == "limit" and not fills_now:
            verb = "falls to" if side == "buy" else "rises to"
            notes.append(f"This order stays open until {symbol} {verb} ${order['limit_price']:,} or better, "
                         "or until you cancel it.")
            if side == "buy":
                notes.append(f"{fmt_money(total)} of cash will be reserved while the order is open.")
            else:
                notes.append(f"{qty:,} share{'s' if qty != 1 else ''} will be reserved while the order is open.")

        return {
            "price": price,
            "estimated_price": est,
            "fills_now": fills_now,
            "total_cents": total,
            "cash_after_cents": cash_after,
            "available_cash_after_cents": available_cash_after,
            "shares_after": shares_after,
            "available_cash_cents": state["available_cash"],
            "available_shares": available_shares,
            "errors": errors,
            "warnings": warnings,
            "notes": notes,
        }

    def preview(self, payload) -> dict:
        order = parse_order(payload)
        quote = self.market.get_quote(order["symbol"])
        with self.db.read() as c:
            ev = self._evaluate(order, quote, self._state(c))
        return {
            "order": {**order, "limit_price": float(order["limit_price"]) if order["limit_price"] else None},
            "quote": quote,
            "estimate": {
                "price": float(ev["estimated_price"]),
                "fills_now": ev["fills_now"],
                "total_cents": ev["total_cents"],
                "cash_after_cents": ev["cash_after_cents"],
                "available_cash_after_cents": ev["available_cash_after_cents"],
                "shares_after": ev["shares_after"],
                "available_cash_cents": ev["available_cash_cents"],
                "available_shares": ev["available_shares"],
            },
            "errors": ev["errors"],
            "warnings": ev["warnings"],
            "notes": ev["notes"],
            "can_submit": not ev["errors"],
        }

    def place(self, payload) -> dict:
        order = parse_order(payload)
        expected = payload.get("expected_price")
        quote = self.market.get_quote(order["symbol"])
        with self.db.tx() as c:
            ev = self._evaluate(order, quote, self._state(c))
            if ev["errors"]:
                raise TradingError(ev["errors"][0], 422)
            if order["type"] == "market" and expected not in (None, ""):
                exp = parse_decimal(expected, "expected_price", "Expected price", 6, Decimal("0.000001"), MAX_PRICE)
                if abs(ev["price"] - exp) / exp > PRICE_MOVE_TOLERANCE:
                    raise TradingError(
                        f"The price moved from ${exp:,.2f} to ${ev['price']:,.2f} since your preview. "
                        "Please review the order again.", 409)
            now = iso_utc()
            reserved = ev["total_cents"] if order["side"] == "buy" and not ev["fills_now"] else 0
            cur = c.execute(
                "INSERT INTO orders (symbol, name, side, order_type, quantity, limit_price, status, "
                "reserved_cents, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?) RETURNING id",
                (order["symbol"], quote.get("name"), order["side"], order["type"], order["quantity"],
                 float(order["limit_price"]) if order["limit_price"] else None, reserved, now, now),
            )
            order_id = cur.fetchone()["id"]
            txn = self._fill(c, order_id, ev["price"], quote) if ev["fills_now"] else None
            saved = _row(c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        return {"order": saved, "transaction": txn, "filled": txn is not None}

    def _fill(self, c, order_id: int, price: Decimal, quote: dict, at: str | None = None,
              note: str | None = None) -> dict:
        o = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        qty, symbol = o["quantity"], o["symbol"]
        amount = cents(price, qty)
        cash = int(get_setting(c, "cash_cents"))
        h = c.execute("SELECT * FROM holdings WHERE symbol = ?", (symbol,)).fetchone()
        now = at or iso_utc()
        realized = None
        if o["side"] == "buy":
            if amount > cash:
                raise TradingError("Not enough cash to fill this order.", 422)
            cash -= amount
            if h:
                shares = h["shares"] + qty
                avg = (Decimal(str(h["avg_cost"])) * h["shares"] + price * qty) / shares
                c.execute("UPDATE holdings SET shares = ?, avg_cost = ?, updated_at = ? WHERE symbol = ?",
                          (shares, float(avg.quantize(Decimal("0.000001"))), now, symbol))
            else:
                c.execute("INSERT INTO holdings (symbol, name, shares, avg_cost, updated_at) VALUES (?, ?, ?, ?, ?)",
                          (symbol, o["name"], qty, float(price), now))
        else:
            if not h or h["shares"] < qty:
                raise TradingError("Not enough shares to fill this order.", 422)
            realized = amount - cents(h["avg_cost"], qty)
            cash += amount
            remaining = h["shares"] - qty
            if remaining:
                c.execute("UPDATE holdings SET shares = ?, updated_at = ? WHERE symbol = ?", (remaining, now, symbol))
            else:
                c.execute("DELETE FROM holdings WHERE symbol = ?", (symbol,))
        set_setting(c, "cash_cents", cash)
        cur = c.execute(
            "INSERT INTO transactions (order_id, symbol, name, side, quantity, price, amount_cents, "
            "realized_pl_cents, cash_after_cents, data_source, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (order_id, symbol, o["name"], o["side"], qty, float(price), amount, realized, cash,
             quote.get("source"), note, now),
        )
        c.execute("UPDATE orders SET status = 'filled', fill_price = ?, reserved_cents = 0, updated_at = ? "
                  "WHERE id = ?", (float(price), now, order_id))
        txn_id = cur.fetchone()["id"]
        return _row(c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone())

    def process_open_orders(self) -> list:
        """Fill resting limit orders whose price has been reached.

        Orders checked recently are compared with the current quote. Orders that have gone
        unchecked for a while (the server was stopped or asleep) are settled by scanning the
        price bars since their last check, so fills that happened while nothing was running
        are still recorded at the right price and time.
        """
        if not self._processing.acquire(blocking=False):
            return []  # another thread is already doing this
        try:
            return self._process_open_orders()
        finally:
            self._processing.release()

    def _process_open_orders(self) -> list:
        with self.db.read() as c:
            orders = [dict(r) for r in c.execute("SELECT * FROM orders WHERE status = 'open' ORDER BY id")]
        if not orders:
            return []
        now = utc_now()
        filled, live = [], []
        for o in orders:
            last = _parse_iso(o["checked_at"] or o["created_at"])
            if (now - last).total_seconds() > CATCH_UP_AFTER_SECONDS:
                txn = self._catch_up(o, last, now)
                if txn:
                    filled.append(txn)
            else:
                live.append(o)

        if live:
            quotes, _ = self.market.get_quotes(sorted({o["symbol"] for o in live}))
            for o in live:
                q = quotes.get(o["symbol"])
                if not q or q.get("stale"):
                    continue
                price, limit = Decimal(str(q["price"])), Decimal(str(o["limit_price"]))
                if (o["side"] == "buy" and price <= limit) or (o["side"] == "sell" and price >= limit):
                    txn = self._settle(o, price, q, note=AUTO_FILL_NOTE)
                    if txn:
                        filled.append(txn)
                else:
                    self._mark_checked(o["id"], iso_utc(now))
        return filled

    def _catch_up(self, order: dict, since: datetime, now: datetime):
        try:
            bars = self.market.bars(order["symbol"], since, now)
        except MarketDataError as exc:
            log.info("Catch-up for order %s postponed: %s", order["id"], exc.message)
            return None
        limit = Decimal(str(order["limit_price"]))
        for bar in bars:
            price = bar_fill_price(order["side"], limit, bar)
            if price is not None:
                at = datetime.fromtimestamp(bar["t"] / 1000, timezone.utc)
                log.info("Order %s filled from history at %s (%s)", order["id"], price, iso_utc(at))
                return self._settle(order, price, {"source": self.market.label}, at=iso_utc(at), note=AWAY_FILL_NOTE)
        self._mark_checked(order["id"], iso_utc(now))
        return None

    def _settle(self, order: dict, price: Decimal, quote: dict, at: str | None = None, note: str | None = None):
        with self.db.tx() as c:
            current = c.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()
            if not current or current["status"] != "open":
                return None
            try:
                return self._fill(c, order["id"], price, quote, at=at, note=note)
            except TradingError as exc:
                log.warning("Cancelling order %s: %s", order["id"], exc.message)
                c.execute("UPDATE orders SET status = 'cancelled', reserved_cents = 0, updated_at = ? WHERE id = ?",
                          (iso_utc(), order["id"]))
                return None

    def _mark_checked(self, order_id: int, when: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE orders SET checked_at = ? WHERE id = ? AND status = 'open'", (when, order_id))

    def cancel(self, order_id: int) -> dict:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                raise TradingError("Order not found.", 404)
            if row["status"] != "open":
                raise TradingError("Only open orders can be cancelled.", 409)
            c.execute("UPDATE orders SET status = 'cancelled', reserved_cents = 0, updated_at = ? WHERE id = ?",
                      (iso_utc(), order_id))
            return _row(c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    def reset(self, payload) -> dict:
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise TradingError("Confirm the reset to continue.", field="confirm")
        balance = parse_decimal(payload.get("starting_balance"), "starting_balance", "Starting balance",
                                2, MIN_BALANCE, MAX_BALANCE)
        amount = cents(balance)
        with self.db.tx() as c:
            c.execute("DELETE FROM transactions")
            c.execute("DELETE FROM orders")
            c.execute("DELETE FROM holdings")
            set_setting(c, "starting_balance_cents", amount)
            set_setting(c, "cash_cents", amount)
            set_setting(c, "reset_at", iso_utc())
            c.execute("DELETE FROM settings WHERE key IN ('last_seen_at', 'last_seen_value_cents', 'last_seen_txn_id')")
        return self.summary()

    # --- read models -----------------------------------------------------------------
    def summary(self, mark_seen: bool = False) -> dict:
        self.process_open_orders()
        day_start = market_day_start_utc()
        with self.db.read() as c:
            state = self._state(c)
            today = [dict(r) for r in c.execute("SELECT * FROM transactions WHERE created_at >= ?", (day_start,))]
            recent = [dict(r) for r in c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 5")]
            realized_total = c.execute(
                "SELECT CAST(COALESCE(SUM(realized_pl_cents), 0) AS BIGINT) AS s FROM transactions").fetchone()["s"]

        flows = _day_flows(today)

        symbols = sorted(set(state["holdings"]) | set(flows))
        quotes, errors = self.market.get_quotes(symbols) if symbols else ({}, {})

        holdings, holdings_value, day_change, start_positions = [], 0, 0, 0
        day_complete = True
        for symbol in symbols:
            h = state["holdings"].get(symbol)
            f = flows.get(symbol, NO_FLOW)
            q = quotes.get(symbol)
            shares = h["shares"] if h else 0
            price = q["price"] if q else (h["avg_cost"] if h else 0)
            value = cents(price, shares)
            sym_day = None
            if q:
                sym_day, start_value = _day_change(value, shares, f, q["previous_close"])
                day_change += sym_day
                start_positions += start_value
            else:
                day_complete = False
            if not h:
                continue
            cost = cents(h["avg_cost"], shares)
            holdings_value += value
            holdings.append({
                "symbol": symbol,
                "name": h["name"] or (q or {}).get("name") or symbol,
                "shares": shares,
                "reserved_shares": state["reserved_shares"].get(symbol, 0),
                "avg_cost": h["avg_cost"],
                "price": price,
                "price_available": q is not None,
                "change_percent": q["change_percent"] if q else None,
                "market_value_cents": value,
                "cost_basis_cents": cost,
                "unrealized_pl_cents": value - cost,
                "unrealized_pl_percent": ((value - cost) / cost * 100) if cost else 0.0,
                "day_change_cents": sym_day,
                "quote": q,
            })

        portfolio = state["cash"] + holdings_value
        for item in holdings:
            item["allocation_percent"] = item["market_value_cents"] / portfolio * 100 if portfolio else 0.0
        holdings.sort(key=lambda x: -x["market_value_cents"])

        away = self._away_report(portfolio) if mark_seen else None
        cash_at_start = state["cash"] + sum(f["buy_cents"] - f["sell_cents"] for f in flows.values())
        start_value = cash_at_start + start_positions
        total_pl = portfolio - state["starting"]
        return {
            "cash_cents": state["cash"],
            "reserved_cash_cents": state["reserved_cash"],
            "available_cash_cents": state["available_cash"],
            "holdings_value_cents": holdings_value,
            "portfolio_value_cents": portfolio,
            "starting_balance_cents": state["starting"],
            "total_pl_cents": total_pl,
            "total_pl_percent": total_pl / state["starting"] * 100 if state["starting"] else 0.0,
            "realized_pl_cents": realized_total,
            "unrealized_pl_cents": sum(h["unrealized_pl_cents"] for h in holdings),
            "day_change_cents": day_change,
            "day_change_percent": day_change / start_value * 100 if start_value else 0.0,
            "day_change_complete": day_complete,
            "reset_at": state["reset_at"],
            "holdings": holdings,
            "open_orders": state["open_orders"],
            "recent_transactions": recent,
            "quote_errors": {s: e.message for s, e in errors.items()},
            "away": away,
        }

    def _away_report(self, portfolio_cents: int):
        """Compare with the last dashboard visit, then remember this one."""
        now = utc_now()
        with self.db.tx() as c:
            last_seen = get_setting(c, "last_seen_at")
            last_value = get_setting(c, "last_seen_value_cents")
            last_txn = int(get_setting(c, "last_seen_txn_id") or 0)
            max_txn = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM transactions").fetchone()["m"]
            report = None
            if last_seen and (now - _parse_iso(last_seen)).total_seconds() > AWAY_AFTER_SECONDS:
                fills = [dict(r) for r in c.execute(
                    "SELECT t.*, o.limit_price, o.order_type FROM transactions t LEFT JOIN orders o ON o.id = t.order_id "
                    "WHERE t.id > ? AND t.note IS NOT NULL ORDER BY t.id", (last_txn,))]
                previous = int(last_value) if last_value is not None else portfolio_cents
                report = {
                    "since": last_seen,
                    "previous_value_cents": previous,
                    "value_change_cents": portfolio_cents - previous,
                    "value_change_percent": (portfolio_cents - previous) / previous * 100 if previous else 0.0,
                    "fills": fills,
                }
            set_setting(c, "last_seen_at", iso_utc(now))
            set_setting(c, "last_seen_value_cents", portfolio_cents)
            set_setting(c, "last_seen_txn_id", max_txn)
        return report

    def stock(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        quote = self.market.get_quote(symbol)
        self.process_open_orders()
        with self.db.read() as c:
            state = self._state(c)
            txns = [dict(r) for r in c.execute(
                "SELECT * FROM transactions WHERE symbol = ? ORDER BY id DESC LIMIT 20", (symbol,))]
            watched = c.execute("SELECT 1 FROM watchlist WHERE symbol = ?", (symbol,)).fetchone() is not None
            today = [dict(r) for r in c.execute("SELECT * FROM transactions WHERE symbol = ? AND created_at >= ?",
                                                 (symbol, market_day_start_utc()))]
        h = state["holdings"].get(symbol)
        position = None
        if h:
            value, cost = cents(quote["price"], h["shares"]), cents(h["avg_cost"], h["shares"])
            day, _ = _day_change(value, h["shares"], _day_flows(today).get(symbol, NO_FLOW), quote["previous_close"])
            position = {
                "shares": h["shares"],
                "avg_cost": h["avg_cost"],
                "market_value_cents": value,
                "cost_basis_cents": cost,
                "unrealized_pl_cents": value - cost,
                "unrealized_pl_percent": (value - cost) / cost * 100 if cost else 0.0,
                "day_change_cents": day,
            }
        return {
            "quote": quote,
            "position": position,
            "available_cash_cents": state["available_cash"],
            "available_shares": self._available_shares(state, symbol),
            "open_orders": [o for o in state["open_orders"] if o["symbol"] == symbol],
            "transactions": txns,
            "watched": watched,
        }

    def watchlist(self) -> dict:
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM watchlist ORDER BY added_at, symbol")]
        quotes, errors = self.market.get_quotes([r["symbol"] for r in rows]) if rows else ({}, {})
        items = [{**r, "quote": quotes.get(r["symbol"]),
                  "error": errors[r["symbol"]].message if r["symbol"] in errors else None} for r in rows]
        return {"items": items}

    def watch(self, symbol_raw) -> dict:
        symbol = normalize_symbol(symbol_raw)
        name = symbol
        try:
            name = self.market.get_quote(symbol).get("name") or symbol
        except MarketDataError as exc:
            if exc.status == 404:
                raise TradingError(exc.message, 404, field="symbol")
        with self.db.tx() as c:
            c.execute("INSERT INTO watchlist (symbol, name, added_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                      (symbol, name, iso_utc()))
        return {"symbol": symbol, "name": name, "watched": True}

    def unwatch(self, symbol_raw) -> dict:
        symbol = normalize_symbol(symbol_raw)
        with self.db.tx() as c:
            c.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
        return {"symbol": symbol, "watched": False}

    def orders(self, status: str | None = None) -> list:
        with self.db.read() as c:
            if status:
                if status not in ("open", "filled", "cancelled"):
                    raise TradingError("Status must be open, filled or cancelled.", field="status")
                rows = c.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT 1000", (status,))
            else:
                rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1000")
            return [dict(r) for r in rows]

    def transactions(self, symbol: str | None = None) -> list:
        with self.db.read() as c:
            if symbol:
                rows = c.execute("SELECT * FROM transactions WHERE symbol = ? ORDER BY id DESC LIMIT 1000",
                                 (normalize_symbol(symbol),))
            else:
                rows = c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1000")
            return [dict(r) for r in rows]
