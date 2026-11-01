#!/usr/bin/env python3
"""PaperTrade server: JSON API + static UI as one WSGI app.

Simulated money only. This server never connects to a brokerage or places real orders.
Run locally:  python3 server.py
On Vercel, the module-level `app` below is the entrypoint.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from paper.auth import COOKIE_NAME, SESSION_SECONDS, Auth
from paper.config import load_config
from paper.db import get_setting, open_database, set_setting
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
SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
     "font-src https://fonts.gstatic.com; img-src 'self' data:; "
     "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
]
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type",
           422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error",
           502: "Bad Gateway", 503: "Service Unavailable"}
DEFAULT_WATCHLIST = [("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"),
                     ("NVDA", "NVIDIA Corporation"), ("SPY", "SPDR S&P 500 ETF Trust")]

log = logging.getLogger("papertrade")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.message, self.status, self.field = message, status, field


class Request:
    """The parts of a WSGI environ the app needs."""

    def __init__(self, environ: dict):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO") or "/"
        self.query = parse_qs(environ.get("QUERY_STRING", ""))

    def header(self, name: str):
        key = "HTTP_" + name.upper().replace("-", "_")
        if name.lower() == "content-type":
            key = "CONTENT_TYPE"
        elif name.lower() == "content-length":
            key = "CONTENT_LENGTH"
        return self.environ.get(key)

    @property
    def https(self) -> bool:
        return self.header("X-Forwarded-Proto") == "https" or self.environ.get("wsgi.url_scheme") == "https"

    @property
    def client(self) -> str:
        # Fly.io and Vercel put the real client address in these headers.
        return (self.header("Fly-Client-IP") or self.header("X-Real-IP")
                or self.environ.get("REMOTE_ADDR") or "unknown")

    def read_body(self) -> bytes:
        try:
            length = int(self.header("Content-Length") or 0)
        except ValueError:
            raise ApiError("Invalid Content-Length.", 400)
        if length > MAX_BODY_BYTES:
            raise ApiError("Request body is too large.", 413)
        return self.environ["wsgi.input"].read(length) if length else b""


class App:
    def __init__(self, config):
        self.config = config
        self.db = open_database(config.database_path, config.database_url)
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

    # --- API handlers ---------------------------------------------------------------------
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

    # --- WSGI -----------------------------------------------------------------------------
    def __call__(self, environ, start_response):
        req = Request(environ)
        head = req.method == "HEAD"
        cookie = None
        if req.path == "/healthz":
            status, ctype, body, cache = 200, "text/plain; charset=utf-8", b"ok", "no-store"
        elif req.path.startswith("/api/"):
            status, payload, cookie = self._api(req)
            ctype, cache = "application/json; charset=utf-8", "no-store"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        elif req.method in ("GET", "HEAD"):
            status, ctype, body = self._static(req.path)
            cache = "no-cache"
        else:
            status, ctype, cache = 405, "application/json; charset=utf-8", "no-store"
            body = b'{"error":{"message":"Method not allowed."}}'

        headers = [("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", cache), *SECURITY_HEADERS]
        if cookie is not None:
            value, max_age = cookie
            secure = "; Secure" if req.https else ""
            headers.append(("Set-Cookie", f"{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"))
        if req.https:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        start_response(f"{status} {REASONS.get(status, 'OK')}", headers)
        return [b"" if head else body]

    def _api(self, req: Request):
        started = time.monotonic()
        method = "GET" if req.method == "HEAD" else req.method
        cookie = None
        try:
            body = None
            if method in ("POST", "DELETE"):
                self._check_origin(req)
            if method == "POST":
                body = self._read_json(req)
            if req.path in PUBLIC_API:
                payload, cookie = self._session_route(req, method, body)
            elif self.auth.enabled and not self._signed_in(req):
                raise ApiError("Please sign in.", 401, "auth")
            else:
                payload = self.dispatch(method, req.path, req.query, body)
            status = 200
        except (ApiError, TradingError, MarketDataError) as exc:
            status = exc.status
            payload = {"error": {"message": exc.message, "field": getattr(exc, "field", None)}}
        except Exception:  # never leak internals to the client
            log.exception("Unhandled error for %s %s", method, req.path)
            status, payload = 500, {"error": {"message": "Unexpected server error. Check the server log."}}
        log.info("%s %s -> %s (%.0f ms)", method, req.path, status, (time.monotonic() - started) * 1000)
        return status, payload, cookie

    def _signed_in(self, req: Request) -> bool:
        try:
            morsel = SimpleCookie(req.header("Cookie") or "").get(COOKIE_NAME)
        except CookieError:
            return False
        return self.auth.verify(morsel.value if morsel else None)

    def _session_route(self, req: Request, method: str, body):
        if req.path == "/api/session" and method == "GET":
            return {"auth_required": self.auth.enabled, "authenticated": not self.auth.enabled or self._signed_in(req)}, None
        if req.path == "/api/login" and method == "POST":
            if not self.auth.enabled:
                return {"authenticated": True}, None
            if not self.auth.allow_attempt(req.client):
                raise ApiError("Too many sign-in attempts. Wait 15 minutes and try again.", 429)
            if not self.auth.check_password((body or {}).get("password")):
                time.sleep(0.4)  # slow down guessing
                raise ApiError("That password isn't right.", 401, "password")
            log.info("Signed in from %s", req.client)
            return {"authenticated": True}, (self.auth.issue(), SESSION_SECONDS)
        if req.path == "/api/logout" and method == "POST":
            return {"authenticated": False}, ("", 0)
        raise ApiError("Method not allowed.", 405)

    @staticmethod
    def _check_origin(req: Request):
        # Block cross-site writes: browsers always send Origin on cross-origin POST/DELETE.
        origin = req.header("Origin")
        if origin and urlparse(origin).netloc != req.header("Host"):
            raise ApiError("Cross-origin requests are not allowed.", 403)

    @staticmethod
    def _read_json(req: Request):
        if (req.header("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError("Content-Type must be application/json.", 415)
        raw = req.read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("Request body must be valid JSON.", 400)

    @staticmethod
    def _static(path: str):
        rel = unquote(path).lstrip("/") or "index.html"
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            target = None
        if target is None or not target.is_file():
            return 404, "text/plain; charset=utf-8", b"Not found"
        return 200, CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"), target.read_bytes()


def _param(query: dict, name: str):
    values = query.get(name)
    return values[0].strip() if values else None


class _LazyApp:
    """Builds the App on the first request, so importing this module (as Vercel does) has no side
    effects, and a configuration error becomes a readable page instead of a crashed function."""

    def __init__(self):
        self._app = None
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):
        if self._app is None:
            with self._lock:
                if self._app is None:
                    try:
                        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
                        self._app = App(load_config())
                    except SystemExit as exc:  # configuration problems are reported as SystemExit
                        body = f"PaperTrade is not configured yet.\n\n{exc}\n".encode("utf-8")
                        start_response("503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8"),
                                                                   ("Content-Length", str(len(body)))])
                        return [body]
        return self._app(environ, start_response)


app = _LazyApp()  # WSGI entrypoint (Vercel and any WSGI server)


# --- local server ---------------------------------------------------------------------------
class _ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):  # requests are already logged by the app
        pass


def start_order_worker(engine: Engine, interval: float) -> None:
    """Checks resting limit orders in the background so they fill even when no page is open."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                for txn in engine.process_open_orders():
                    log.info("Limit order filled: %s %s %s @ %s", txn["side"], txn["quantity"], txn["symbol"], txn["price"])
            except Exception:
                log.exception("Order worker error")
    threading.Thread(target=loop, name="order-worker", daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    application = App(config)
    server = make_server(config.host, config.port, application, server_class=_ThreadingServer, handler_class=_QuietHandler)
    start_order_worker(application.engine, interval=float(application.market.quote_ttl))

    mode = ("DEMO DATA - simulated prices (no TWELVE_DATA_API_KEY set)" if application.market.demo
            else f"Twelve Data ({config.data_label}), refresh every {config.quote_cache_seconds}s")
    url = f"http://{'localhost' if config.host in ('127.0.0.1', '0.0.0.0') else config.host}:{config.port}"
    login = "password required" if application.auth.enabled else "off (local only)"
    storage = "Postgres (DATABASE_URL)" if config.database_url else str(config.database_path)
    print(f"\n  PaperTrade - simulated money only\n  Market data: {mode}\n  Database:    {storage}\n"
          f"  Sign-in:     {login}\n  Open:        {url}\n  Stop:        Ctrl+C\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
