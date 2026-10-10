# AI Coding Guide: NIFTY Options Engine

This document is a handoff guide for a coding assistant or developer who is
new to this repository. It describes the current design, data flow, runtime
configuration, safety boundaries, deployment model, and known operational
issues. Treat the code and `.env.example` as the source of truth when details
change.

## Project summary

This is a single-user Flask/Socket.IO dashboard and Python engine for analyzing
and buying intraday index options through FYERS API v3. Supported underlyings
are NIFTY 50, BANKNIFTY, FINNIFTY, and MIDCPNIFTY on NSE, plus SENSEX on BSE.
It reads option-chain and
market data, computes technical/probability signals, optionally asks one AI
provider for a sentiment opinion, then either reports a recommendation or
places/monitors orders depending on the selected action and `dry_run` setting.

The application is not a strategy simulator or a guaranteed-profit system.
Live order placement is real broker activity. Keep changes to decision logic,
position management, and order handling especially reviewable.

## Repository map

| File | Responsibility |
|---|---|
| `app.py` | Flask routes, login/session/CSRF, Socket.IO, settings UI/API, process state, chart APIs, journal sync, background engine threads. |
| `engine.py` | Analysis pipeline, market-data orchestration, final decisions, Execute/Analyze/manual run flows, open-position monitoring, exits. |
| `fyers_client.py` | FYERS v3 authentication, symbols/instrument master, quotes/history/WebSocket, order/position APIs and broker journal APIs. It adapts FYERS to an older Kite-like method interface, so `kite_client` variable names in `engine.py` are historical naming. |
| `app_config.py` | Loads `.env`, `settings.json`, and environment overrides; keeps credentials environment-only. |
| `option_signal.py` | Pure signal math: PCR/OI walls, VWAP, ORB, gaps, expected move, probability combination, confidence. |
| `decision.py` | Pure final decision rules: CALL, PUT, BOTH, or NO_TRADE. |
| `ai_sentiment.py` | Shared prompt, provider adapters (Gemini, Claude, OpenAI), response parsing, safe neutral fallback. |
| `trading_journal.py` | SQLite storage for app recommendations/outcomes, FYERS account records and sync metadata. |
| `deploy_oracle.ps1` | Windows PowerShell uploader for selected app files and an Oracle VM restart. |
| `test_fyers_sensex.py` | Mocked regression coverage for SENSEX/BSE symbols, contract metadata and market status. |
| `templates/` | Login, dashboard, FYERS callback pages. |
| `static/dashboard.js` | Browser settings, dashboard polling, chart, events/log display and controls. |
| `diagnose_fyers_oi.py` | FYERS option-chain/WebSocket/quote OI diagnostic using the cached token. |
| `test_gemini_api.py` | Gemini key/model and generation diagnostic; can separately exercise Google Search grounding. |
| `test_option_signal.py`, `test_decision.py` | Existing pure-logic tests. |
| `.env.example` | Names and safe defaults for environment variables. Never put real keys here. |
| `settings.example.json` | Safe non-secret strategy defaults. |

`README.md` has the user-facing quick start and deployment instructions.

## Main application flow

1. `app.py` serves the password-protected dashboard. Login credentials come
   from `APP_USERNAME` and `APP_PASSWORD`. State-changing requests require the
   session CSRF token. The dashboard login is separate from FYERS OAuth.
2. The dashboard's **Login** action builds a FYERS OAuth URL. FYERS redirects
   to `/auth/fyers/callback`; `fyers_client.py` exchanges the auth code and
   stores a same-day access token in `token.json` under `DATA_DIR`.
3. **Analyze** calls `engine.analyze_only()`. It gathers current market data
   and reports a recommendation but does not submit an order, even when
   `dry_run` is false.
4. **Execute** calls `engine.run()`. It performs analysis and may place an
   order if the decision and configured mode allow it, then monitors the
   position. `dry_run: true` is the default and logs simulated orders only.
5. **Manual Trade** calls `engine.manual_run()` with the chosen side. It uses
   real order and position handling; `dry_run` still controls whether orders
   are submitted to FYERS.
6. **Stop** signals the running engine and follows its square-off/stop logic.
   Do not change this path without tracing active-position behavior first.

The server keeps process-local FYERS login and engine state. Run one app
worker/process per account instance. Multiple Gunicorn workers can have
different login state, engine state, and Socket.IO clients.

## Analysis and strategy data flow

`engine.py` builds an option universe for the selected `settings["index"]`,
finds the nearest expiry, computes ATM and tracks ATM +/- 5 strikes. The
engine subscribes to the selected index, tracked options and a nearby futures
contract (used as the VWAP proxy). It then computes, in broad terms:

- Spot/index quote and option premiums.
- Option-chain OI/PCR, OI walls and OI-change ratios versus the previous
  session where history is available.
- Futures VWAP bias, current-day ORB, opening gap and CPR context.
- VIX-implied and recent historical expected-move estimates.
- Probability/confidence values and premium-based minimum profitable moves.
- One AI sentiment result, then a combined probability and final decision.

`option_signal.py` owns the calculations. `decision.py` applies the final
truth table. AI sentiment is a bounded probability nudge and conflict input;
it must not choose the contract or lot size. The engine's risk/entry/exit
logic remains authoritative. Do not move trading decisions into an AI prompt.

### OI behavior and known FYERS data shape

FYERS WebSocket ticks may omit OI for many option contracts. `engine.py`
logs how many are missing and makes one REST `quote()` request for those
symbols. In `FyersClient.get_quote()`, FYERS quote OI is preferred; if that is
zero/missing, the method can use the OI already present in the option-chain
snapshot. The engine logs a recovery summary with source counts and unresolved
symbols. A message such as `recovered 22/22; FYERS quote OI=0, option-chain
snapshot OI=22` means fallback succeeded, but values came from the option
chain rather than the quote response.

If an OI value remains missing, inspect how the caller handles zero/missing
values before changing PCR math. Do not silently claim REST quote OI was
available when it was actually sourced from the chain snapshot.

### Timezones and charts

Market session calculations use India Standard Time (IST, UTC+05:30). ORB
must compare today's candles with the 09:15 IST session open and configured
opening-range duration. FYERS history candles use Unix epoch timestamps.
`/api/spot_chart` converts epoch seconds (and tolerates milliseconds) and
returns epoch seconds for the browser chart. Avoid assuming FYERS timestamps
are Python `datetime` objects.

## AI providers

`ai_sentiment.py` normalizes each provider's response to:

```json
{"sentiment":"POSITIVE|NEGATIVE|NEUTRAL","reason":"one-line reason"}
```

The instrument name comes from the selected index and is shared by the live
AI request and `/api/prompt_preview`. Supported labels are NIFTY 50, NIFTY
BANK (BANKNIFTY), NIFTY Financial Services (FINNIFTY), NIFTY Midcap Select
(MIDCPNIFTY), and SENSEX.

| `AI_PROVIDER` | Credential | Model setting | Transport |
|---|---|---|---|
| `gemini` | `GEMINI_API_KEY` + `TAVILY_API_KEY` | `GEMINI_MODEL` | One basic Tavily news search restricted to the previous day supplies compact web results; its query date and Gemini prompt date use Asia/Kolkata. Gemini `generateContent` interprets them; Google Search grounding is disabled. |
| `claude` | `ANTHROPIC_API_KEY` | `CLAUDE_MODEL` | Anthropic Messages API with web search. |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` | OpenAI Responses API with `web_search` and strict structured JSON output. |

OpenAI here means the OpenAI API, not signing into the ChatGPT website. An
OpenAI API key is needed and API usage is billed through the OpenAI API
account. Requests use the existing `requests` dependency; no OpenAI SDK is
required. The `.env.example` default model is `gpt-6-astra`, configurable via
`OPENAI_MODEL`.

If a provider fails, times out, returns invalid JSON, or is not configured,
the code returns the safe `NEUTRAL` result so the engine can finish analysis.
Logs must never include API keys. Gemini uses Tavily search results as
untrusted reference data; a Tavily failure does not prevent the Gemini call.
If Gemini fails, the safe `NEUTRAL` result is returned.

### Gemini API diagnostic

The current diagnostic script supports:

```powershell
python test_gemini_api.py
python test_gemini_api.py --list-models
python test_gemini_api.py --grounded --application-prompt
python test_gemini_api.py --model gemini-flash-lite-latest --grounded --application-prompt
```

The diagnostic script directly tests Gemini, independently of Tavily. The
`--grounded` option is only for diagnosing Google's own Search grounding; the
application no longer enables that tool. For the application pipeline, inspect
the `[ai_sentiment] Tavily` and `Gemini request HTTP` log entries.

## Configuration and persistence

`app_config.py` reads the project `.env` first, then overlays non-empty
environment variables on `settings.json`. Environment values take precedence.
AI credentials and FYERS secrets are environment-only and are removed from
settings responses/saved settings.

### Environment variables

| Variable(s) | Purpose |
|---|---|
| `APP_USERNAME`, `APP_PASSWORD` | Dashboard login. Password must be at least 16 characters. |
| `APP_SESSION_SECRET` | Flask session signing secret; use a random value of at least 32 characters. |
| `APP_ENV`, `COOKIE_SECURE` | Production/session-cookie behavior; use HTTPS and secure cookies in production. |
| `HOST`, `PORT` | Bind host and port. Default host is `0.0.0.0`; default port is `5050`. |
| `FYERS_CLIENT_ID`, `FYERS_SECRET_KEY`, `FYERS_REDIRECT_URI` | FYERS OAuth application credentials and exact callback URL. Register the callback URL in FYERS. |
| `AI_PROVIDER` | Optional fixed provider (`gemini`, `claude`, `openai`). If set, the UI provider dropdown is disabled. If absent, provider can be saved in `settings.json`. |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Provider-specific API keys. Configure the key matching the selected provider. |
| `TAVILY_API_KEY` | Tavily key used for Gemini's web search context; one Basic Search request per Gemini sentiment call, filtered with `topic=news` and `days=1`. |
| `GEMINI_MODEL`, `CLAUDE_MODEL`, `OPENAI_MODEL` | Optional model overrides. |
| `DATA_DIR` | Directory for settings, FYERS token cache and trading journal. Use persistent storage on cloud hosts. |
| `SETTINGS_PATH` | Optional override for strategy settings file. |
| `PROXY_USERNAME`, `PROXY_PASSWORD` | Optional proxy credentials. |

`.env.example` contains variable names, not working credentials. `.env`,
`settings.json`, `token.json`, logs, analysis snapshots and SQLite journal
files are ignored by Git. Never copy secrets or account data into a handoff
document, issue, or commit.

`settings.json` holds non-secret strategy preferences such as selected index,
lots, thresholds, dry-run mode, stops/targets, and model/provider preferences.
`token.json` is the daily FYERS access-token cache. `trading_journal.sqlite3`
is user data. Do not delete these files as a cleanup step unless the user
specifically asks to reset that data.

## Dashboard endpoints

The app requires a dashboard-authenticated session for application routes.
POST routes also require the CSRF header from the page.

| Route | Purpose |
|---|---|
| `/` and `/login` | Dashboard and app login. |
| `/auth/fyers/callback` | FYERS OAuth callback. |
| `/api/settings` | Read/update editable strategy settings; credentials are redacted. |
| `/api/login`, `/api/login/manual` | Start/reuse FYERS login or submit callback token. |
| `/api/execute` | Start full analysis/execute/monitor flow. |
| `/api/analyze` | Read-only analysis; never places an order. |
| `/api/manual_trade` | Start a user-selected CALL/PUT/BOTH run. |
| `/api/stop` | Signal stop/square-off behavior. |
| `/api/status`, `/api/last_analysis` | Runtime state and last analysis. |
| `/api/spot_ltp`, `/api/spot_chart`, `/api/atm_preview` | Read-only market/dashboard data. |
| `/api/prompt_preview` | Preview the provider prompt for the selected instrument. |
| `/api/journal`, `/api/journal/sync` | Read/sync trading journal data. |

## Trading journal

`trading_journal.py` uses SQLite in `DATA_DIR`. It stores app recommendation
events and underlying outcomes, broker order/trade records, and raw broker
JSON. `app.py` starts a background sync loop after FYERS login, backfills
historical periods, then periodically syncs account activity. FYERS account-wide
order/trade feeds are the source for manual activity; the app's own recommendation
log is separate from account fills and P&L. FYERS charge-report syncing is not
implemented.

Keep sync idempotent and preserve raw broker payloads when changing field
normalization. FYERS response envelopes vary; inspect `fyers_client.py` and
`trading_journal.py` together before changing journal behavior.

## Run and deploy

### Local

Windows: `run.bat` or `python app.py`. Linux/macOS: `./run.sh` or
`python3 app.py`. The scripts install `requirements.txt`; the shell scripts
also stop an earlier local process before starting. The Flask-SocketIO server
defaults to `0.0.0.0:5050`; set `HOST=127.0.0.1` to restrict it locally.

### Render

Use a **Web Service**, not a Static Site. Leave Publish Directory empty.
Build command: `pip install -r requirements.txt`. Start command:

```sh
gunicorn -w 1 --threads 100 --bind 0.0.0.0:$PORT app:app
```

Configure environment variables in the Render dashboard, including secrets.
Use one worker because the engine/session state is in-process. Mount persistent
storage and set `DATA_DIR` if settings, token cache and journal must survive
restarts. Set `FYERS_REDIRECT_URI` to the public HTTPS callback and register
that exact value with FYERS.

### Oracle Cloud VM

Bind on `0.0.0.0` (default) and expose the app port through the VM/network
firewall only as intended. Prefer a TLS reverse proxy for public access. Keep
port `5000` closed for the hosted dashboard OAuth flow; the app callback is on
the dashboard port (`5050` by default). A small memory VM should run one app
process and one worker. Use a service manager (for restart-on-boot) rather
than leaving a terminal session as the only process supervisor.

For Windows deployments, `deploy_oracle.ps1` stages and copies only the app
modules, `requirements.txt`, `settings.example.json`, `run.sh`, `templates`,
and `static`, then restarts through `run.sh`. It defaults to the project's
Oracle address and key path; use `-SshKey`, `-Remote`, and `-RemoteDir` to
override them. It does not transfer `.env`, `settings.json`, `token.json`, logs,
SQLite databases, tests, or documents. The upload overlays those selected
paths without deleting unrelated remote files. It requires an existing
`$RemoteDir/.venv/bin/activate`; if missing, the remote script aborts before
replacing app files. It activates that venv before `run.sh`, so dependencies
are installed and the app starts inside it. The app log is timestamped under
the remote `logs/` directory.

The FYERS adapter resolves SENSEX through `BSE:SENSEX-INDEX` and the BSE F&O
symbol master. It keeps FYERS's `BSE` API exchange while translating to the
legacy `BFO` key used by internal option/future interfaces. The live-market
guard checks NSE F&O as 10/11 and BSE F&O as 12/12. This adds SENSEX, not
SENSEX50. India VIX remains the shared volatility input across selections;
for SENSEX it is a proxy, not a SENSEX-specific volatility index.

### Restarting

Python source edits do not hot-reload. Fully restart `app.py`/Gunicorn after
changing `.py` files or environment variables. The dashboard has a restart
notice based on source mtimes. A browser refresh does not reload Python code.

## Diagnostics and troubleshooting

- **FYERS token/auth:** Check `FYERS_CLIENT_ID`, secret, exact redirect URL,
  server time/date and whether today's cached token belongs to the same app.
  `token.json` is sensitive; never paste or commit it.
- **Option contracts missing:** Run `diagnose_fyers_oi.py` with the existing
  token. It reports option-chain, WebSocket and quote OI details; raw tokens
  must not be copied into public logs.
- **Tavily search failure:** Check that `TAVILY_API_KEY` is configured and read
  the `[ai_sentiment] Tavily` status/error log. Gemini still runs without live
  search context if Tavily is unavailable.
- **AI safe NEUTRAL:** Read `[ai_sentiment]` logs for provider/model, Tavily
  result count, fallback status, response or error. A safe default means no valid
  AI response; it is not a directional AI signal.
- **Chart 500:** Check server traceback. FYERS history dates are Unix epochs;
  avoid `.timestamp()` on an integer.
- **Old source still running:** Restart the Python process; do not infer a
  deployment update from a browser refresh alone.
- **Journal missing older account events:** Check whether the journal sync
  loop ran while logged in, persistent `DATA_DIR` exists, and FYERS history
  endpoints return records.

HTTP 503 means the upstream model/API service may be temporarily unavailable;
the app currently falls back to safe neutral rather than escalating to a
different provider/model automatically. HTTP 429 must be interpreted using
the actual API error body: it can represent different rate/quota conditions.

## Change workflow for coding assistants

1. Read this guide and `README.md`, then inspect `git status --short` before
   editing. The working tree may contain uncommitted user work. Preserve it;
   do not reset, clean, or overwrite unrelated files.
2. Inspect the concrete call path before changing a behavior: UI/API in
   `app.py` and `static/`, orchestration in `engine.py`, broker adaptation in
   `fyers_client.py`, math in `option_signal.py` / `decision.py`.
3. Keep secrets in environment variables. Never print keys, tokens, passwords,
   or the contents of `.env`/`token.json`.
4. Keep read-only analysis separated from order placement. Trace `dry_run`,
   `analyze_only`, `manual_run`, stops, and existing-position recovery before
   modifying trading paths.
5. When adding an AI provider, implement its environment-only key, model
   setting, UI choice/status, prompt index, response normalization, safe
   failure behavior and diagnostics together. Do not let it choose quantity or
   instrument.
6. Network/API diagnostics can use quota or incur charges. Use explicit
   one-shot commands and record the actual HTTP status and sanitized API error
   details.
7. After edits, summarize changed files and what was or was not verified.
   Do not claim a live FYERS/Gemini/Claude/OpenAI test unless a live call was
   actually made.
