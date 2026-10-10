"""
Phase 2: Flask + Flask-SocketIO web dashboard on top of the Phase 1 engine.

This file does NOT re-implement any trading/analysis logic - it only:
  - serves the single-page dashboard (templates/index.html)
  - reads/writes settings.json (the same file engine.py reads)
  - runs engine.run() in a background thread on "Execute"
  - relays engine.py's on_event/on_tick callbacks to the browser over
    WebSocket (Flask-SocketIO), and relays "Stop" clicks to the same
    stop_event that engine.py's monitoring loop already checks

Run with `python app.py` (or run.bat / run.sh). By default the dashboard
binds to 0.0.0.0:5050 for access through the machine's network interfaces.
Set HOST=127.0.0.1 to restrict it to local access.
"""

import datetime
import hmac
import json
import math
import os
import secrets
import threading
import time
import uuid
import webbrowser

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO

from app_config import DATA_DIR, SETTINGS_PATH, env_setting_is_set, load_settings, save_settings
import ai_sentiment
import engine
import option_signal
import trading_journal
from fyers_client import INDEX_SYMBOLS, FyersClient, extract_request_token, load_cached_access_token

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_ANALYSIS_PATH = os.path.join(BASE_DIR, "last_analysis_state.json")
PORT = int(os.environ.get("PORT", "5050"))
SUPPORTED_INDEXES = frozenset(INDEX_SYMBOLS)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")

# Editing a .py file never hot-reloads a running Python process - only a
# full restart of `python app.py` picks up new code (unlike templates/
# static files, which Flask already re-reads fresh on every request). This
# has caused real, hard-to-diagnose confusion more than once: the browser
# shows the latest UI (fetched fresh) while the backend keeps running old
# logic. So: remember when THIS process started, and compare it against the
# .py files' own last-modified time on every page load / status check - if
# any of them changed after this process started, we know for certain a
# restart is needed and can say so plainly instead of guessing.
SERVER_STARTED_AT = datetime.datetime.now()
_WATCHED_PY_FILES = ["app.py", "app_config.py", "engine.py", "fyers_client.py", "trading_journal.py", "option_signal.py", "ai_sentiment.py", "decision.py"]


def _code_last_modified():
    mtimes = []
    for fname in _WATCHED_PY_FILES:
        path = os.path.join(BASE_DIR, fname)
        if os.path.exists(path):
            mtimes.append(datetime.datetime.fromtimestamp(os.path.getmtime(path)))
    return max(mtimes) if mtimes else None


def restart_status():
    code_modified = _code_last_modified()
    needed = code_modified is not None and code_modified > SERVER_STARTED_AT
    return {
        "restart_needed": needed,
        "server_started_at": SERVER_STARTED_AT.strftime("%H:%M:%S"),
        "code_last_modified": code_modified.strftime("%H:%M:%S") if code_modified else None,
    }

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SESSION_SECRET") or os.urandom(32).hex()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "COOKIE_SECURE", "true" if os.environ.get("APP_ENV", "").lower() == "production" else "false"
).lower() in {"1", "true", "yes"}
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=8)
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(app, async_mode="threading")

LOGIN_FAILURES = {}
LOGIN_FAILURES_LOCK = threading.Lock()
OAUTH_PENDING = {}
OAUTH_PENDING_LOCK = threading.Lock()
LOGIN_MAX_FAILURES = 8
LOGIN_WINDOW_SECONDS = 15 * 60


def _login_credentials():
    return os.environ.get("APP_USERNAME", ""), os.environ.get("APP_PASSWORD", "")


def _login_environment_ready():
    username, password = _login_credentials()
    session_secret = os.environ.get("APP_SESSION_SECRET", "")
    return bool(
        username and username != "change-this-username"
        and len(password) >= 16 and not password.startswith("replace-with-")
        and len(session_secret) >= 32 and not session_secret.startswith("replace-with-")
    )


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def require_dashboard_login():
    if request.endpoint in {"login_page", "login_submit", "static"}:
        return None
    if session.get("authenticated"):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            sent = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not sent or not expected or not hmac.compare_digest(sent, expected):
                return jsonify({"ok": False, "error": "invalid or missing CSRF token"}), 400
        return None
    if request.path.startswith("/api/") or request.path.startswith("/socket.io"):
        return jsonify({"ok": False, "error": "login required"}), 401
    return redirect(url_for("login_page", next=request.path))


@socketio.on("connect")
def require_socket_login(auth=None):
    if not session.get("authenticated"):
        return False


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    ready = _login_environment_ready()
    return render_template("login.html", error=None, csrf_token=_csrf_token(), login_ready=ready)


@app.route("/auth/login", methods=["POST"])
def login_submit():
    csrf = request.form.get("csrf_token", "")
    if not csrf or not hmac.compare_digest(csrf, session.get("csrf_token", "")):
        return render_template("login.html", error="Login form expired. Please try again.",
                               csrf_token=_csrf_token(), login_ready=True), 400

    username, password = _login_credentials()
    if not _login_environment_ready():
        return render_template("login.html", error="Set APP_USERNAME, an APP_PASSWORD of at least 16 characters, and an APP_SESSION_SECRET of at least 32 characters, then restart the app.",
                               csrf_token=_csrf_token(), login_ready=False), 503

    now = time.time()
    remote = request.remote_addr or "unknown"
    with LOGIN_FAILURES_LOCK:
        failures, started = LOGIN_FAILURES.get(remote, (0, now))
        if now - started >= LOGIN_WINDOW_SECONDS:
            failures, started = 0, now
        if failures >= LOGIN_MAX_FAILURES:
            return render_template("login.html", error="Too many failed attempts. Wait 15 minutes and try again.",
                                   csrf_token=_csrf_token(), login_ready=True), 429

    submitted_username = request.form.get("username", "")
    submitted_password = request.form.get("password", "")
    valid_user = hmac.compare_digest(submitted_username.encode(), username.encode())
    valid_password = hmac.compare_digest(submitted_password.encode(), password.encode())
    if not (valid_user and valid_password):
        with LOGIN_FAILURES_LOCK:
            LOGIN_FAILURES[remote] = (failures + 1, started)
        return render_template("login.html", error="Incorrect username or password.",
                               csrf_token=_csrf_token(), login_ready=True), 401

    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.pop(remote, None)
    session.clear()
    session["authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    next_path = request.form.get("next", "/")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    return redirect(next_path)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))

STATE_LOCK = threading.Lock()
STATE = {
    "kite_client": None,
    "logged_in": False,
    "running": False,
    "stop_event": None,
    # Last "universe_ready"/"decision" engine_event seen, so a page refresh
    # (including while a position is still open and the engine mid-run) can
    # redisplay the same analysis instead of resetting to blank - these are
    # one-time events with nothing that re-sends them, unlike pnl_update
    # which repeats every monitoring tick and is why P&L/positions alone
    # used to survive a refresh. Also mirrored to last_analysis_state.json
    # (see _save_last_analysis_to_disk) so a full process restart doesn't
    # lose it either - seeded below from that file, not just None. See
    # /api/last_analysis and dashboard.js's loadLastAnalysis().
    "last_universe_event": None,
    "last_decision_event": None,
}


def _load_last_analysis_from_disk():
    """
    STATE["last_universe_event"]/["last_decision_event"] alone only survive
    a browser refresh - they live in THIS process's memory, so a full
    restart (or crash) wipes them back to None even though the Signal
    Breakdown/Probability-of-Move numbers they represent are still exactly
    what led to whatever position is (or was) open. Mirrored to
    last_analysis_state.json on every update (see _make_event_relays) and
    reloaded here at startup, so a restart no longer loses that context -
    only a genuinely fresh analysis (a new Execute/Analyze run) replaces it.
    """
    try:
        with open(LAST_ANALYSIS_PATH, "r") as f:
            data = json.load(f)
        return data.get("universe"), data.get("decision")
    except (OSError, json.JSONDecodeError):
        return None, None


def _save_last_analysis_to_disk():
    try:
        with open(LAST_ANALYSIS_PATH, "w") as f:
            json.dump({"universe": STATE["last_universe_event"], "decision": STATE["last_decision_event"]}, f)
    except OSError as exc:  # noqa: BLE001 - best-effort persistence, must never crash the event relay
        print(f"[app] could not persist last-analysis snapshot to disk: {exc!r}")


STATE["last_universe_event"], STATE["last_decision_event"] = _load_last_analysis_from_disk()


def settings_configured(settings):
    """Whether the minimum required keys are filled in to allow Login/Execute."""
    if not settings.get("fyers_client_id") or not settings.get("fyers_secret_key"):
        return False
    provider = settings.get("ai_provider", "gemini")
    if provider not in {"gemini", "claude", "openai"}:
        return False
    if provider == "gemini" and not settings.get("gemini_api_key"):
        return False
    if provider == "claude" and not settings.get("anthropic_api_key"):
        return False
    if provider == "openai" and not settings.get("openai_api_key"):
        return False
    return True


def _make_log():
    def log(message):
        print(message)
        socketio.emit("log", {"message": message})
    return log


JOURNAL_SYNC_LOCK = threading.Lock()
JOURNAL_SYNC_THREAD = None
JOURNAL_SYNC_STOP = threading.Event()


def _update_recommendation_outcomes(client, now_ist):
    """Persist real spot observations and explicit labels for prior recommendations."""
    pending = trading_journal.pending_recommendations(limit=1000)
    grouped = {}
    option_candle_cache = {}
    for row in pending:
        try:
            context = json.loads(row.get("universe_json") or "{}")
            symbol = context.get("spot_symbol")
            if symbol:
                grouped.setdefault((symbol, row.get("trading_date")), []).append((row, context))
        except (TypeError, ValueError):
            continue

    for (symbol, day), rows in grouped.items():
        try:
            trade_date = datetime.date.fromisoformat(day)
            market_open = datetime.datetime.combine(trade_date, datetime.time(9, 15), tzinfo=trading_journal.IST)
            market_close = datetime.datetime.combine(trade_date, datetime.time(15, 30), tzinfo=trading_journal.IST)
            now_for_day = min(now_ist, market_close) if trade_date == now_ist.date() else market_close
            candles = client.get_intraday_history_for_symbol(symbol, market_open, now_for_day, "1minute")
        except Exception:
            # A transport/API failure is not evidence of zero movement or a missing market outcome.
            continue

        points = []
        for candle in candles:
            try:
                stamp = datetime.datetime.fromtimestamp(int(candle["date"]), datetime.timezone.utc).astimezone(
                    trading_journal.IST)
                points.append((stamp, float(candle["close"])))
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
        points.sort(key=lambda item: item[0])
        samples = [{"timestamp": stamp.isoformat(), "price": price} for stamp, price in points]

        for row, context in rows:
            try:
                created = datetime.datetime.fromisoformat(row["created_at"]).astimezone(trading_journal.IST)
                start_price = float(row.get("spot_price") or context.get("spot_price"))
                outcomes = json.loads(row.get("outcomes_json") or "{}")
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, ValueError):
                continue
            if start_price <= 0:
                continue
            direction = str(row.get("direction") or "").upper()
            signal_samples = [sample for sample in samples
                              if datetime.datetime.fromisoformat(sample["timestamp"]) >= created]
            targets = {"15m": created + datetime.timedelta(minutes=15),
                       "30m": created + datetime.timedelta(minutes=30),
                       "60m": created + datetime.timedelta(minutes=60),
                       "close": market_close}
            required = {"call": payload.get("required_move_call"),
                        "put": payload.get("required_move_put"),
                        "both": payload.get("required_move_both")}
            changed = {}
            for horizon, target in targets.items():
                existing = outcomes.get(horizon)
                if isinstance(existing, dict) and existing.get("status") in ("ready", "unavailable"):
                    continue
                if horizon == "close" and created >= market_close:
                    if now_ist >= market_close + datetime.timedelta(minutes=5):
                        changed[horizon] = {"status": "unavailable", "reason": "recommendation after market close"}
                    continue
                if target > market_close:
                    if now_ist >= market_close + datetime.timedelta(minutes=10):
                        changed[horizon] = {"status": "unavailable", "reason": "horizon extends past market close"}
                    continue
                if horizon == "close":
                    if now_ist < market_close + datetime.timedelta(minutes=5):
                        continue
                    closing_points = [sample for sample in signal_samples
                                      if datetime.datetime.fromisoformat(sample["timestamp"]) <= market_close]
                    if not closing_points:
                        if now_ist >= market_close + datetime.timedelta(minutes=10):
                            changed[horizon] = {"status": "unavailable", "reason": "no market-close spot candle"}
                        continue
                    outcome = trading_journal.build_horizon_outcome(
                        start_price, signal_samples, direction, closing_points[-1]["timestamp"], required_moves=required)
                    if outcome.get("status") == "ready":
                        outcome["target_at"] = market_close.isoformat()
                else:
                    if now_ist < target:
                        continue
                    outcome = trading_journal.build_horizon_outcome(
                        start_price, signal_samples, direction, target.isoformat(), required_moves=required)
                    if outcome.get("status") == "pending":
                        if now_ist > target + datetime.timedelta(minutes=10):
                            outcome = {"status": "unavailable", "reason": "no spot candle at or after target"}
                        else:
                            continue
                if direction == "BOTH" and outcome.get("status") == "ready":
                    call_symbol = payload.get("atm_call_symbol") or context.get("atm_call_symbol")
                    put_symbol = payload.get("atm_put_symbol") or context.get("atm_put_symbol")
                    entries = {"call": payload.get("ce_premium"), "put": payload.get("pe_premium")}
                    exits = {}
                    option_history_available = True
                    for leg, option_symbol in (("call", call_symbol), ("put", put_symbol)):
                        if not option_symbol or entries[leg] is None:
                            option_history_available = False
                            break
                        cache_key = (day, option_symbol)
                        if cache_key not in option_candle_cache:
                            try:
                                option_candles = client.get_intraday_history_for_symbol(
                                    option_symbol, market_open, now_for_day, "1minute")
                                option_candle_cache[cache_key] = [
                                    {"timestamp": datetime.datetime.fromtimestamp(
                                        int(candle["date"]), datetime.timezone.utc).astimezone(
                                            trading_journal.IST).isoformat(), "price": float(candle["close"])}
                                    for candle in option_candles
                                ]
                            except Exception:
                                option_candle_cache[cache_key] = None
                        option_samples = option_candle_cache[cache_key]
                        if option_samples is None:
                            option_history_available = False
                            break
                        matching = [sample for sample in option_samples
                                    if sample["timestamp"] >= outcome["observed_at"]]
                        if matching:
                            exits[leg] = matching[0]["price"]
                        else:
                            option_history_available = False
                            break
                    if option_history_available:
                        outcome = trading_journal.build_horizon_outcome(
                            start_price, signal_samples, direction, outcome["observed_at"],
                            required_moves=required, option_entry_premiums=entries,
                            option_exit_premiums=exits)
                    elif now_ist > target + datetime.timedelta(minutes=10):
                        outcome["both_leg_profitability"] = {
                            "status": "unavailable", "reason": "FYERS option-price history not available at horizon"}
                    else:
                        continue
                if outcome.get("status") == "ready":
                    outcome["opportunity_context"] = {
                        "decision": direction,
                        "label_method": ("directional spot movement; not realized trading P&L"
                                         if direction in ("CALL", "PUT") else
                                         "separate underlying-move premium-threshold proxy"),
                    }
                changed[horizon] = outcome
            if changed:
                trading_journal.update_recommendation_outcomes(row["id"], changed)

def _journal_sync_loop():
    last_outcome_sync = 0.0
    last_error_log = 0.0
    log = _make_log()
    while not JOURNAL_SYNC_STOP.is_set():
        with STATE_LOCK:
            client = STATE.get("kite_client") if STATE.get("logged_in") else None
        if client is not None:
            now_ist = datetime.datetime.now(trading_journal.IST)
            if time.monotonic() - last_outcome_sync >= 60:
                try:
                    _update_recommendation_outcomes(client, now_ist)
                except Exception as exc:
                    if time.monotonic() - last_error_log > 60:
                        log(f"[journal] recommendation outcome update failed: {exc!r}")
                        last_error_log = time.monotonic()
                last_outcome_sync = time.monotonic()
        JOURNAL_SYNC_STOP.wait(30)


def _ensure_journal_sync():
    global JOURNAL_SYNC_THREAD
    with JOURNAL_SYNC_LOCK:
        if JOURNAL_SYNC_THREAD is not None and JOURNAL_SYNC_THREAD.is_alive():
            return
        JOURNAL_SYNC_STOP.clear()
        JOURNAL_SYNC_THREAD = threading.Thread(target=_journal_sync_loop, name="recommendation-outcome-sync", daemon=True)
        JOURNAL_SYNC_THREAD.start()


def _silent_log(message):
    pass


# Cache for the ATM CALL/PUT preview (see /api/atm_preview below): the full
# NFO instrument dump (build_option_universe) is expensive and only needs
# refetching when the underlying/expiry changes or the spot has drifted far
# enough that the ATM strike falls outside the previously fetched +/-5
# window - every other poll only needs two lightweight quote() calls.
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_UNIVERSE = {"data": None}


def _get_atm_preview(kite_client, settings):
    """
    Read-only, no-order-placement preview of exactly what a manual Buy CALL/
    PUT/BOTH click would enter right now: same ATM strike selection
    (engine.build_option_universe / option_signal.get_atm_strike) as
    enter_positions() itself uses, with live LTPs pulled straight from FYERS.
    """
    index = settings["index"]
    spot_symbol = engine._resolve_spot_tradingsymbol(index)
    spot_quote_key = INDEX_SYMBOLS.get(index, f"NSE:{spot_symbol}")
    spot_price = kite_client.get_quote([spot_quote_key])[spot_quote_key]["last_price"]

    with _PREVIEW_LOCK:
        universe = _PREVIEW_UNIVERSE["data"]
        needs_rebuild = (
            universe is None
            or universe["spot_symbol"] != spot_symbol
            or option_signal.get_atm_strike(spot_price, universe["strike_interval"]) not in universe["instruments_by_strike"]
        )
        if needs_rebuild:
            universe = engine.build_option_universe(kite_client, index, _silent_log)
            _PREVIEW_UNIVERSE["data"] = universe

    atm_strike = option_signal.get_atm_strike(spot_price, universe["strike_interval"])
    ce_instr = universe["instruments_by_strike"][atm_strike]["CE"]
    pe_instr = universe["instruments_by_strike"][atm_strike]["PE"]

    ce_key = f"{ce_instr.get('legacy_exchange', 'NFO')}:{ce_instr['tradingsymbol']}"
    pe_key = f"{pe_instr.get('legacy_exchange', 'NFO')}:{pe_instr['tradingsymbol']}"
    opt_quotes = kite_client.get_quote([ce_key, pe_key])
    ce_ltp = opt_quotes[ce_key]["last_price"]
    pe_ltp = opt_quotes[pe_key]["last_price"]
    lot_size = ce_instr["lot_size"]
    lots = settings.get("lots", 1)

    return {
        "spot_ltp": spot_price,
        "atm_strike": atm_strike,
        "expiry": str(universe["nearest_expiry"]),
        "lot_size": lot_size,
        "lots": lots,
        "call": {"tradingsymbol": ce_instr["tradingsymbol"], "ltp": ce_ltp,
                 "total_cost": round(ce_ltp * lot_size * lots, 2)},
        "put": {"tradingsymbol": pe_instr["tradingsymbol"], "ltp": pe_ltp,
                "total_cost": round(pe_ltp * lot_size * lots, 2)},
        "both_combined_ltp": round(ce_ltp + pe_ltp, 2),
        "both_total_cost": round((ce_ltp + pe_ltp) * lot_size * lots, 2),
    }


# Separate, lightweight cache for just the spot instrument token (used by the
# always-on chart/LTP feed below) - get_index_instrument() fetches Kite's
# full NSE instrument dump, so this is cached independently of the (option-
# chain) ATM preview cache above, shared across /api/spot_chart and
# /api/spot_ltp so idle polling doesn't re-fetch that dump every few seconds.
_SPOT_LOCK = threading.Lock()
_SPOT_INSTRUMENT = {"symbol": None, "token": None}
MARKET_OPEN_TIME = datetime.time(9, 15)


def _get_spot_instrument(kite_client, settings):
    index = settings["index"]
    spot_tradingsymbol = engine._resolve_spot_tradingsymbol(index)
    spot_symbol = INDEX_SYMBOLS.get(index, f"NSE:{spot_tradingsymbol}")
    with _SPOT_LOCK:
        if _SPOT_INSTRUMENT["symbol"] != spot_symbol:
            instrument = kite_client.get_index_instrument(spot_tradingsymbol, exchange="NSE")
            spot_symbol = instrument.get("symbol", spot_symbol)
            _SPOT_INSTRUMENT["symbol"] = spot_symbol
            _SPOT_INSTRUMENT["token"] = instrument["instrument_token"]
    return _SPOT_INSTRUMENT["symbol"], _SPOT_INSTRUMENT["token"]


# --------------------------------------------------------------- page

@app.route("/")
def index():
    settings = load_settings()
    ui_settings = dict(settings)
    if ui_settings.get("index") not in SUPPORTED_INDEXES:
        ui_settings["index"] = "NIFTY"
    for key in ("fyers_client_id", "fyers_secret_key", "gemini_api_key", "anthropic_api_key", "openai_api_key", "tavily_api_key"):
        ui_settings.pop(key, None)
    ui_settings["proxy"] = dict(settings.get("proxy", {}))
    ui_settings["proxy"].pop("user", None)
    ui_settings["proxy"].pop("pass", None)
    return render_template("index.html", settings=ui_settings, configured=settings_configured(settings),
                           restart=restart_status(), csrf_token=session.get("csrf_token", ""),
                           env_provider=env_setting_is_set("ai_provider"),
                           credential_status={
                               "fyers": env_setting_is_set("fyers_client_id") and env_setting_is_set("fyers_secret_key"),
                               "gemini": env_setting_is_set("gemini_api_key"),
                               "anthropic": env_setting_is_set("anthropic_api_key"),
                               "openai": env_setting_is_set("openai_api_key"),
                               "tavily": env_setting_is_set("tavily_api_key"),
                           })


# --------------------------------------------------------------- settings

@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = load_settings()
    # Never send credentials back to the dashboard, even if an old local
    # settings.json still contains them.
    for key in ("fyers_client_id", "fyers_secret_key", "gemini_api_key", "anthropic_api_key", "openai_api_key", "tavily_api_key"):
        settings.pop(key, None)
    if isinstance(settings.get("proxy"), dict):
        settings["proxy"] = {k: v for k, v in settings["proxy"].items() if k not in {"user", "pass"}}
    return jsonify({"settings": settings, "configured": settings_configured(settings)})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    incoming = request.get_json(force=True, silent=True) or {}
    settings = load_settings()

    # Only the fields the Settings section actually exposes are writable
    # here. Safety-critical/advanced fields (dry_run, force_exit_time,
    # pcr_threshold, range_lookback_days, historical_range_lookback_days,
    # gap_threshold_factor, orb_minutes, atm_oi_change_threshold) are
    # deliberately NOT editable from this endpoint - dry_run especially is
    # left as a manual settings.json edit on purpose, so nobody can flip
    # live trading on with one misplaced click (see README).
    if "index" in incoming:
        requested_index = str(incoming["index"]).strip().upper()
        if requested_index not in SUPPORTED_INDEXES:
            return jsonify({"ok": False, "error": "Choose a supported index: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, or SENSEX."}), 400
        with STATE_LOCK:
            if STATE["running"] and requested_index != settings.get("index"):
                return jsonify({"ok": False, "error": "Stop the engine before changing the options instrument."}), 400
        settings["index"] = requested_index

    if "ai_provider" in incoming:
        requested_provider = str(incoming["ai_provider"]).strip().lower()
        if requested_provider not in {"gemini", "claude", "openai"}:
            return jsonify({"ok": False, "error": "Choose Gemini, Claude, or OpenAI (ChatGPT)."}), 400
        settings["ai_provider"] = requested_provider

    str_fields = ["gemini_model", "claude_model", "openai_model", "time_exit",
                  "custom_ai_note"]
    for field in str_fields:
        if field in incoming:
            settings[field] = str(incoming[field])

    if "lots" in incoming:
        try:
            settings["lots"] = max(1, int(incoming["lots"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "lots must be a whole number"}), 400

    for bool_field in ["sl_enabled", "target_enabled", "profit_lock_enabled", "time_exit_enabled"]:
        if bool_field in incoming:
            settings[bool_field] = bool(incoming[bool_field])

    for num_field in ["max_loss", "target_profit"]:
        if num_field in incoming:
            try:
                settings[num_field] = float(incoming[num_field])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{num_field} must be a number"}), 400

    for num_field in ["profit_lock_activation", "profit_lock_amount"]:
        if num_field in incoming:
            try:
                number = float(incoming[num_field])
                if not math.isfinite(number) or number < 0:
                    return jsonify({"ok": False, "error": f"{num_field} must be a non-negative number"}), 400
                settings[num_field] = number
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{num_field} must be a number"}), 400

    if settings.get("profit_lock_enabled"):
        activation = float(settings.get("profit_lock_activation", 2000))
        floor = float(settings.get("profit_lock_amount", 1000))
        if (not math.isfinite(activation) or not math.isfinite(floor)
                or activation < 0 or floor < 0 or activation <= floor):
            return jsonify({"ok": False, "error": "Profit lock requires non-negative amounts and activation profit greater than the locked profit floor."}), 400

    if "profit_margin_factor" in incoming:
        try:
            settings["profit_margin_factor"] = max(1.0, float(incoming["profit_margin_factor"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "profit_margin_factor must be a number"}), 400

    for frac_field in ["momentum_min_fraction", "momentum_max_fraction"]:
        if frac_field in incoming:
            try:
                settings[frac_field] = max(0.0, min(1.0, float(incoming[frac_field])))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{frac_field} must be a number"}), 400

    if "oi_change_clamp" in incoming:
        try:
            settings["oi_change_clamp"] = max(0.0, min(1.0, float(incoming["oi_change_clamp"])))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "oi_change_clamp must be a number"}), 400

    for vc_field in ["volatility_confidence_ratio_scale", "volatility_confidence_orb_bonus",
                     "volatility_confidence_momentum_scale", "volatility_confidence_oi_buildup_scale",
                     "volatility_confidence_cpr_narrow_scale"]:
        if vc_field in incoming:
            try:
                settings[vc_field] = max(0.0, float(incoming[vc_field]))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{vc_field} must be a number"}), 400

    if isinstance(incoming.get("proxy"), dict):
        settings.setdefault("proxy", {})
        for k in ["enabled", "host", "port", "user", "pass"]:
            if k in incoming["proxy"]:
                settings["proxy"][k] = incoming["proxy"][k]

    save_settings(settings)
    return jsonify({"ok": True, "configured": settings_configured(settings)})


@app.route("/api/mode", methods=["POST"])
def set_mode():
    """
    Dedicated, standalone switch for dry_run/live - deliberately kept OUT of
    the generic /api/settings save (see the comment there) so flipping live
    trading on/off is always its own explicit action, never bundled with an
    unrelated settings change. Refuses while an Execute run is in progress,
    so a run never has its safety mode changed out from under it mid-flight.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "cannot change mode while the engine is running - Stop first"}), 400

    incoming = request.get_json(force=True, silent=True) or {}
    if "live" not in incoming:
        return jsonify({"ok": False, "error": "missing 'live' field"}), 400

    settings = load_settings()
    settings["dry_run"] = not bool(incoming["live"])
    save_settings(settings)
    log = _make_log()
    log(f"[app] mode switched to {'LIVE - real orders will be placed' if incoming['live'] else 'DRY RUN'}")
    return jsonify({"ok": True, "dry_run": settings["dry_run"]})


@app.route("/api/atm_preview", methods=["GET"])
def atm_preview():
    """
    Polled every few seconds by the dashboard while logged in AND the Manual
    Entry section is open, so the manual CALL/PUT/BOTH buttons show live ATM
    symbols + prices BEFORE any order is placed, instead of only finding out
    the entry price after a real order already went in. Purely read-only
    (quote() calls) - never touches order placement. (The LTP box and chart
    are driven separately by /api/spot_ltp/spot_chart below, so they stay
    live even while this section is collapsed.)
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "not logged in"}), 400
        kite_client = STATE["kite_client"]

    settings = load_settings()
    try:
        data = _get_atm_preview(kite_client, settings)
        return jsonify({"ok": True, **data})
    except Exception as exc:  # noqa: BLE001 - a preview hiccup must never break the dashboard
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/spot_chart", methods=["GET"])
def spot_chart():
    """
    Today's 5-minute candles for the underlying index, from market open
    (09:15 IST) to now - lets the dashboard's live chart show real history
    immediately after Login, instead of starting blank and only filling in
    from whatever moment Execute/a manual trade happens to be clicked.
    Entirely independent of Execute/manual trade - a pure market-data read,
    places no orders and touches no position state. Called once after Login
    (or on page load if already logged in) to backfill the chart; the
    still-forming current candle is then kept live via /api/spot_ltp polling
    (idle) or the real tick stream (once a run is active).
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "not logged in"}), 400
        kite_client = STATE["kite_client"]

    settings = load_settings()
    try:
        spot_symbol, spot_token = _get_spot_instrument(kite_client, settings)
        today = datetime.datetime.now(IST).date()
        market_open = datetime.datetime.combine(today, MARKET_OPEN_TIME, tzinfo=IST)
        now = datetime.datetime.now(IST)
        candles = []
        if now > market_open:
            raw = kite_client.get_intraday_history(spot_token, market_open, now, interval="5minute")
            for candle in raw:
                candle_date = candle["date"]
                if isinstance(candle_date, datetime.datetime):
                    if candle_date.tzinfo is None:
                        candle_date = candle_date.replace(tzinfo=IST)
                    candle_time = int(candle_date.timestamp())
                else:
                    # FYERS returns candle dates as Unix epoch integers;
                    # tolerate millisecond values from alternate wrappers.
                    candle_time = int(candle_date)
                    if abs(candle_time) > 10_000_000_000:
                        candle_time //= 1000
                candles.append({
                    "time": candle_time, "open": candle["open"], "high": candle["high"],
                    "low": candle["low"], "close": candle["close"],
                })
        return jsonify({"ok": True, "spot_symbol": spot_symbol, "spot_token": spot_token, "candles": candles})
    except Exception as exc:  # noqa: BLE001 - a chart-backfill hiccup must never break the dashboard
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/spot_ltp", methods=["GET"])
def spot_ltp():
    """
    Cheap, single-quote spot LTP - polled every few seconds while logged in
    and idle (no run active) so the LTP box and the chart's still-forming
    candle stay live regardless of whether the Manual Entry section is open
    or Execute has ever been clicked. Once a real run starts, the actual
    FYERS market-data socket "tick" events take over (far more real-time) and this polling
    pauses - see syncSpotFeed() in dashboard.js.
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "not logged in"}), 400
        kite_client = STATE["kite_client"]

    settings = load_settings()
    try:
        spot_symbol, _ = _get_spot_instrument(kite_client, settings)
        ltp = kite_client.get_quote([spot_symbol])[spot_symbol]["last_price"]
        return jsonify({"ok": True, "spot_symbol": spot_symbol, "ltp": ltp})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/prompt_preview", methods=["GET"])
def prompt_preview():
    """
    Read-only preview of the exact AI prompt (minus the market-context
    section, which only exists once numbers are computed mid-Execute) - so
    the previously-invisible prompt, plus whatever custom_ai_note is
    currently saved, is visible in the Advanced section before you ever
    click Execute.
    """
    settings = load_settings()
    custom_note = (settings.get("custom_ai_note") or "").strip() or None
    return jsonify({
        "prompt": ai_sentiment.build_prompt_preview(
            custom_note=custom_note, index=settings.get("index", "NIFTY")
        )
    })


# --------------------------------------------------------------- control

@app.route("/api/login", methods=["POST"])
def login():
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is running - stop it before logging in again"}), 400

    settings = load_settings()
    if not settings.get("fyers_client_id") or not settings.get("fyers_secret_key"):
        return jsonify({"ok": False, "error": "Set FYERS_CLIENT_ID and FYERS_SECRET_KEY in the server environment."}), 400
    if not env_setting_is_set("fyers_redirect_uri"):
        settings["fyers_redirect_uri"] = url_for("fyers_callback", _external=True)

    client = FyersClient(settings, log=_make_log())
    cached = load_cached_access_token(client.client_id)
    if cached:
        client.set_access_token(cached)
        with STATE_LOCK:
            STATE["kite_client"] = client
            STATE["logged_in"] = True
        _ensure_journal_sync()
        socketio.emit("login_status", {"status": "success", "source": "auto"})
        _resume_existing_position_if_any(_make_log())
        return jsonify({"ok": True, "cached": True})

    oauth_state = secrets.token_urlsafe(32)
    try:
        auth_url = client.generate_auth_url(oauth_state)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"could not create FYERS authorization URL: {exc}"}), 502

    with OAUTH_PENDING_LOCK:
        now = time.time()
        for old_state, pending in list(OAUTH_PENDING.items()):
            if pending["expires"] <= now:
                OAUTH_PENDING.pop(old_state, None)
        OAUTH_PENDING[oauth_state] = {"client": client, "expires": now + 600}
    session["fyers_oauth_state"] = oauth_state
    socketio.emit("login_status", {"status": "waiting", "source": "auto"})
    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/auth/fyers/callback", methods=["GET"])
def fyers_callback():
    oauth_state = request.args.get("state", "")
    auth_code = request.args.get("auth_code", "")
    if not oauth_state or not hmac.compare_digest(oauth_state, session.get("fyers_oauth_state", "")):
        socketio.emit("login_status", {"status": "error", "message": "OAuth state did not match. Start Login again.", "source": "auto"})
        return render_template("auth_callback.html", success=False,
                               message="This login link expired or did not match your browser session. Start Login again."), 400
    with OAUTH_PENDING_LOCK:
        pending = OAUTH_PENDING.pop(oauth_state, None)
    session.pop("fyers_oauth_state", None)
    if not pending or pending["expires"] < time.time():
        socketio.emit("login_status", {"status": "error", "message": "The login request expired. Start Login again.", "source": "auto"})
        return render_template("auth_callback.html", success=False,
                               message="The login request expired. Return to the dashboard and start Login again."), 400
    if not auth_code:
        message = request.args.get("error", "FYERS did not return an authorization code.")
        socketio.emit("login_status", {"status": "error", "message": message, "source": "auto"})
        return render_template("auth_callback.html", success=False, message=message), 400

    client = pending["client"]
    try:
        client.complete_login(auth_code)
        with STATE_LOCK:
            STATE["kite_client"] = client
            STATE["logged_in"] = True
        _ensure_journal_sync()
        socketio.emit("login_status", {"status": "success", "source": "auto"})
        _resume_existing_position_if_any(_make_log())
        return render_template("auth_callback.html", success=True,
                               message="FYERS login complete. Return to the dashboard.")
    except Exception as exc:  # noqa: BLE001
        _make_log()(f"[fyers] callback token exchange failed: {exc!r}")
        socketio.emit("login_status", {"status": "error", "message": str(exc), "source": "auto"})
        return render_template("auth_callback.html", success=False,
                               message="FYERS could not verify this login code. Return to the dashboard and try again."), 400


@app.route("/api/login/manual", methods=["POST"])
def login_manual():
    """
    Fallback token exchange for a user who needs to paste an authorization
    code instead of completing the hosted callback automatically.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is running - stop it before logging in again"}), 400

    settings = load_settings()
    if not settings.get("fyers_client_id") or not settings.get("fyers_secret_key"):
        return jsonify({"ok": False, "error": "Set FYERS_CLIENT_ID and FYERS_SECRET_KEY in the server environment."}), 400

    incoming = request.get_json(force=True, silent=True) or {}
    raw_input = str(incoming.get("token_input", ""))

    try:
        request_token = extract_request_token(raw_input)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    log = _make_log()

    def _do_manual_login():
        try:
            client = FyersClient(settings, log=log)
            client.complete_login(request_token)
            with STATE_LOCK:
                STATE["kite_client"] = client
                STATE["logged_in"] = True
            _ensure_journal_sync()
            socketio.emit("login_status", {"status": "success", "source": "manual"})
            _resume_existing_position_if_any(log)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI, never crash the server
            log(f"[app] manual login failed: {exc!r}")
            socketio.emit("login_status", {"status": "error", "message": str(exc), "source": "manual"})

    threading.Thread(target=_do_manual_login, daemon=True).start()
    return jsonify({"ok": True, "message": "Verifying token..."})


def _make_event_relays(run_kind="execute"):
    """Shared by /api/execute, /api/manual_trade, and /api/analyze - all
    relay the exact same engine.py event/tick shapes to the browser over
    WebSocket."""
    run_id = uuid.uuid4().hex
    signal_id = None
    try:
        trading_journal.start_recommendation_run(
            run_id, run_kind, payload={"configuration": load_settings()})
    except Exception as exc:
        _make_log()(f"[journal] could not record run start: {exc!r}")

    def on_event(evt):
        nonlocal signal_id
        evt = dict(evt)
        evt["run_id"] = run_id
        universe = None
        if evt.get("type") == "universe_ready":
            with STATE_LOCK:
                STATE["last_universe_event"] = evt
                _save_last_analysis_to_disk()
        elif evt.get("type") == "decision":
            with STATE_LOCK:
                STATE["last_decision_event"] = evt
                universe = STATE.get("last_universe_event")
                _save_last_analysis_to_disk()
            if not evt.get("manual"):
                signal_id = signal_id or uuid.uuid4().hex
                evt["signal_id"] = signal_id
                try:
                    journal_event = dict(evt)
                    journal_event["run_kind"] = run_kind
                    trading_journal.record_recommendation(
                        journal_event, universe, load_settings(), signal_id=signal_id,
                        execution_attempt_id=run_id if run_kind == "execute" else None,
                    )
                except Exception as exc:  # journal persistence must never interrupt engine safety logic
                    _make_log()(f"[journal] could not save recommendation: {exc!r}")
                trading_journal.record_recommendation_run_event(
                    run_id, "signal_decision", evt, signal_id=signal_id,
                    status="no_trade" if evt.get("direction") == "NO_TRADE" else "decision_recorded",
                    result=evt.get("direction"),
                )
            else:
                trading_journal.record_recommendation_run_event(
                    run_id, "manual_direction_selected", evt,
                    status="manual_entry_selected", result=evt.get("direction"),
                )
        elif evt.get("type") in {"positions_entered", "exit", "leg_exit", "no_trade", "error"}:
            if signal_id:
                evt["signal_id"] = signal_id
            if evt.get("type") == "positions_entered":
                # The engine retains the broker order IDs and order API response,
                # but this event alone is not proof of a fill. Keep execution
                # analytics explicitly separate from signal outcomes.
                evt["execution_confirmation"] = "order_acknowledgement_only_fill_unconfirmed"
            status_by_type = {
                "positions_entered": "order_acknowledged_fill_unconfirmed",
                "exit": "position_exit_reported",
                "leg_exit": "position_leg_exit_reported",
                "no_trade": "no_trade",
                "error": "failed",
            }
            trading_journal.record_recommendation_run_event(
                run_id, evt["type"], evt, signal_id=signal_id,
                status=status_by_type[evt["type"]], result=evt.get("reason") or evt.get("direction"),
            )
        socketio.emit("engine_event", evt)

    def on_tick(tick):
        socketio.emit("tick", {
            "instrument_token": tick.get("instrument_token"),
            "last_price": tick.get("last_price"),
            "average_price": tick.get("average_price"),
            "oi": tick.get("oi"),
        })

    return on_event, on_tick


@app.route("/api/journal", methods=["GET"])
def journal_data():
    try:
        limit = request.args.get("limit", 100, type=int)
        return jsonify({"ok": True, **trading_journal.fetch_journal(limit)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not read journal: {exc}"}), 500


@app.route("/api/journal/performance", methods=["GET"])
def journal_performance():
    try:
        return jsonify({"ok": True, **trading_journal.historical_performance_report()})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not calculate recommendation performance: {exc}"}), 500


@app.route("/api/journal/training-data", methods=["GET"])
def journal_training_data():
    try:
        min_train = request.args.get("min_train", 100, type=int)
        test_size = request.args.get("test_size", 30, type=int)
        return jsonify({"ok": True, **trading_journal.fetch_training_records(min_train, test_size)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not prepare chronological signal data: {exc}"}), 500


def _resume_existing_position_if_any(log):
    """
    Called right after every successful login (automatic and manual-token
    flows alike). If this process was just (re)started while a real
    position was already open at FYERS - e.g. the app crashed or was
    restarted mid-trade - this reconciles from FYERS's own real position
    data and resumes the exact same SL/target/time-exit monitoring run()
    uses, automatically, with no click needed: the position "just is what it
    is" on FYERS's side regardless of this app's process lifecycle, same
    as it would be if you'd closed and reopened the FYERS app itself. A
    fast no-op on the normal login where nothing matching is open.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        stop_event = threading.Event()
        STATE["stop_event"] = stop_event
        kite_client = STATE["kite_client"]

    on_event, on_tick = _make_event_relays("resume")
    _spawn_engine_thread(
        lambda: engine.resume_existing_position(
            stop_event=stop_event, settings_path=SETTINGS_PATH, log=log,
            kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log, on_error=on_event,
    )


def _spawn_engine_thread(runnable, log, on_error=None):
    """Shared STATE/thread/socketio boilerplate for both the automated
    Execute run and the manual direction override - the only difference
    between them is which engine function `runnable` actually calls."""
    def _run():
        try:
            runnable()
        except Exception as exc:  # noqa: BLE001 - surface to UI, never crash the server process
            log(f"[app] engine run failed: {exc!r}")
            if on_error is not None:
                on_error({"type": "error", "message": str(exc)})
            socketio.emit("engine_event", {"type": "error", "message": str(exc)})
        finally:
            with STATE_LOCK:
                STATE["running"] = False
                STATE["stop_event"] = None
            socketio.emit("engine_status", {"running": False})

    socketio.emit("engine_status", {"running": True})
    threading.Thread(target=_run, daemon=True).start()


@app.route("/api/execute", methods=["POST"])
def execute():
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "please Login first"}), 400
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is already running"}), 400
        STATE["running"] = True
        stop_event = threading.Event()
        STATE["stop_event"] = stop_event
        kite_client = STATE["kite_client"]

    log = _make_log()
    on_event, on_tick = _make_event_relays("execute")

    _spawn_engine_thread(
        lambda: engine.run(
            stop_event=stop_event, settings_path=SETTINGS_PATH, log=log,
            kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log, on_error=on_event,
    )
    return jsonify({"ok": True})


@app.route("/api/manual_trade", methods=["POST"])
def manual_trade():
    """
    Manual override: the user picks the direction directly, skipping the
    automated analysis entirely. Still goes through engine.manual_run(),
    which uses the exact same order-placement and SL/target/time-exit/
    force-exit monitoring code as the automated Execute path - only how the
    direction was chosen differs, never the safety net around it.
    """
    incoming = request.get_json(force=True, silent=True) or {}
    direction = str(incoming.get("direction", "")).upper()
    if direction not in engine.VALID_MANUAL_DIRECTIONS:
        return jsonify({"ok": False, "error": "direction must be CALL, PUT, or BOTH"}), 400

    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "please Login first"}), 400
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is already running"}), 400
        STATE["running"] = True
        stop_event = threading.Event()
        STATE["stop_event"] = stop_event
        kite_client = STATE["kite_client"]

    log = _make_log()
    on_event, on_tick = _make_event_relays("manual_entry")

    _spawn_engine_thread(
        lambda: engine.manual_run(
            direction, stop_event=stop_event, settings_path=SETTINGS_PATH, log=log,
            kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log, on_error=on_event,
    )
    return jsonify({"ok": True, "direction": direction})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Runs the exact same analysis engine.run() itself uses (engine.analyze_only,
    sharing engine._run_analysis with it) and reports the decision - but
    NEVER places an order, in any mode (dry_run or LIVE). For watching what
    the system would decide without committing to a trade.
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "please Login first"}), 400
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is already running"}), 400
        STATE["running"] = True
        STATE["stop_event"] = None  # nothing to interrupt - analysis-only has no monitoring loop
        kite_client = STATE["kite_client"]

    log = _make_log()
    on_event, on_tick = _make_event_relays("analyze_only")

    _spawn_engine_thread(
        lambda: engine.analyze_only(
            settings_path=SETTINGS_PATH, log=log, kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log, on_error=on_event,
    )
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    with STATE_LOCK:
        stop_event = STATE["stop_event"]
        running = STATE["running"]
    if not running or stop_event is None:
        return jsonify({"ok": False, "error": "engine is not running"}), 400
    stop_event.set()
    return jsonify({"ok": True, "message": "Stop signal sent - squaring off any open position now."})


@app.route("/api/status", methods=["GET"])
def status():
    with STATE_LOCK:
        result = {"logged_in": STATE["logged_in"], "running": STATE["running"]}
    result.update(restart_status())
    return jsonify(result)


@app.route("/api/last_analysis", methods=["GET"])
def last_analysis():
    """The last "universe_ready"/"decision" engine_event this server has
    seen (if any), so the dashboard can redisplay the same analysis on a
    page refresh instead of resetting to blank - see dashboard.js's
    loadLastAnalysis(). Read-only, no Kite/login needed - just whatever is
    already cached in memory from the last Execute/Analyze Only/manual
    entry since this server process started."""
    with STATE_LOCK:
        return jsonify({
            "universe": STATE["last_universe_event"],
            "decision": STATE["last_decision_event"],
        })


def _try_restore_session():
    """
    On startup, if today's FYERS access token is already cached in
    token.json (e.g. the app was closed/restarted, or the machine slept,
    earlier the same day), silently restore the logged-in session instead
    of making the user click Login again - Kite's own token stays valid
    until ~6 AM the next day regardless of whether this process kept
    running. Never opens a browser or blocks: if there's no valid cached
    token, this just does nothing and Login works as normal.
    """
    settings = load_settings()
    if not settings.get("fyers_client_id") or not settings.get("fyers_secret_key"):
        return
    cached_token = load_cached_access_token(settings.get("fyers_client_id"))
    if not cached_token:
        return
    try:
        client = FyersClient(settings, log=print)
        client.set_access_token(cached_token)
        with STATE_LOCK:
            STATE["kite_client"] = client
            STATE["logged_in"] = True
        _ensure_journal_sync()
        print("[app] restored today's FYERS session from token.json - no need to log in again")
        # This silent-restore path counts as "logged in" exactly like
        # /api/login and /api/login/manual - a position left open from
        # before this restart must be reconciled here too, not just on
        # those two routes (see _resume_existing_position_if_any).
        _resume_existing_position_if_any(print)
    except Exception as exc:  # noqa: BLE001 - never prevent the server from starting over this
        print(f"[app] could not restore cached session ({exc!r}) - Login will be needed")


if __name__ == "__main__":
    _try_restore_session()
    # Bind to all interfaces by default so the dashboard is reachable on a
    # cloud VM. Restrict inbound access with the VM/cloud firewall.
    host = os.environ.get("HOST", "0.0.0.0")
    local_url = f"http://127.0.0.1:{PORT}"
    if host in {"127.0.0.1", "localhost"}:
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()
        print(f"[app] starting dashboard at {local_url}")
    else:
        print(f"[app] starting dashboard bound to {host}:{PORT} - "
              f"open http://<this-machine's-public-IP>:{PORT} from your browser")
    socketio.run(app, host=host, port=PORT, allow_unsafe_werkzeug=True)
