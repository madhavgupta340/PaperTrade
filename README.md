# PaperTrade

A paper-trading app for practising stock trades with **simulated money** and live or demo market data.

**Live:** [papertrade-viue.vercel.app](https://papertrade-viue.vercel.app). This is my personal instance, so it's password-protected and you'll only see the sign-in page.
To try it yourself, run it locally (no setup, demo prices) or deploy your own copy (see [Host it on Vercel](#host-it-on-vercel-free-works-from-your-phone)).

> **Simulated money only.** PaperTrade never connects to a brokerage and never places real orders.
> Nothing in it is investment advice.

## Features

- **Dashboard:** portfolio value, available cash (and cash reserved by open orders), total P/L vs. your starting balance, and today's change.
- **Search** by ticker or company name, with price and daily change. Use the keyboard: press `/` to focus, arrow keys to move, Enter to open.
- **Watchlist:** add symbols from the dashboard or with the ★ button on a stock page.
- **Stock pages:** an interactive price chart (1D, 1W, 1M, 6M, 1Y, 5Y; hover, touch or arrow keys), key stats, and your position.
- **Market and limit orders** (buy/sell) with an **order preview** before you confirm.
  - Open limit buys reserve cash and open limit sells reserve shares, so you can't over-commit.
  - A market order is re-checked when you submit. If the price moved more than 2% since the preview, you're asked to review it again.
  - Open limit orders can be cancelled at any time.
- **History:** transactions and orders, filterable by symbol, with CSV export. Holdings show per-position and realized P/L.
- **Reset** the account to any starting balance from $1,000 to $10,000,000.
- **Data status** is always visible: Live, Delayed or Demo, plus the last update time.

## Run it

Requirements: **Python 3.9+**. That's all: the app uses only the standard library (no `pip install`, no Node).

```bash
cp .env.example .env        # optional; without a key the app runs in demo mode
python3 server.py
```

Open <http://localhost:8000>.

Run the tests:

```bash
python3 -m unittest discover -s tests
```

## Host it on Vercel (free, works from your phone)

Vercel runs the app as a serverless function, connected to your GitHub repo, with a free Postgres database for your account.
- Nothing runs between visits.
- Limit orders that should have filled meanwhile are settled from price history the next time you open the app.
- Every `git push` redeploys automatically.

1. Sign in at <https://vercel.com> with GitHub. The free **Hobby** plan is enough.
2. **Add New → Project**, import `papertrade`, and keep the detected settings. Don't deploy yet, or let the first deploy fail; that's fine.
3. **Storage → Create Database → Neon (Postgres)**, then connect it to the project. This sets `DATABASE_URL` for you.
4. **Settings → Environment Variables**, then add:
   - `APP_PASSWORD`: a long password (12+ characters). It's what protects the app on the internet.
   - `TWELVE_DATA_API_KEY` (optional): leave it out to stay in demo mode.
5. **Deployments → Redeploy**. Open the `*.vercel.app` URL on your phone, sign in, and use **Add to Home Screen**.

If something is missing, the site shows a plain "not configured yet" message naming the missing setting.
Locally nothing changes: `python3 server.py` still uses a SQLite file and needs no installs.
To run against Postgres locally, `pip install -r requirements.txt` and set `DATABASE_URL`.

**Vercel notes**
- On the free Hobby plan, Vercel only deploys commits whose author email belongs to your GitHub account. Other commits show as "Blocked".
  Use your GitHub noreply address for this repo:
  `git config user.email "<id>+<username>@users.noreply.github.com"` (find it under GitHub → Settings → Emails).
- Price caches live in memory per function instance. On a cold start the first page load fetches fresh quotes, which uses a few extra API credits.
- The free-tier request budget is also tracked per instance. If several instances run at once, Twelve Data's own rate limit applies, and the app shows cached prices marked **Delayed**.

## Alternative: Fly.io (always-on container)

The app doesn't need to run all the time. Positions are valued at current prices whenever you open it.
Limit orders that should have filled while it was off are settled from price history when it wakes:
- each order fills at its limit price, or at a better opening price if the market gapped past it
- the fill is recorded at the time it actually happened
- the dashboard shows a **While you were away** summary

That makes it a good fit for a host that **stops the app when idle and starts it on the next visit**.
The recommended host is [Fly.io](https://fly.io): scale-to-zero machines plus a persistent volume for the SQLite database.
Fly bills by usage and needs a card on file. A stopped 256 MB machine with a 1 GB volume costs very little, but check
[current pricing](https://fly.io/docs/about/pricing/).

1. Install the CLI and sign in: <https://fly.io/docs/flyctl/install/>, then `fly auth login`.
2. From this folder, create the app without deploying yet:
   ```bash
   fly launch --no-deploy --copy-config
   ```
   Pick a unique app name and the region nearest you (it updates `fly.toml`).
3. Create the volume that holds your account, in the same region:
   ```bash
   fly volumes create papertrade_data --size 1
   ```
4. Set secrets. Use a long password; it's the only thing protecting the app.
   ```bash
   fly secrets set APP_PASSWORD='a-long-unique-password' TWELVE_DATA_API_KEY='your-key'
   ```
   Leave out the key to stay in demo mode.
5. Deploy:
   ```bash
   fly deploy
   ```
6. Open `https://<your-app>.fly.dev` on your phone, sign in, then use **Share → Add to Home Screen** (iOS) or
   **Install app** (Android) to get an app icon.

The first visit after a quiet spell takes a few seconds while the machine starts.
Other hosts work too: the `Dockerfile` runs anywhere that provides a persistent disk mounted at `/data`.
Hosts whose free tier has no persistent disk, such as Render's free plan, lose your account on every restart.

**Security when deployed**
- `APP_PASSWORD` (12+ characters) is required whenever `HOST` isn't `127.0.0.1`; the server refuses to start without it.
- Sign-in uses a signed, HttpOnly, SameSite cookie that's valid for 30 days. Password guesses are throttled.
- Changing `APP_PASSWORD` signs out every device.
- The server runs behind Fly's HTTPS proxy.

## Market data

The app uses [Twelve Data](https://twelvedata.com/docs). As of October 2026, the free **Basic** plan provides:

- 8 API credits per minute and 800 per day (1 credit per symbol per request)
- US equities and ETFs, listed as real-time on the pricing page
- `/quote`, `/time_series` and `/symbol_search` endpoints

To enable real data:

1. Create a free account at <https://twelvedata.com/pricing> and copy your API key.
2. Put `TWELVE_DATA_API_KEY=...` in `.env`.
3. Restart `python3 server.py`. The startup banner shows which mode is active.

The key is read from the environment on the server and sent to Twelve Data in an `Authorization` header. The browser never sees it.

### Demo mode

With no key set, the app runs in **demo mode**. A banner and "Demo" badges make this clear.

- Demo prices are **fictional**. They are deterministic random walks for 36 popular tickers (AAPL, MSFT, NVDA, SPY…).
- Prices change every 15 seconds.
- The demo market trades around the clock.

### Limitations

- **Rate limits.** Quotes are cached for `QUOTE_CACHE_SECONDS` (default 60s). A built-in budget keeps you under 8 requests/minute.
  - When the budget is spent, cached prices are shown and labelled **Delayed**.
  - Market orders are blocked until a fresh price arrives; limit orders can still be placed.
  - A dashboard with many holdings plus watchlist symbols can exceed 8 symbols per minute. Some prices will then lag by a minute or two.
- **Search prices.** Search results only fetch prices for the top 3 matches in live mode, to save credits.
- **Simplified fills.** Orders fill at the last price, with no bid/ask spread, slippage, partial fills, commissions or fees.
- **Limit-order timing.** While the app is running, limit orders are checked against the latest quote about once a minute. A brief spike between checks can be missed.
  - After the app has been asleep, orders are settled from 5-minute bars (1-hour bars for gaps over 3 days, daily bars over 120 days). Very long absences are therefore settled less precisely.
  - Each catch-up uses 1 API credit per symbol with an open order.
- **After hours.** When the market is closed, market orders still fill immediately at the last price, and the preview warns about this. A real broker would queue them.
- **Not supported:** whole shares only (no fractional shares), US-dollar securities only, and no short selling, options, dividends or splits.
- **Today's change** is measured from the previous close. For shares bought or sold today, it uses your actual fill prices.
- **Single user.** One account, protected by one password when deployed.
- **Unverified endpoints.** The Twelve Data endpoints are tested against recorded response shapes, not against the live API.

## Project layout

```
server.py            WSGI app (JSON API + static files); local server with background limit-order worker
paper/config.py      Environment / .env configuration
paper/db.py          SQLite (local file) or Postgres (DATABASE_URL) storage
paper/engine.py      Order validation, fills, reservations, catch-up settling, P/L accounting
paper/auth.py        Password sign-in and signed session cookies
paper/market.py      Twelve Data adapter, rate limiter, caching facade
paper/demo.py        Deterministic simulated market for demo mode
static/              index.html, styles.css, app.js (no build step, no CDN)
tests/               unittest suites (engine, catch-up, auth, demo market, Twelve Data adapter)
vercel.json          Vercel function config (entrypoint: `app` in server.py)
requirements.txt     psycopg, only needed with DATABASE_URL (Postgres)
Dockerfile, fly.toml Container and Fly.io deployment (scale to zero + volume)
```

### API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/status` | Data mode, provider, rate-limit usage |
| GET | `/api/account` | Balances, holdings with P/L, open orders, recent trades |
| POST | `/api/account/reset` | `{starting_balance, confirm: true}` |
| GET | `/api/search?q=` | Symbol search (US listings) |
| GET | `/api/quotes?symbols=A,B` | Batch quotes |
| GET | `/api/stocks/{symbol}` | Quote, position, open orders, trades |
| GET | `/api/stocks/{symbol}/history?range=1D` | Chart data |
| GET/POST/DELETE | `/api/watchlist[/{symbol}]` | Watchlist |
| POST | `/api/orders/preview` | Dry-run an order |
| POST | `/api/orders` | Place an order (`expected_price` guards against price moves) |
| POST | `/api/orders/{id}/cancel` | Cancel an open order |
| GET | `/api/orders`, `/api/transactions` | History |

### Safety

- Write requests must send JSON, and cross-origin writes are rejected.
- A strict Content-Security-Policy is set, and all output is escaped.
- The server binds to localhost by default.
- All money is stored in integer cents, and every fill runs in a single SQLite transaction.
