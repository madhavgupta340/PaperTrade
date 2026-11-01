"""Configuration loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader. Real environment variables always win over the file."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    database_path: Path
    twelve_data_api_key: str
    provider: str  # "auto" | "twelvedata" | "demo"
    data_label: str  # how Twelve Data quotes are labelled: "live" | "delayed"
    quote_cache_seconds: int
    rate_limit_per_minute: int
    rate_limit_per_day: int
    default_starting_balance: Decimal
    app_password: str
    session_secret: str  # optional; a random one is stored in the database if empty
    database_url: str  # Postgres connection string; empty = local SQLite file
    serverless: bool  # running on Vercel: no background worker, no local disk

    @property
    def use_demo(self) -> bool:
        return self.provider == "demo" or (self.provider == "auto" and not self.twelve_data_api_key)


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"Config error: {name} must be an integer (got {raw!r}).")
    if not lo <= value <= hi:
        raise SystemExit(f"Config error: {name} must be between {lo} and {hi}.")
    return value


def _choice(name: str, default: str, choices: tuple) -> str:
    value = os.environ.get(name, "").strip().lower() or default
    if value not in choices:
        raise SystemExit(f"Config error: {name} must be one of {', '.join(choices)} (got {value!r}).")
    return value


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    provider = _choice("MARKET_DATA_PROVIDER", "auto", ("auto", "twelvedata", "demo"))
    if provider == "twelvedata" and not key:
        raise SystemExit("Config error: MARKET_DATA_PROVIDER=twelvedata requires TWELVE_DATA_API_KEY.")

    raw_balance = os.environ.get("DEFAULT_STARTING_BALANCE", "").strip() or "100000"
    try:
        balance = Decimal(raw_balance)
    except InvalidOperation:
        raise SystemExit("Config error: DEFAULT_STARTING_BALANCE must be a number.")
    if not Decimal("1000") <= balance <= Decimal("10000000"):
        raise SystemExit("Config error: DEFAULT_STARTING_BALANCE must be between 1000 and 10000000.")

    db_path = Path(os.environ.get("DATABASE_PATH", "").strip() or "data/paper_trading.db")
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    host = os.environ.get("HOST", "").strip() or "127.0.0.1"
    password = os.environ.get("APP_PASSWORD", "")
    serverless = bool(os.environ.get("VERCEL"))
    database_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
    if serverless and not password:
        raise SystemExit("Config error: set APP_PASSWORD (12+ characters) in the Vercel project's environment variables.")
    if serverless and not database_url:
        raise SystemExit("Config error: Vercel has no persistent disk. Connect a Postgres database "
                         "(Vercel Storage / Neon) so DATABASE_URL is set.")
    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        raise SystemExit(
            "Config error: HOST is set to accept outside connections, so APP_PASSWORD is required. "
            "Set a strong APP_PASSWORD (12+ characters), or use HOST=127.0.0.1 for local-only use.")
    if password and len(password) < 12:
        raise SystemExit("Config error: APP_PASSWORD must be at least 12 characters.")

    return Config(
        host=host,
        port=_int("PORT", 8000, 1, 65535),
        database_path=db_path,
        twelve_data_api_key=key,
        provider=provider,
        data_label=_choice("MARKET_DATA_LABEL", "live", ("live", "delayed")),
        quote_cache_seconds=_int("QUOTE_CACHE_SECONDS", 60, 10, 3600),
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 8, 1, 10000),
        rate_limit_per_day=_int("RATE_LIMIT_PER_DAY", 800, 1, 10000000),
        default_starting_balance=balance.quantize(Decimal("0.01")),
        app_password=password,
        session_secret=os.environ.get("SESSION_SECRET", "").strip(),
        database_url=database_url,
        serverless=serverless,
    )
