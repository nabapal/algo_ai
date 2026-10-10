import json

import pytest

import trading_journal


@pytest.fixture
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_journal, "DB_PATH", str(tmp_path / "signals.sqlite3"))
    trading_journal.initialize()
    return trading_journal


def _event(direction="CALL", probability=0.7):
    return {
        "type": "decision", "direction": direction, "spot_price": 100.0,
        "upside_probability": probability, "downside_probability": 1 - probability,
        "training_snapshot": {"feature_schema_version": 1, "raw_inputs": {"spot": 100.0}},
        "input_quality": {"missing_inputs": []},
    }


def test_signal_insert_is_idempotent_and_snapshot_is_immutable(isolated_journal):
    signal_id = "immutable-signal-1"
    first = _event()
    isolated_journal.record_recommendation(first, {"index": "NIFTY"}, {"index": "NIFTY"}, signal_id=signal_id)
    isolated_journal.record_recommendation(_event("PUT", 0.2), {"index": "BANKNIFTY"}, {}, signal_id=signal_id)

    with isolated_journal._connect() as db:
        rows = db.execute("SELECT id, direction, snapshot_json FROM recommendations").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == signal_id
    assert rows[0]["direction"] == "CALL"
    assert json.loads(rows[0]["snapshot_json"])["signal_event"]["direction"] == "CALL"


def test_snapshot_removes_secret_values(isolated_journal):
    isolated_journal.record_recommendation(_event(), strategy={
        "index": "NIFTY", "fyers_secret_key": "must-not-persist", "proxy": {"user": "u", "pass": "p"}
    }, signal_id="safe-signal")
    with isolated_journal._connect() as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM recommendations").fetchone()[0])
    serialized = json.dumps(snapshot)
    assert "must-not-persist" not in serialized
    assert '"user"' not in serialized
    assert '"pass"' not in serialized
    assert snapshot["code_version"].startswith("sha256:")


def test_safe_migration_keeps_legacy_rows_and_existing_tables(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE recommendations (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, trading_date TEXT NOT NULL,
            index_name TEXT, direction TEXT, manual INTEGER NOT NULL DEFAULT 0,
            spot_price REAL, upside_probability REAL, downside_probability REAL,
            payload_json TEXT NOT NULL, universe_json TEXT, strategy_json TEXT NOT NULL DEFAULT '{}',
            outcomes_json TEXT NOT NULL DEFAULT '{}')""")
        db.execute("""INSERT INTO recommendations
            (id, created_at, trading_date, direction, payload_json)
            VALUES ('legacy-1', '2026-10-01T09:30:00+05:30', '2026-10-01', 'CALL', '{}')""")
        db.execute("CREATE TABLE broker_activity (event_key TEXT PRIMARY KEY, raw_json TEXT)")
        db.execute("INSERT INTO broker_activity VALUES ('preserve-me', '{}')")
    monkeypatch.setattr(trading_journal, "DB_PATH", str(path))

    trading_journal.initialize()

    with trading_journal._connect() as db:
        legacy = db.execute("SELECT id, direction, snapshot_json, signal_kind FROM recommendations").fetchone()
        activity = db.execute("SELECT event_key FROM broker_activity").fetchone()
    assert legacy["id"] == "legacy-1"
    assert legacy["direction"] == "CALL"
    assert legacy["snapshot_json"] is None
    assert legacy["signal_kind"] == "prediction"
    assert activity["event_key"] == "preserve-me"


def test_run_event_idempotency_and_failed_outcome_sync_can_recover(isolated_journal):
    isolated_journal.start_recommendation_run("run-1", "execute")
    event_id = isolated_journal.record_recommendation_run_event(
        "run-1", "order_acknowledged", {"state": "unconfirmed"},
        status="order_acknowledged_unconfirmed", event_id="event-1")
    repeated = isolated_journal.record_recommendation_run_event(
        "run-1", "order_acknowledged", {"state": "filled"},
        status="order_acknowledged_unconfirmed", event_id="event-1")
    assert repeated == event_id
    with isolated_journal._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recommendation_run_events").fetchone()[0] == 1
        assert db.execute("SELECT status FROM recommendation_runs WHERE run_id='run-1'").fetchone()[0] == "order_acknowledged_unconfirmed"

    signal_id = isolated_journal.record_recommendation(_event(), signal_id="retry-outcome")
    with isolated_journal._connect() as db:
        outcomes = json.loads(db.execute("SELECT outcomes_json FROM recommendations WHERE id=?", (signal_id,)).fetchone()[0])
    assert outcomes["15m"]["status"] == "pending"  # transient history API failure leaves it retryable
    isolated_journal.update_recommendation_outcomes(signal_id, {
        "15m": {"status": "ready", "signed_move_points": 2, "directional_label": "correct"}
    })
    with isolated_journal._connect() as db:
        outcomes = json.loads(db.execute("SELECT outcomes_json FROM recommendations WHERE id=?", (signal_id,)).fetchone()[0])
    assert outcomes["15m"]["signed_move_points"] == 2


def test_outcome_history_api_failure_stays_pending_then_recovers(isolated_journal, monkeypatch):
    import datetime
    import importlib
    import sys
    import trading_journal as journal

    # Import the application only after redirecting its migration/init to this
    # test's temporary database, so the live journal is never opened.
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    signal_time = datetime.datetime(2026, 10, 11, 3, 45, tzinfo=datetime.timezone.utc)
    signal_id = journal.record_recommendation(
        {**_event(), "signal_time_utc": signal_time.isoformat()},
        {"index": "NIFTY", "spot_symbol": "NIFTY50-INDEX", "spot_price": 100},
        signal_id="history-recovery")
    now_ist = datetime.datetime(2026, 10, 11, 10, 30, tzinfo=journal.IST)

    class Client:
        fail = True

        def get_intraday_history_for_symbol(self, symbol, start, end, interval):
            if self.fail:
                raise RuntimeError("temporary history API error")
            candles = []
            point = datetime.datetime(2026, 10, 11, 9, 15, tzinfo=journal.IST)
            while point <= now_ist:
                candles.append({"date": int(point.timestamp()), "close": 101.0})
                point += datetime.timedelta(minutes=1)
            return candles

    client = Client()
    app_module._update_recommendation_outcomes(client, now_ist)
    with journal._connect() as db:
        outcomes = json.loads(db.execute("SELECT outcomes_json FROM recommendations WHERE id=?", (signal_id,)).fetchone()[0])
    assert outcomes["15m"]["status"] == "pending"
    assert "signed_move_points" not in outcomes["15m"]

    client.fail = False
    app_module._update_recommendation_outcomes(client, now_ist)
    with journal._connect() as db:
        outcomes = json.loads(db.execute("SELECT outcomes_json FROM recommendations WHERE id=?", (signal_id,)).fetchone()[0])
    assert outcomes["15m"]["status"] == "ready"
    assert outcomes["15m"]["signed_move_points"] == 1
    assert outcomes["30m"]["status"] == "ready"
    assert outcomes["60m"]["status"] == "ready"


def test_missing_outcome_is_explicit_and_never_zero_filled(isolated_journal):
    result = isolated_journal.build_horizon_outcome(100, [], "CALL", "2026-10-11T10:00:00+05:30")
    assert result == {"status": "unavailable", "reason": "no spot observations for this horizon"}
    assert "signed_move_points" not in result


def test_call_and_put_directional_labels_and_excursions(isolated_journal):
    samples = [
        {"timestamp": "2026-10-11T09:16:00+05:30", "price": 102},
        {"timestamp": "2026-10-11T09:17:00+05:30", "price": 98},
        {"timestamp": "2026-10-11T09:18:00+05:30", "price": 103},
    ]
    call = isolated_journal.build_horizon_outcome(100, samples, "CALL", "2026-10-11T09:18:00+05:30")
    put = isolated_journal.build_horizon_outcome(100, samples, "PUT", "2026-10-11T09:18:00+05:30")
    assert (call["signed_move_points"], call["directional_label"], call["mfe_points"], call["mae_points"]) == (3, "correct", 3, -2)
    assert (put["signed_move_points"], put["directional_label"], put["mfe_points"], put["mae_points"]) == (3, "incorrect", 2, -3)


def test_both_leg_profitability_is_separate_from_spot_proxy(isolated_journal):
    samples = [{"timestamp": "2026-10-11T09:20:00+05:30", "price": 110}]
    result = isolated_journal.build_horizon_outcome(
        100, samples, "BOTH", "2026-10-11T09:20:00+05:30", required_moves={"both": 8})
    assert result["directional_label"] == "not_applicable"
    assert result["both_breakeven_proxy"]["cleared"] is True
    assert result["both_leg_profitability"]["status"] == "unavailable"

    priced = isolated_journal.build_horizon_outcome(
        100, samples, "BOTH", "2026-10-11T09:20:00+05:30",
        option_entry_premiums={"call": 10, "put": 12},
        option_exit_premiums={"call": 13, "put": 10})
    assert priced["both_leg_profitability"]["gross_option_points"] == 1
    assert priced["both_leg_profitability"]["actual_execution"] is False
    assert priced["both_leg_profitability"]["charges_included"] is False


def test_no_trade_opportunity_labels_use_thresholds(isolated_journal):
    result = isolated_journal.build_horizon_outcome(
        100, [{"timestamp": "2026-10-11T09:20:00+05:30", "price": 106}], "NO_TRADE",
        "2026-10-11T09:20:00+05:30", required_moves={"call": 5, "put": 7})
    assert result["directional_label"] == "not_applicable"
    assert result["opportunity_analysis"]["call_threshold_reached"] is True
    assert result["opportunity_analysis"]["put_threshold_reached"] is False
    assert result["opportunity_analysis"]["method"].endswith("proxy")


def test_ready_outcomes_are_not_rewritten_by_repeated_sync(isolated_journal):
    signal_id = isolated_journal.record_recommendation(_event(), signal_id="outcome-lock")
    first = {"15m": {"status": "ready", "signed_move_points": 5}}
    assert isolated_journal.update_recommendation_outcomes(signal_id, first)
    isolated_journal.update_recommendation_outcomes(signal_id, {"15m": {"status": "ready", "signed_move_points": -9}})
    with isolated_journal._connect() as db:
        outcomes = json.loads(db.execute("SELECT outcomes_json FROM recommendations WHERE id=?", (signal_id,)).fetchone()[0])
    assert outcomes["15m"]["signed_move_points"] == 5
    assert outcomes["30m"]["status"] == "pending"


def test_walk_forward_groups_signals_and_never_shuffles_future(isolated_journal):
    rows = [
        {"id": "signal-a", "created_at": "2026-01-01T09:00:00+00:00"},
        {"id": "signal-a", "created_at": "2026-01-01T09:00:00+00:00"},
        {"id": "signal-b", "created_at": "2026-01-02T09:00:00+00:00"},
        {"id": "signal-c", "created_at": "2026-01-03T09:00:00+00:00"},
    ]
    folds = isolated_journal.chronological_walk_forward(rows, min_train=1, test_size=1)
    assert folds[0]["train_ids"] == ["signal-a"]
    assert folds[0]["test_ids"] == ["signal-b"]
    assert folds[0]["train_through"] < folds[0]["test_from"]


def test_report_tracks_counts_calibration_and_unavailable_net_pnl(isolated_journal):
    signal_id = isolated_journal.record_recommendation(_event("CALL", 0.7), signal_id="report-signal")
    isolated_journal.update_recommendation_outcomes(signal_id, {
        "15m": {"status": "ready", "signed_move_points": 2, "directional_label": "correct"}
    })
    report = isolated_journal.historical_performance_report()
    row = report["by_horizon"][0]
    assert row["sample_count"] == 1
    assert row["directional_accuracy"] == 1
    assert row["mean_predicted_up_probability"] == pytest.approx(0.7)
    assert row["observed_up_frequency"] == 1
    assert row["sample_assessment"] == "insufficient"
    assert row["net_profitability"]["net_pnl"] is None


def test_flat_observation_counts_for_calibration_but_not_directional_accuracy(isolated_journal):
    signal_id = isolated_journal.record_recommendation(_event("CALL", 0.7), signal_id="flat-signal")
    isolated_journal.update_recommendation_outcomes(signal_id, {
        "15m": {"status": "ready", "signed_move_points": 0, "directional_label": "breakeven"}
    })
    row = isolated_journal.historical_performance_report()["by_horizon"][0]
    assert row["sample_count"] == 1
    assert row["directional_sample_count"] == 0
    assert row["directional_accuracy"] is None
    assert row["mean_predicted_up_probability"] == pytest.approx(0.7)
    assert row["observed_up_frequency"] == 0
