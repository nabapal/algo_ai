"""Read-only check of FYERS option ticker OI using today's cached token.

Run during NSE F&O market hours for meaningful tick coverage:
    python diagnose_fyers_oi.py --wait-seconds 30

This script never places an order and never prints the access token.
"""

import argparse
import datetime
import json
import sys
import time

from app_config import load_settings
from engine import build_option_universe
from fyers_client import FyersClient, load_cached_access_token


def _summarize(tokens, ticks):
    received = 0
    oi_present = 0
    oi_none = 0
    raw_key_only = 0
    no_tick = 0
    rows = []
    for token, symbol in tokens:
        tick = ticks.get(str(token))
        if tick is None:
            no_tick += 1
            rows.append((symbol, "NO TICK", "<none>", "<none>"))
            continue
        received += 1
        raw_present = bool(tick.get("oi_field_present"))
        oi_value = tick.get("oi")
        if oi_value is not None:
            oi_present += 1
            status = "OI PRESENT"
        elif raw_present:
            oi_none += 1
            raw_key_only += 1
            status = "OI KEY PRESENT, VALUE NULL"
        else:
            oi_none += 1
            status = "TICK RECEIVED, OI KEY ABSENT"
        fields = ",".join(tick.get("oi_field_names") or []) or "<none>"
        rows.append((symbol, status, fields, repr(tick.get("oi_raw_value"))))
    return {
        "subscribed": len(tokens), "received": received, "oi_present": oi_present,
        "oi_missing_or_null": oi_none, "no_tick": no_tick,
        "raw_oi_key_with_null_value": raw_key_only, "rows": rows,
    }


def _print_summary(label, result):
    print(f"\n{label}: subscribed={result['subscribed']} tick_received={result['received']} "
          f"oi_value_present={result['oi_present']} oi_missing_or_null={result['oi_missing_or_null']} "
          f"no_tick={result['no_tick']}")
    for symbol, status, fields, value in result["rows"]:
        print(f"  {symbol}: {status}; raw OI key(s)={fields}; raw OI value={value}")


def _redact(value):
    """Redact credential fields before printing decoded market messages."""
    sensitive_names = {"access_token", "authorization", "client_secret", "secret_key", "api_key"}
    if isinstance(value, dict):
        return {key: ("<redacted>" if str(key).lower() in sensitive_names else _redact(child))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-seconds", type=int, default=20,
                        help="total time to collect ticks (default: 20; minimum: 5)")
    parser.add_argument("--index", choices=("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"),
                        help="index to inspect (defaults to the index in settings)")
    args = parser.parse_args()
    wait_seconds = max(5, args.wait_seconds)

    settings = load_settings()
    client_id = (settings.get("fyers_client_id") or "").strip()
    token = load_cached_access_token(client_id)
    if not token:
        print("No valid token.json session for the configured FYERS client today. "
              "Log in to FYERS in the app, then run this script again.")
        return 2

    client = FyersClient(settings, log=lambda message: print(message, flush=True))
    client.set_access_token(token)
    del token  # avoid retaining another reference longer than necessary
    index = args.index or settings.get("index", "NIFTY")
    now_ist = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    print(f"Authenticated with today's cached FYERS token (not displayed). Index={index}; "
          f"local IST={now_ist:%Y-%m-%d %H:%M:%S %z}; collection={wait_seconds}s")

    try:
        try:
            market_open = client.is_nse_fo_market_open()
            print(f"FYERS reports NSE F&O market {'OPEN' if market_open else 'CLOSED'}.")
            if not market_open:
                print("No ticks outside market hours may make this run inconclusive; "
                      "repeat during market hours to verify active ticker fields.")
        except Exception as exc:
            print(f"Could not confirm market status ({type(exc).__name__}: {exc}).")

        universe = build_option_universe(client, index, print)
        contracts = []
        for strike in universe["target_strikes"]:
            for option_type in ("CE", "PE"):
                instrument = universe["instruments_by_strike"][strike][option_type]
                contracts.append((str(instrument["instrument_token"]), instrument["symbol"]))

        raw_messages = {}
        def capture_raw(row):
            symbol = row.get("symbol") or row.get("n")
            if symbol:
                raw_messages[str(symbol)] = dict(row)

        client.start_ticker([token_id for token_id, _ in contracts], on_raw_message=capture_raw)
        if not client.wait_for_ticker(timeout=10):
            print("Ticker did not confirm a connection within 10 seconds.")
            return 3

        # The application decides to use its REST quote fallback after a
        # five-second wait. Capture that exact point, then keep listening to
        # distinguish delayed ticks from ticks that genuinely omit OI.
        time.sleep(5)
        ticks_at_fallback = client.get_all_ticks()
        at_fallback = _summarize(contracts, ticks_at_fallback)
        _print_summary("At the app's five-second fallback point", at_fallback)

        missing_contracts = [
            (token_id, symbol) for token_id, symbol in contracts
            if ticks_at_fallback.get(token_id) is None or ticks_at_fallback[token_id].get("oi") is None
        ]
        fallback_quotes = {}
        if missing_contracts:
            quote_symbols = [f"NFO:{symbol.split(':', 1)[-1]}" for _, symbol in missing_contracts]
            print(f"\nRequesting FYERS quotes for {len(quote_symbols)} contracts, matching the engine fallback...")
            fallback_quotes = client.get_quote(quote_symbols, include_oi_source=True)

        recovered = 0
        print("\nOI source audit for the engine's fallback decision:")
        for token_id, symbol in contracts:
            tick = ticks_at_fallback.get(token_id)
            tick_oi = tick.get("oi") if tick else None
            quote_key = f"NFO:{symbol.split(':', 1)[-1]}"
            quote = fallback_quotes.get(quote_key, {})
            chain_oi = client._option_snapshot.get(symbol, {}).get("oi")
            if tick_oi is not None:
                final_oi, source = tick_oi, "ticker"
            else:
                final_oi = quote.get("oi", 0) or 0
                source = quote.get("oi_source", "no_quote_row") if quote else "no_quote_row"
            if final_oi not in (None, 0):
                recovered += 1
            print(f"  {symbol}: tick_oi={tick_oi!r}; quote_api_oi={quote.get('quote_oi')!r}; "
                  f"option_chain_oi={chain_oi!r}; engine_fallback_oi={final_oi!r}; source={source}")
        print(f"Fallback result: {recovered}/{len(contracts)} contracts have non-zero OI after the engine-style fallback.")

        remaining = wait_seconds - 5
        if remaining > 0:
            time.sleep(remaining)
        final = _summarize(contracts, client.get_all_ticks())
        _print_summary("At end of collection", final)
        print("\nLatest decoded FYERS WebSocket message per contract (credential fields redacted):")
        for _, symbol in contracts:
            raw = raw_messages.get(symbol)
            payload = json.dumps(_redact(raw), ensure_ascii=False, sort_keys=True) if raw else "<no message>"
            print(f"  {symbol}: {payload}")

        if final["received"] == 0:
            print("RESULT: INCONCLUSIVE — FYERS delivered no option ticks during this collection window.")
        elif final["oi_missing_or_null"]:
            print("RESULT: Ticker messages omitted usable OI. See the OI source audit above to determine "
                  "whether the quote/option-chain fallback supplied each value.")
        else:
            print("RESULT: Every received ticker message contained a non-null OI value. "
                  "Any contracts without a tick are not evidence that their tick omitted OI.")
        return 0
    finally:
        client.stop_ticker()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; no orders were placed.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Diagnostic failed: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
