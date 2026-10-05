"""Persistent, append-oriented trading activity and recommendation journal."""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app_config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "trading_journal.sqlite3")
IST = ZoneInfo("Asia/Kolkata")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    if os.name != "nt" and os.path.exists(DB_PATH):
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def initialize():
    with _connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            index_name TEXT,
            direction TEXT,
            manual INTEGER NOT NULL DEFAULT 0,
            spot_price REAL,
            upside_probability REAL,
            downside_probability REAL,
            payload_json TEXT NOT NULL,
            universe_json TEXT,
            strategy_json TEXT NOT NULL,
            outcomes_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_recommendations_date ON recommendations(trading_date, created_at DESC);
        CREATE TABLE IF NOT EXISTS broker_activity (
            event_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            broker_id TEXT,
            parent_id TEXT,
            event_time TEXT,
            synced_at TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            quantity REAL,
            price REAL,
            status TEXT,
            source TEXT,
            product TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_activity_time ON broker_activity(synced_at DESC);
        CREATE TABLE IF NOT EXISTS charge_reports (
            report_key TEXT PRIMARY KEY,
            report_date TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            total REAL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_charges_date ON charge_reports(report_date DESC);
        CREATE TABLE IF NOT EXISTS journal_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)


def record_recommendation(event, universe=None, strategy=None):
    now = datetime.now(timezone.utc)
    local_day = now.astimezone(IST).date().isoformat()
    universe = universe or {}
    strategy = dict(strategy or {})
    for key in (
        "fyers_client_id", "fyers_secret_key", "fyers_redirect_uri",
        "gemini_api_key", "anthropic_api_key", "openai_api_key",
    ):
        strategy.pop(key, None)
    if isinstance(strategy.get("proxy"), dict):
        strategy["proxy"] = {key: value for key, value in strategy["proxy"].items() if key not in {"user", "pass"}}
    signal_id = hashlib.sha256(f"{now.isoformat()}:{_json(event)}".encode("utf-8")).hexdigest()[:24]
    with _connect() as db:
        db.execute("""INSERT OR IGNORE INTO recommendations
            (id, created_at, trading_date, index_name, direction, manual, spot_price,
             upside_probability, downside_probability, payload_json, universe_json, strategy_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            signal_id, now.isoformat(), local_day,
            strategy.get("index") or universe.get("index"), event.get("direction"),
            int(bool(event.get("manual"))), event.get("spot_price", universe.get("spot_price")),
            event.get("upside_probability"), event.get("downside_probability"),
            _json(event), _json(universe) if universe else None, _json(strategy),
        ))
    return signal_id


def pending_recommendations(trading_date, limit=200):
    with _connect() as db:
        rows = db.execute("""SELECT id, created_at, index_name, direction, spot_price,
            universe_json, outcomes_json FROM recommendations
            WHERE trading_date=? AND manual=0 ORDER BY created_at DESC LIMIT ?""",
            (trading_date, limit)).fetchall()
    return [dict(row) for row in rows]


def update_recommendation_outcomes(signal_id, outcomes):
    with _connect() as db:
        db.execute("UPDATE recommendations SET outcomes_json=? WHERE id=?",
                   (_json(outcomes), signal_id))


def _pick(record, *keys):
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _side(record):
    value = _pick(record, "side", "transaction_type", "transactionType", "orderSide")
    if str(value).strip() in {"1", "+1"}:
        return "BUY"
    if str(value).strip() in {"-1", "2"}:
        return "SELL"
    return str(value).upper() if value is not None else None


def store_broker_records(kind, records):
    if not isinstance(records, list):
        return 0
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    with _connect() as db:
        for record in records:
            if not isinstance(record, dict):
                continue
            if kind == "order":
                broker_id = str(_pick(record, "id", "orderId", "order_id", "orderNumber") or "")
                parent_id = None
            else:
                broker_id = str(_pick(record, "tradeNumber", "tradeNo", "trade_id", "id") or "")
                parent_id = _pick(record, "orderNumber", "orderId", "order_id", "id")
            raw = _json(record)
            if not broker_id:
                broker_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            event_time = str(_pick(record, "orderDateTime", "tradeDateTime", "tradeTime", "order_time", "timestamp") or "")
            event_day = event_time[:10] if len(event_time) >= 10 else datetime.now(IST).date().isoformat()
            event_key = f"{kind}:{event_day}:{broker_id}"
            db.execute("""INSERT INTO broker_activity
                (event_key, kind, broker_id, parent_id, event_time, synced_at, symbol,
                 side, quantity, price, status, source, product, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    parent_id=excluded.parent_id, event_time=excluded.event_time,
                    synced_at=excluded.synced_at, symbol=excluded.symbol, side=excluded.side,
                    quantity=excluded.quantity, price=excluded.price, status=excluded.status,
                    source=excluded.source, product=excluded.product, raw_json=excluded.raw_json""", (
                event_key, kind, broker_id, str(parent_id) if parent_id else None,
                event_time,
                now, _pick(record, "symbol", "tradingsymbol", "tradingSymbol"), _side(record),
                _number(_pick(record, *(('tradedQty', 'filledQty', 'qty', 'quantity') if kind == "trade" else ('qty', 'quantity', 'tradedQty', 'filledQty')))),
                _number(_pick(record, "tradedPrice", "tradePrice", "price", "avgPrice", "average_price")),
                str(_pick(record, "status", "orderStatus", "order_status") or ""),
                str(_pick(record, "source", "orderSource", "order_source") or "UNKNOWN"),
                str(_pick(record, "productType", "product_type", "product") or ""), raw,
            ))
            saved += 1
    return saved


def store_charge_report(trading_date, response):
    payload = response if isinstance(response, (dict, list)) else {"response": str(response)}
    raw = _json(payload)
    key = f"account:{trading_date}"
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    total = None
    if isinstance(data, dict):
        total = _number(_pick(data, "totalCharges", "total_charges", "total", "amount"))
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        db.execute("""INSERT INTO charge_reports (report_key, report_date, synced_at, total, raw_json)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(report_key) DO UPDATE SET
            synced_at=excluded.synced_at, total=excluded.total, raw_json=excluded.raw_json""",
                   (key, trading_date, now, total, raw))


def store_charge_history(from_date, to_date, response):
    """Keep actual charge rows by date where FYERS returns them; preserve raw report otherwise."""
    rows = None
    def find_rows(value):
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
        if isinstance(value, dict):
            for child in value.values():
                result = find_rows(child)
                if result is not None:
                    return result
        return None
    rows = find_rows(response)
    if rows:
        for row in rows:
            date_value = _pick(row, "date", "tradeDate", "tradingDate", "reportDate")
            day = str(date_value or to_date)[:10]
            store_charge_report(day, row)
    else:
        store_charge_report(to_date, {"from_date": from_date, "to_date": to_date, "report": response})


def get_meta(key):
    with _connect() as db:
        row = db.execute("SELECT value FROM journal_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key, value):
    with _connect() as db:
        db.execute("INSERT INTO journal_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))


def fetch_journal(limit=100):
    limit = max(1, min(int(limit), 500))
    with _connect() as db:
        recommendations = db.execute("""SELECT * FROM recommendations
            ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
        activity = db.execute("""SELECT * FROM broker_activity
            ORDER BY synced_at DESC LIMIT ?""", (limit * 3,)).fetchall()
        charges = db.execute("""SELECT * FROM charge_reports
            ORDER BY report_date DESC, synced_at DESC LIMIT ?""", (min(limit, 90),)).fetchall()
    def expand(row, json_fields=()):
        value = dict(row)
        for field in json_fields:
            try:
                value[field.removesuffix("_json")] = json.loads(value[field]) if value.get(field) else None
            except (TypeError, ValueError):
                value[field.removesuffix("_json")] = None
        return value
    recs = [expand(row, ("payload_json", "universe_json", "strategy_json", "outcomes_json")) for row in recommendations]
    events = [expand(row, ("raw_json",)) for row in activity]
    reports = [expand(row, ("raw_json",)) for row in charges]
    return {"recommendations": recs, "activity": events, "charges": reports}


initialize()
