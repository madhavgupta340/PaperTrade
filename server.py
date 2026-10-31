#!/usr/bin/env python3
"""PaperTrade server: JSON API + static UI. Python standard library only.

Simulated money only. This server never connects to a brokerage or places real orders.
Run:  python3 server.py
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from http.cookies import CookieError, SimpleCookie

from paper.auth import COOKIE_NAME, SESSION_SECONDS, Auth
from paper.config import load_config
from paper.db import Database, get_setting, set_setting
from paper.engine import Engine, TradingError, cents, normalize_symbol
from paper.market import MarketData, MarketDataError
from paper.timeutil import iso_utc

STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()
MAX_BODY_BYTES = 16 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}
PUBLIC_API = {"/api/session", "/api/login", "/api/logout"}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
DEFAULT_WATCHLIST = [("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"),
                     ("NVDA", "NVIDIA Corporation"), ("SPY", "SPDR S&P 500 ETF Trust")]

log = logging.getLogger("papertrade")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.message, self.status, self.field = message, status, field


class App:
    def __init__(self, config):
        self.config = config
        self.db = Database(config.database_path)
        self.db.initialize(cents(config.default_starting_balance), DEFAULT_WATCHLIST)
        self.market = MarketData(config)
        self.engine = Engine(self.db, self.market)
        self.auth = Auth(config.app_password, config.session_secret or self._stored_secret())
        routes = [
            ("GET", r"/api/status", self.status),
            ("GET", r"/api/account", lambda m, q, b: self.engine.summary(mark_seen=_param(q, "seen") == "1")),
            ("POST", r"/api/account/reset", lambda m, q, b: self.engine.reset(b)),
            ("GET", r"/api/search", self.search),
            ("GET", r"/api/quotes", self.quotes),
            ("GET", r"/api/stocks/(?P<symbol>[^/]+)", lambda m, q, b: self.engine.stock(m["symbol"])),
            ("GET", r"/api/stocks/(?P<symbol>[^/]+)/history", self.history),
            ("GET", r"/api/watchlist", lambda m, q, b: self.engine.watchlist()),
            ("POST", r"/api/watchlist", lambda m, q, b: self.engine.watch((b or {}).get("symbol"))),
            ("DELETE", r"/api/watchlist/(?P<symbol>[^/]+)", lambda m, q, b: self.engine.unwatch(m["symbol"])),
            ("GET", r"/api/orders", lambda m, q, b: {"orders": self.engine.orders(_param(q, "status"))}),
            ("POST", r"/api/orders/preview", lambda m, q, b: self.engine.preview(b)),
            ("POST", r"/api/orders", lambda m, q, b: self.engine.place(b)),
            ("POST", r"/api/orders/(?P<order_id>\d+)/cancel", lambda m, q, b: self.engine.cancel(int(m["order_id"]))),
            ("GET", r"/api/transactions",
             lambda m, q, b: {"transactions": self.engine.transactions(_param(q, "symbol"))}),
        ]
        self.routes = [(method, re.compile(pattern), fn) for method, pattern, fn in routes]

    def _stored_secret(self) -> str:
        """A random signing secret kept in the database, so sessions survive restarts."""
        with self.db.tx() as c:
            secret = get_setting(c, "session_secret")
            if not secret:
                secret = secrets.token_hex(32)
                set_setting(c, "session_secret", secret)
            return secret

    def status(self, m, q, b):
        return {**self.market.status(), "server_time": iso_utc(), "simulated_money_only": True,
                "auth_enabled": self.auth.enabled,
                "default_starting_balance_cents": cents(self.config.default_starting_balance)}

    def search(self, m, q, b):
        query = _param(q, "q") or ""
        if len(query) > 40:
            raise ApiError("Search text is too long.", 400, "q")
        results = self.market.search(query)
        # Attach prices for the top results; in live mode this is capped to protect the free-tier budget.
        limit = 8 if self.market.demo else 3
        quotes, _ = self.market.get_quotes([r["symbol"] for r in results[:limit]])
        return {"results": [{**r, "quote": quotes.get(r["symbol"])} for r in results]}

    def quotes(self, m, q, b):
        raw = _param(q, "symbols") or ""
        symbols = [normalize_symbol(s) for s in raw.split(",") if s.strip()][:25]
        quotes, errors = self.market.get_quotes(symbols)
        return {"quotes": quotes, "errors": {s: e.message for s, e in errors.items()}}

    def history(self, m, q, b):
        return self.market.history(normalize_symbol(m["symbol"]), (_param(q, "range") or "1M").upper())

    def dispatch(self, method: str, path: str, query: dict, body):
        allowed = False
        for route_method, pattern, fn in self.routes:
            match = pattern.fullmatch(path)
            if match:
                if route_method == method:
                    return fn({k: unquote(v) for k, v in match.groupdict().items()}, query, body)
                allowed = True
        if allowed:
            raise ApiError("Method not allowed.", 405)
        raise ApiError("Not found.", 404)


def _param(query: dict, name: str):
    values = query.get(name)
    return values[0].strip() if values else None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperTrade/1.0"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("HEAD")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

        def _handle(self, method: str):
            self._cookie_out = None
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send(200, "text/plain; charset=utf-8", b"ok", head=method == "HEAD")
            elif parsed.path.startswith("/api/"):
                self._api(method, parsed.path, parse_qs(parsed.query))
            elif method in ("GET", "HEAD"):
                self._static(parsed.path, head=method == "HEAD")
            else:
                self._json(405, {"error": {"message": "Method not allowed."}})

        def _api(self, method: str, path: str, query: dict):
            started = time.monotonic()
            try:
                body = None
                if method in ("POST", "DELETE"):
                    self._check_origin()
                if method == "POST":
                    body = self._read_json()
                if path in PUBLIC_API:
                    status, payload = 200, self._session_route(method, path, body)
                elif app.auth.enabled and not self._signed_in():
                    raise ApiError("Please sign in.", 401, "auth")
                else:
                    status, payload = 200, app.dispatch("GET" if method == "HEAD" else method, path, query, body)
            except (ApiError, TradingError, MarketDataError) as exc:
                status = exc.status
                payload = {"error": {"message": exc.message, "field": getattr(exc, "field", None)}}
            except Exception:  # never leak internals to the client
                log.exception("Unhandled error for %s %s", method, path)
                status, payload = 500, {"error": {"message": "Unexpected server error. Check the server log."}}
            log.info("%s %s -> %s (%.0f ms)", method, path, status, (time.monotonic() - started) * 1000)
            self._json(status, payload, head=method == "HEAD")

        def _client(self) -> str:
            # Fly.io sets Fly-Client-IP; fall back to the socket address.
            return self.headers.get("Fly-Client-IP") or self.client_address[0]

        def _signed_in(self) -> bool:
            try:
                morsel = SimpleCookie(self.headers.get("Cookie") or "").get(COOKIE_NAME)
            except CookieError:
                return False
            return app.auth.verify(morsel.value if morsel else None)

        def _session_route(self, method: str, path: str, body):
            if path == "/api/session" and method == "GET":
                return {"auth_required": app.auth.enabled, "authenticated": not app.auth.enabled or self._signed_in()}
            if path == "/api/login" and method == "POST":
                if not app.auth.enabled:
                    return {"authenticated": True}
                if not app.auth.allow_attempt(self._client()):
                    raise ApiError("Too many sign-in attempts. Wait 15 minutes and try again.", 429)
                if not app.auth.check_password((body or {}).get("password")):
                    time.sleep(0.4)  # slow down guessing
                    raise ApiError("That password isn't right.", 401, "password")
                self._cookie_out = (app.auth.issue(), SESSION_SECONDS)
                log.info("Signed in from %s", self._client())
                return {"authenticated": True}
            if path == "/api/logout" and method == "POST":
                self._cookie_out = ("", 0)
                return {"authenticated": False}
            raise ApiError("Method not allowed.", 405)

        def _check_origin(self):
            # Block cross-site writes: browsers always send Origin on cross-origin POST/DELETE.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise ApiError("Cross-origin requests are not allowed.", 403)

        def _read_json(self):
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise ApiError("Content-Type must be application/json.", 415)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError("Invalid Content-Length.", 400)
            if length > MAX_BODY_BYTES:
                raise ApiError("Request body is too large.", 413)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ApiError("Request body must be valid JSON.", 400)

        def _send(self, status: int, content_type: str, data: bytes, head: bool = False, cache: str = "no-store"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            cookie = getattr(self, "_cookie_out", None)
            if cookie is not None:
                value, max_age = cookie
                secure = "; Secure" if self._is_https() else ""
                self.send_header("Set-Cookie", f"{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}")
            if self._is_https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _is_https(self) -> bool:
            return self.headers.get("X-Forwarded-Proto") == "https"

        def _json(self, status: int, payload, head: bool = False):
            self._send(status, "application/json; charset=utf-8",
                       json.dumps(payload, separators=(",", ":")).encode("utf-8"), head)

        def _static(self, path: str, head: bool = False):
            rel = unquote(path).lstrip("/") or "index.html"
            target = (STATIC_DIR / rel).resolve()
            try:
                target.relative_to(STATIC_DIR)
            except ValueError:
                target = None
            if target is None or not target.is_file():
                self._send(404, "text/plain; charset=utf-8", b"Not found", head)
                return
            ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, ctype, target.read_bytes(), head, cache="no-cache")

    return Handler


def start_order_worker(app: App, interval: float) -> None:
    """Checks resting limit orders in the background so they fill even when no page is open."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                for txn in app.engine.process_open_orders():
                    log.info("Limit order filled: %s %s %s @ %s", txn["side"], txn["quantity"], txn["symbol"], txn["price"])
            except Exception:
                log.exception("Order worker error")
    threading.Thread(target=loop, name="order-worker", daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    app = App(config)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(app))
    server.daemon_threads = True
    start_order_worker(app, interval=float(app.market.quote_ttl))

    mode = ("DEMO DATA - simulated prices (no TWELVE_DATA_API_KEY set)" if app.market.demo
            else f"Twelve Data ({config.data_label}), refresh every {config.quote_cache_seconds}s")
    url = f"http://{'localhost' if config.host in ('127.0.0.1', '0.0.0.0') else config.host}:{config.port}"
    login = "password required" if app.auth.enabled else "off (local only)"
    print(f"\n  PaperTrade - simulated money only\n  Market data: {mode}\n  Database:    {config.database_path}\n"
          f"  Sign-in:     {login}\n  Open:        {url}\n  Stop:        Ctrl+C\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
