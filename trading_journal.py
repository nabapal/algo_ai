"""Persistent, append-oriented trading activity and recommendation journal."""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app_config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "trading_journal.sqlite3")
IST = ZoneInfo("Asia/Kolkata")
SNAPSHOT_SCHEMA_VERSION = 1
REPORT_MIN_SAMPLE = 10
SECRET_KEYS = {
    "fyers_client_id", "fyers_secret_key", "fyers_redirect_uri", "gemini_api_key",
    "anthropic_api_key", "openai_api_key", "tavily_api_key", "proxy_username",
    "proxy_password", "user", "pass", "access_token", "authorization",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _sanitize(value):
    if isinstance(value, dict):
        return {str(key): _sanitize(child) for key, child in value.items()
                if str(key).lower() not in SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_sanitize(child) for child in value]
    return value


def code_version():
    """Fingerprint the decision-producing source files; does not require Git metadata."""
    digest = hashlib.sha256()
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("engine.py", "option_signal.py", "decision.py", "ai_sentiment.py"):
        digest.update(name.encode("utf-8"))
        try:
            with open(os.path.join(base, name), "rb") as source:
                digest.update(source.read())
        except OSError:
            digest.update(b"unavailable")
    return "sha256:" + digest.hexdigest()


def _ensure_column(db, table, name, definition):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
        CREATE TABLE IF NOT EXISTS recommendation_runs (
            run_id TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL,
            run_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            signal_id TEXT,
            result TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_runs_signal ON recommendation_runs(signal_id);
        CREATE TABLE IF NOT EXISTS recommendation_run_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            signal_id TEXT,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_events_run ON recommendation_run_events(run_id, occurred_at);
        CREATE TABLE IF NOT EXISTS journal_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        _ensure_column(db, "recommendations", "snapshot_json", "TEXT")
        _ensure_column(db, "recommendations", "code_version", "TEXT")
        _ensure_column(db, "recommendations", "input_quality_json", "TEXT")
        _ensure_column(db, "recommendations", "execution_attempt_id", "TEXT")
        _ensure_column(db, "recommendations", "signal_kind", "TEXT NOT NULL DEFAULT 'prediction'")


def record_recommendation(event, universe=None, strategy=None, signal_id=None, execution_attempt_id=None):
    universe = universe or {}
    strategy = _sanitize(strategy or {})
    event = _sanitize(event or {})
    universe = _sanitize(universe)
    now = datetime.now(timezone.utc)
    try:
        signal_time = datetime.fromisoformat(str(event.get("signal_time_utc")))
        now = signal_time.astimezone(timezone.utc) if signal_time.tzinfo else now
    except (TypeError, ValueError):
        pass
    local_day = now.astimezone(IST).date().isoformat()
    signal_id = str(signal_id or event.get("signal_id") or uuid.uuid4().hex)
    quality = event.get("input_quality") or {}
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "signal_id": signal_id,
        "captured_at": now.isoformat(),
        "signal_event": event,
        "universe": universe,
        "configuration": strategy,
        "code_version": code_version(),
    }
    initial_outcomes = {horizon: {"status": "pending", "reason": "awaiting future market observations"}
                        for horizon in ("15m", "30m", "60m", "close")}
    with _connect() as db:
        db.execute("""INSERT OR IGNORE INTO recommendations
            (id, created_at, trading_date, index_name, direction, manual, spot_price,
             upside_probability, downside_probability, payload_json, universe_json, strategy_json,
             outcomes_json, snapshot_json, code_version, input_quality_json, execution_attempt_id, signal_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            signal_id, now.isoformat(), local_day,
            strategy.get("index") or universe.get("index"), event.get("direction"),
            int(bool(event.get("manual"))), event.get("spot_price", universe.get("spot_price")),
            event.get("upside_probability"), event.get("downside_probability"),
            _json(event), _json(universe) if universe else None, _json(strategy),
            _json(initial_outcomes), _json(snapshot), snapshot["code_version"], _json(quality), execution_attempt_id,
            "prediction",
        ))
    return signal_id


def start_recommendation_run(run_id, run_kind, payload=None, requested_at=None):
    run_id = str(run_id or uuid.uuid4().hex)
    requested_at = requested_at or datetime.now(timezone.utc).isoformat()
    run_kind = str(run_kind or "unknown")
    status = "execution_attempted" if run_kind in ("execute", "manual_entry") else "analysis_started"
    with _connect() as db:
        db.execute("""INSERT OR IGNORE INTO recommendation_runs
            (run_id, requested_at, run_kind, status, payload_json) VALUES (?, ?, ?, ?, ?)""",
                   (run_id, requested_at, run_kind, status, _json(_sanitize(payload or {}))))
    return run_id


def record_recommendation_run_event(run_id, event_type, payload=None, signal_id=None,
                                    status=None, result=None, event_id=None):
    payload = _sanitize(payload or {})
    occurred_at = datetime.now(timezone.utc).isoformat()
    event_id = str(event_id or hashlib.sha256(
        f"{run_id}:{event_type}:{_json(payload)}".encode("utf-8")).hexdigest())
    with _connect() as db:
        db.execute("""INSERT OR IGNORE INTO recommendation_run_events
            (event_id, run_id, signal_id, event_type, occurred_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
                   (event_id, str(run_id), signal_id, str(event_type), occurred_at, _json(payload)))
        if status is not None or signal_id is not None or result is not None:
            assignments = []
            values = []
            for column, value in (("status", status), ("signal_id", signal_id), ("result", result)):
                if value is not None:
                    assignments.append(f"{column}=?")
                    values.append(value)
            if assignments:
                values.append(str(run_id))
                db.execute(f"UPDATE recommendation_runs SET {', '.join(assignments)} WHERE run_id=?", values)
    return event_id


def pending_recommendations(trading_date=None, limit=1000):
    with _connect() as db:
        query = """SELECT id, created_at, trading_date, index_name, direction, spot_price,
            universe_json, payload_json, outcomes_json, snapshot_json FROM recommendations
            WHERE manual=0"""
        params = []
        if trading_date:
            query += " AND trading_date=?"
            params.append(trading_date)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def update_recommendation_outcomes(signal_id, outcomes):
    with _connect() as db:
        row = db.execute("SELECT outcomes_json FROM recommendations WHERE id=?", (signal_id,)).fetchone()
        if not row:
            return False
        try:
            current = json.loads(row["outcomes_json"] or "{}")
        except (TypeError, ValueError):
            current = {}
        for horizon, result in (outcomes or {}).items():
            previous = current.get(horizon)
            if isinstance(previous, dict) and previous.get("status") == "ready":
                continue
            if isinstance(result, dict):
                current[horizon] = result
        db.execute("UPDATE recommendations SET outcomes_json=? WHERE id=?", (_json(current), signal_id))
    return True


def build_horizon_outcome(start_price, samples, direction, target_at, required_moves=None,
                          option_entry_premiums=None, option_exit_premiums=None):
    """Build explicit labels from real observations; unavailable is never treated as zero."""
    if start_price is None or not samples:
        return {"status": "unavailable", "reason": "no spot observations for this horizon"}
    points = []
    for sample in samples:
        try:
            points.append((str(sample["timestamp"]), float(sample["price"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(key=lambda item: item[0])
    if not points:
        return {"status": "unavailable", "reason": "spot observations were invalid"}
    target_text = str(target_at)
    after_target = [item for item in points if item[0] >= target_text]
    if not after_target:
        return {"status": "pending", "reason": "target observation is not available yet"}

    endpoint_time, endpoint_price = after_target[0]
    baseline = float(start_price)
    observed_moves = [price - baseline for stamp, price in points if stamp <= endpoint_time]
    raw_move = endpoint_price - baseline
    side = str(direction or "").upper()
    multiplier = 1 if side == "CALL" else -1 if side == "PUT" else None
    oriented_moves = [move * multiplier for move in observed_moves] if multiplier else observed_moves
    directional_move = raw_move * multiplier if multiplier else None
    if multiplier:
        directional_label = "correct" if directional_move > 0 else "incorrect" if directional_move < 0 else "breakeven"
    else:
        directional_label = "not_applicable"
    outcome = {
        "status": "ready", "target_at": target_text, "observed_at": endpoint_time,
        "spot_price": endpoint_price, "signed_move_points": raw_move,
        "directional_move_points": directional_move,
        "mfe_points": max([0.0, *oriented_moves]),
        "mae_points": min([0.0, *oriented_moves]),
        "directional_label": directional_label,
    }
    required_moves = required_moves or {}
    if side == "BOTH":
        outcome["both_leg_profitability"] = {
            "status": "unavailable", "reason": "future option-leg prices were not recorded"}
        threshold = required_moves.get("both")
        if threshold is not None:
            outcome["both_breakeven_proxy"] = {
                "method": "absolute_underlying_move_vs_signal_premium_threshold",
                "threshold_points": float(threshold), "cleared": abs(raw_move) >= float(threshold),
            }
    elif side == "NO_TRADE":
        call_threshold, put_threshold = required_moves.get("call"), required_moves.get("put")
        outcome["opportunity_analysis"] = {
            "method": "underlying_move_vs_signal_premium_threshold_proxy",
            "call_threshold_reached": raw_move >= float(call_threshold) if call_threshold is not None else None,
            "put_threshold_reached": raw_move <= -float(put_threshold) if put_threshold is not None else None,
            "status": "ready" if call_threshold is not None and put_threshold is not None else "partial",
        }
    if option_entry_premiums is not None and option_exit_premiums is not None:
        try:
            net_points = sum(float(option_exit_premiums[k]) - float(option_entry_premiums[k])
                             for k in ("call", "put"))
            outcome["both_leg_profitability"] = {
                "status": "ready", "gross_option_points": net_points,
                "label": "profitable" if net_points > 0 else "loss" if net_points < 0 else "breakeven",
                "actual_execution": False, "charges_included": False,
            }
        except (KeyError, TypeError, ValueError):
            pass
    return outcome


def chronological_walk_forward(records, min_train=100, test_size=30):
    """Create chronological folds for future model evaluation; never shuffle."""
    unique = {}
    for row in records:
        identity = row.get("signal_id") or row.get("id")
        if identity is not None:
            unique.setdefault(str(identity), row)
    ordered = sorted(unique.values(), key=lambda row: (row.get("created_at") or "", row.get("signal_id") or row.get("id") or ""))
    folds = []
    train_end = max(1, int(min_train))
    test_size = max(1, int(test_size))
    while train_end < len(ordered):
        test_end = min(len(ordered), train_end + test_size)
        folds.append({"train_ids": [row.get("signal_id") or row.get("id") for row in ordered[:train_end]],
                      "test_ids": [row.get("signal_id") or row.get("id") for row in ordered[train_end:test_end]],
                      "train_through": ordered[train_end - 1].get("created_at"),
                      "test_from": ordered[train_end].get("created_at"),
                      "test_through": ordered[test_end - 1].get("created_at")})
        train_end = test_end
    return folds


def historical_performance_report():
    with _connect() as db:
        records = db.execute("""SELECT id, created_at, direction, upside_probability,
            outcomes_json, snapshot_json FROM recommendations WHERE manual=0 ORDER BY created_at ASC""").fetchall()
    grouped = {key: {} for key in ("by_horizon", "by_decision", "by_probability_bucket", "by_market_regime")}
    prediction_count = len(records)
    for raw in records:
        row = dict(raw)
        try:
            outcomes = json.loads(row.get("outcomes_json") or "{}")
        except (TypeError, ValueError):
            outcomes = {}
        try:
            snapshot = json.loads(row.get("snapshot_json") or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        event = snapshot.get("signal_event") or {}
        confidence = event.get("volatility_confidence")
        volatility = ("unknown" if confidence is None else "low" if float(confidence) < 40
                      else "high" if float(confidence) >= 65 else "moderate")
        gap = event.get("large_gap")
        gap_regime = "large_gap" if gap is True else "no_large_gap" if gap is False else "unknown_gap"
        probability = row.get("upside_probability")
        if probability is None:
            bucket = "unknown"
        else:
            lower = min(90, max(0, int(float(probability) * 100) // 10 * 10))
            bucket = f"{lower:02d}-{lower + 10}%"
        for horizon, outcome in outcomes.items():
            if not isinstance(outcome, dict) or outcome.get("status") != "ready":
                continue
            item = {**row, "outcome": outcome}
            values = (("by_horizon", horizon), ("by_decision", row.get("direction") or "unknown"),
                      ("by_probability_bucket", bucket), ("by_market_regime", f"volatility:{volatility}"),
                      ("by_market_regime", f"gap:{gap_regime}"))
            for dimension, value in values:
                grouped[dimension].setdefault((horizon, value), []).append(item)

    def summarize(items, horizon, dimension, value):
        labels = [item["outcome"].get("directional_label") for item in items]
        directional = [label for label in labels if label in ("correct", "incorrect")]
        # A recorded zero is a real flat outcome; only missing values are excluded
        # from probability calibration. Directional accuracy still excludes ties.
        moves = [item for item in items if item["outcome"].get("signed_move_points") is not None]
        accuracy = sum(label == "correct" for label in directional) / len(directional) if directional else None
        predicted = sum(float(item.get("upside_probability") or 0) for item in moves) / len(moves) if moves else None
        observed = (sum(float(item["outcome"]["signed_move_points"]) > 0 for item in moves) / len(moves)
                    if moves else None)
        brier = (sum((float(item.get("upside_probability") or 0) -
                      (1.0 if float(item["outcome"]["signed_move_points"]) > 0 else 0.0)) ** 2
                     for item in moves) / len(moves)) if moves else None
        return {"horizon": horizon, "dimension": dimension, "value": value,
                "sample_count": len(items), "directional_sample_count": len(directional),
                "breakeven_count": labels.count("breakeven"), "directional_accuracy": accuracy,
                "mean_predicted_up_probability": predicted, "observed_up_frequency": observed,
                "brier_score": brier,
                "no_trade_call_opportunity_count": sum(
                    item["outcome"].get("opportunity_analysis", {}).get("call_threshold_reached") is True
                    for item in items),
                "no_trade_put_opportunity_count": sum(
                    item["outcome"].get("opportunity_analysis", {}).get("put_threshold_reached") is True
                    for item in items),
                "both_leg_profitable_count": sum(
                    item["outcome"].get("both_leg_profitability", {}).get("label") == "profitable"
                    for item in items),
                "sample_assessment": "insufficient" if len(items) < REPORT_MIN_SAMPLE else "descriptive",
                "net_profitability": {"available": False, "sample_count": 0, "net_pnl": None,
                    "note": "No linked actual execution P&L is stored; signal outcomes are not account P&L."}}

    report = {"prediction_count": prediction_count,
              "ready_horizon_outcomes": sum(len(items) for items in grouped["by_horizon"].values()),
              "minimum_sample": REPORT_MIN_SAMPLE,
              "validation": "not run; no trained model exists",
              "chronological_walk_forward": "required before any future model evaluation",
              **{name: [summarize(items, horizon, name.removeprefix("by_"), value)
                       for (horizon, value), items in sorted(groups.items())]
                 for name, groups in grouped.items()}}
    return report


def get_meta(key):
    with _connect() as db:
        row = db.execute("SELECT value FROM journal_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key, value):
    with _connect() as db:
        db.execute("INSERT INTO journal_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))


def get_active_profit_lock_positions():
    """Return position fingerprints whose combined profit lock was activated."""
    try:
        value = json.loads(get_meta("profit_lock_active_positions") or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(item) for item in value} if isinstance(value, list) else set()


def set_active_profit_lock_positions(position_keys):
    set_meta("profit_lock_active_positions", _json(sorted({str(key) for key in position_keys})))


def fetch_journal(limit=100):
    limit = max(1, min(int(limit), 500))
    with _connect() as db:
        recommendations = db.execute("""SELECT id, created_at, trading_date, index_name, direction,
            manual, spot_price, upside_probability, downside_probability, payload_json,
            universe_json, strategy_json, outcomes_json, code_version, signal_kind, execution_attempt_id
            FROM recommendations
            ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    def expand(row, json_fields=()):
        value = dict(row)
        for field in json_fields:
            try:
                value[field.removesuffix("_json")] = json.loads(value[field]) if value.get(field) else None
            except (TypeError, ValueError):
                value[field.removesuffix("_json")] = None
        return value
    recs = [expand(row, ("payload_json", "universe_json", "strategy_json", "outcomes_json")) for row in recommendations]
    for row in recs:
        row["signal_id"] = row.get("id")
    return {"recommendations": recs}


def fetch_training_records(min_train=100, test_size=30):
    """Expose timestamp-safe, signal-time features alongside later labels, oldest first."""
    with _connect() as db:
        rows = db.execute("""SELECT id, created_at, direction, upside_probability,
            snapshot_json, outcomes_json, manual FROM recommendations
            WHERE manual=0 ORDER BY created_at ASC, id ASC""").fetchall()
    dataset = []
    for raw in rows:
        row = dict(raw)
        try:
            snapshot = json.loads(row.get("snapshot_json") or "{}")
            event = snapshot.get("signal_event") or {}
            features = event.get("training_snapshot")
        except (TypeError, ValueError):
            snapshot, features = {}, None
        try:
            outcomes = json.loads(row.get("outcomes_json") or "{}")
        except (TypeError, ValueError):
            outcomes = {}
        for horizon in ("15m", "30m", "60m", "close"):
            outcome = outcomes.get(horizon)
            dataset.append({
                "signal_id": row["id"], "created_at": row["created_at"], "horizon": horizon,
                "decision": row["direction"], "upside_probability": row["upside_probability"],
                "features": features,
                "label": outcome if isinstance(outcome, dict) else {"status": "unavailable", "reason": "not recorded"},
                "feature_timestamp": snapshot.get("captured_at"),
                "code_version": snapshot.get("code_version"),
                "feature_availability": "available" if features is not None else "legacy_snapshot_unavailable",
            })
    return {"records": dataset,
            "walk_forward_folds": chronological_walk_forward(dataset, min_train=min_train, test_size=test_size),
            "feature_policy": "Features are copied only from immutable signal-time snapshots; outcomes are labels and never features."}


initialize()
