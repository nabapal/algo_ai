"""Tests for the combined-position profit-lock exit rule."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import engine


class ProfitLockRuleTests(unittest.TestCase):
    def test_does_not_activate_below_threshold(self):
        self.assertEqual(
            engine.evaluate_combined_profit_lock(1999, True, 2000, 1000),
            (False, False, False, True),
        )

    def test_activates_at_threshold(self):
        self.assertEqual(
            engine.evaluate_combined_profit_lock(2000, True, 2000, 1000),
            (True, False, True, True),
        )

    def test_activation_latches_until_floor_is_hit(self):
        active, exit_now, newly_active, valid = engine.evaluate_combined_profit_lock(
            1500, True, 2000, 1000, activated=True
        )
        self.assertEqual((active, exit_now, newly_active, valid), (True, False, False, True))
        self.assertEqual(
            engine.evaluate_combined_profit_lock(1000, True, 2000, 1000, activated=active),
            (True, True, False, True),
        )

    def test_disabling_lock_disarms_it(self):
        self.assertEqual(
            engine.evaluate_combined_profit_lock(900, False, 2000, 1000, activated=True),
            (False, False, False, True),
        )

    def test_rejects_invalid_thresholds(self):
        self.assertEqual(
            engine.evaluate_combined_profit_lock(3000, True, 1000, 1000),
            (False, False, False, False),
        )


class _FakeClient:
    def __init__(self, prices=None):
        prices = prices or {1: (1200, 600), 2: (1200, 600)}
        self.prices = {token: iter(values) for token, values in prices.items()}
        self.orders = []

    def seconds_since_last_tick(self):
        return 0

    def get_tick(self, token):
        return {"last_price": next(self.prices[token])}

    def get_actual_position_qty(self, symbol, exchange, product, expected_qty):
        return expected_qty

    def place_market_order(self, symbol, exchange, side, quantity):
        self.orders.append((symbol, side, quantity))


class _NoWaitEvent:
    def __init__(self, after_wait=None):
        self.waits = 0
        self.after_wait = after_wait

    def is_set(self):
        return False

    def wait(self, timeout=None):
        self.waits += 1
        if self.after_wait:
            self.after_wait(self.waits)


class CombinedProfitLockMonitorTests(unittest.TestCase):
    def test_combined_pnl_activates_and_exits_all_legs_at_floor(self):
        positions = [
            {"token": token, "tradingsymbol": f"OPT{token}", "type": "CALL", "qty": 1,
             "entry_price": 100, "ltp": 100, "pnl": 0}
            for token in (1, 2)
        ]
        settings = {
            "sl_enabled": False, "max_loss": 1000,
            "target_enabled": False, "target_profit": 5000,
            "profit_lock_enabled": True, "profit_lock_activation": 2000,
            "profit_lock_amount": 1000, "pnl_mode": "COMBINED",
            "time_exit_enabled": False, "time_exit": "23:59", "force_exit_time": "23:59",
        }
        events = []
        client = _FakeClient()
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as config:
            json.dump(settings, config)
            config.flush()
            with patch.object(engine, "_stored_profit_lock_positions", return_value=set()), \
                    patch.object(engine, "_save_profit_lock_positions"):
                engine.monitor_and_exit(
                    client, positions, settings, _NoWaitEvent(), lambda _message: None,
                    on_event=events.append, settings_path=config.name,
                )

        self.assertEqual(positions, [])
        self.assertEqual(len(client.orders), 2)
        self.assertEqual([event.get("type") for event in events if event.get("type") in {
            "profit_lock_activated", "exit"
        }], ["profit_lock_activated", "exit"])
        exit_event = next(event for event in events if event.get("type") == "exit")
        self.assertEqual(exit_event["reason"], "PROFIT_LOCK")
        self.assertEqual(exit_event["total_pnl"], 1000)

    def test_threshold_changes_apply_while_position_is_open(self):
        positions = [
            {"token": token, "tradingsymbol": f"OPT{token}", "type": "CALL", "qty": 1,
             "entry_price": 100, "ltp": 100, "pnl": 0}
            for token in (1, 2)
        ]
        settings = {
            "sl_enabled": False, "max_loss": 1000,
            "target_enabled": False, "target_profit": 5000,
            "profit_lock_enabled": True, "profit_lock_activation": 3000,
            "profit_lock_amount": 1000, "pnl_mode": "COMBINED",
            "time_exit_enabled": False, "time_exit": "23:59", "force_exit_time": "23:59",
        }
        events = []
        client = _FakeClient({1: (1200, 1200, 600), 2: (1200, 1200, 600)})
        config_fd, config_path = tempfile.mkstemp(suffix=".json", dir=os.getcwd())
        os.close(config_fd)
        try:
            with open(config_path, "w", encoding="utf-8") as config:
                json.dump(settings, config)

            def update_thresholds(wait_number):
                if wait_number == 1:
                    settings["profit_lock_activation"] = 2000
                    with open(config_path, "w", encoding="utf-8") as updated:
                        json.dump(settings, updated)

            with patch.object(engine, "_stored_profit_lock_positions", return_value=set()), \
                    patch.object(engine, "_save_profit_lock_positions"):
                engine.monitor_and_exit(
                    client, positions, settings, _NoWaitEvent(update_thresholds), lambda _message: None,
                    on_event=events.append, settings_path=config_path,
                )
        finally:
            os.remove(config_path)

        activation_event = next(event for event in events if event.get("type") == "profit_lock_activated")
        exit_event = next(event for event in events if event.get("type") == "exit")
        self.assertEqual(activation_event["total_pnl"], 2200)
        self.assertEqual(exit_event["reason"], "PROFIT_LOCK")
        self.assertEqual(exit_event["total_pnl"], 1000)


if __name__ == "__main__":
    unittest.main()
