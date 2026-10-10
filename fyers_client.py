"""FYERS API v3 adapter exposing the normalized interface used by engine.py."""
import datetime as dt
import json
import os
import threading
import time
import webbrowser
from urllib.parse import parse_qs, unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from fyers_apiv3 import fyersModel
from app_config import DATA_DIR

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 5000
REDIRECT_URL = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"
LOGIN_TIMEOUT_SECONDS = 240
INDEX_SYMBOLS = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
                 "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
                 "SENSEX": "BSE:SENSEX-INDEX"}
INDEX_EXCHANGES = {name: symbol.split(":", 1)[0] for name, symbol in INDEX_SYMBOLS.items()}
MASTER_URLS = {"NSE": "https://public.fyers.in/sym_details/NSE_FO_sym_master.json",
               "BSE": "https://public.fyers.in/sym_details/BSE_FO_sym_master.json"}
# This dashboard only builds option universes for these index families. Keep
# the cached derivative master limited to them instead of retaining every
# stock and contract in NSE F&O for the lifetime of the process.
MASTER_UNDERLYING_ALIASES = {
    "NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY",
    "NIFTYFINSERVICE", "NIFTYFINANCIALSERVICES", "MIDCPNIFTY", "NIFTYMIDSELECT",
}
MASTER_SYMBOL_PREFIXES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")


def _today_str():
    return dt.date.today().isoformat()


def load_cached_access_token(client_id):
    """Return today's token only when it belongs to the configured FYERS app."""
    client_id = (client_id or "").strip()
    if not client_id:
        return None
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        matches_app = data.get("client_id") == client_id
        valid_today = data.get("date") == _today_str() and data.get("provider") == "fyers"
        return data.get("access_token") if matches_app and valid_today else None
    except (OSError, ValueError):
        return None


def extract_request_token(raw_input):
    raw = (raw_input or "").strip()
    if not raw:
        raise ValueError("Paste the FYERS redirect URL or auth_code first.")
    query = parse_qs(urlparse(raw).query)
    code = query.get("auth_code", [None])[0]
    if code:
        return unquote(code)
    if raw.startswith("http") or "?" in raw or " " in raw:
        raise ValueError("Could not find auth_code in the pasted redirect URL.")
    return raw


class _RedirectCatcher(BaseHTTPRequestHandler):
    captured_code = None
    captured_error = None
    bounce = None

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        type(self).captured_code = q.get("auth_code", [None])[0]
        type(self).captured_error = q.get("error", [None])[0]
        body = ("FYERS authorization received. Return to the dashboard." if self.captured_code
                else f"FYERS authorization failed: {self.captured_error or 'auth_code missing'}").encode()
        self.send_response(200 if self.captured_code else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class FyersClient:
    BUY, SELL = 1, -1

    def __init__(self, settings, log=print):
        self.settings, self.log = settings, log
        self.client_id = settings.get("fyers_client_id", "").strip()
        self.secret = settings.get("fyers_secret_key", "").strip()
        self.redirect_uri = settings.get("fyers_redirect_uri", REDIRECT_URL).strip()
        self.fyers = None
        self.access_token = None
        self.ticker = None
        self._lock = threading.Lock()
        self._tick_store = {}
        self._symbol_by_token = {}
        self._token_by_symbol = {}
        self._option_snapshot = {}
        self._ticker_connected = threading.Event()
        self._last_tick_time = None
        self._master = {}

    def _session(self, state="algo_ai"):
        return fyersModel.SessionModel(client_id=self.client_id, secret_key=self.secret,
            redirect_uri=self.redirect_uri, response_type="code", grant_type="authorization_code",
            state=state)

    def generate_auth_url(self, state):
        """Build the FYERS OAuth URL for a browser-based hosted callback."""
        return self._session(state=state).generate_authcode()

    def login(self, redirect_display_url=None):
        cached = load_cached_access_token(self.client_id)
        if cached:
            self.set_access_token(cached)
            self.log("[fyers] reused today's cached access token")
            return
        url = self._session().generate_authcode()
        self.log(f"[fyers] opening authorization: {url}")
        webbrowser.open(url)
        _RedirectCatcher.captured_code = _RedirectCatcher.captured_error = None
        try:
            server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _RedirectCatcher)
        except OSError as exc:
            raise RuntimeError(f"Cannot listen for FYERS redirect on {REDIRECT_URL}: {exc}") from exc
        server.timeout = 1
        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        try:
            while not _RedirectCatcher.captured_code and time.time() < deadline:
                server.handle_request()
                if _RedirectCatcher.captured_error:
                    break
        finally:
            server.server_close()
        if not _RedirectCatcher.captured_code:
            raise RuntimeError("FYERS login did not return auth_code; verify the app redirect URL or paste it manually.")
        self.complete_login(_RedirectCatcher.captured_code)

    def complete_login(self, auth_code):
        session = self._session()
        session.set_token(auth_code)
        response = session.generate_token()
        token = response.get("access_token") if isinstance(response, dict) else None
        if not token:
            raise RuntimeError(f"FYERS token exchange failed: {response}")
        self.set_access_token(token)
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "date": _today_str(), "provider": "fyers",
                       "client_id": self.client_id}, f)
        self.log("[fyers] login successful; access token cached for today")

    def set_access_token(self, token):
        self.access_token = token
        self.fyers = fyersModel.FyersModel(client_id=self.client_id, token=token,
            is_async=False, log_path="")

    def _get_master(self, exchange="NSE"):
        exchange = "BSE" if str(exchange).upper() in ("BSE", "BFO") else "NSE"
        if exchange not in self._master:
            response = requests.get(MASTER_URLS[exchange], timeout=30)
            response.raise_for_status()
            try:
                records = response.json()
            except ValueError as exc:
                raise RuntimeError(f"FYERS returned an invalid {exchange}_FO symbol-master response") from exc
            master = []
            aliases = MASTER_UNDERLYING_ALIASES if exchange == "NSE" else {"SENSEX"}
            prefixes = MASTER_SYMBOL_PREFIXES if exchange == "NSE" else ("SENSEX",)
            # The current JSON master is keyed by API symbol and supplies
            # explicit fyToken, minLotSize, expiryDate, strikePrice and optType.
            for api_symbol, record in records.items():
                if not isinstance(record, dict):
                    continue
                try:
                    symbol = str(record.get("symTicker") or api_symbol)
                    tradingsymbol = symbol.split(":", 1)[-1]
                    underlying = "".join(ch for ch in str(record.get("underSym") or "").upper()
                                         if ch.isalnum())
                    normalized_symbol = "".join(ch for ch in tradingsymbol.upper() if ch.isalnum())
                    if not symbol.startswith(exchange + ":"):
                        continue
                    if (underlying not in aliases
                            and not normalized_symbol.startswith(prefixes)):
                        continue
                    expiry_raw = record.get("expiryDate")
                    if not expiry_raw:
                        expiry = dt.date.max
                    else:
                        try:
                            expiry = dt.datetime.fromtimestamp(int(expiry_raw), dt.timezone.utc).date()
                        except (ValueError, TypeError, OverflowError):
                            expiry = dt.date.fromisoformat(str(expiry_raw)[:10])
                    opt_type = str(record.get("optType") or "XX")
                    exchange_type = int(record.get("exInstType") or 0)
                    kind = opt_type if opt_type in ("CE", "PE") else (
                        "FUT" if exchange_type in (11, 12, 13, 16, 17, 18, 25, 30) else "XX")
                    master.append({"instrument_token": str(record.get("fyToken") or ""),
                        "tradingsymbol": tradingsymbol, "symbol": symbol,
                        "lot_size": int(float(record.get("minLotSize") or 1)), "expiry": expiry,
                        "strike": float(record.get("strikePrice") or 0), "instrument_type": kind,
                        "name": str(record.get("underSym") or record.get("exSymName") or ""),
                        "api_exchange": exchange, "legacy_exchange": "BFO" if exchange == "BSE" else "NFO",
                        "segment": str(record.get("segment") or "")})
                except (ValueError, TypeError, OverflowError):
                    continue
            self._master[exchange] = master
        return self._master[exchange]

    def get_index_instrument(self, tradingsymbol, exchange="NSE"):
        index = next((k for k, v in INDEX_SYMBOLS.items() if v.split(":", 1)[-1] == tradingsymbol), None)
        if index is None:
            index = next((k for k, v in INDEX_SYMBOLS.items() if v.split(":", 1)[-1].replace("-INDEX", "") == tradingsymbol), None)
        if index is None:
            # Map the legacy engine labels.
            index = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
                     "NIFTY MID SELECT": "MIDCPNIFTY", "SENSEX": "SENSEX", "SENSEX-INDEX": "SENSEX"}.get(tradingsymbol)
        symbol = INDEX_SYMBOLS.get(index, f"{exchange}:{tradingsymbol}")
        result = self.fyers.quotes({"symbols": symbol})
        values = result.get("d", [])
        if not values:
            raise ValueError(f"FYERS did not return quote for {symbol}: {result}")
        token = str(values[0]["v"].get("fyToken", symbol))
        self._symbol_by_token[token] = symbol
        self._token_by_symbol[symbol] = token
        return {"instrument_token": token, "tradingsymbol": symbol.split(":", 1)[-1], "symbol": symbol,
                "lot_size": 1, "strike": 0, "instrument_type": "INDEX", "name": index,
                "api_exchange": symbol.split(":", 1)[0],
                "legacy_exchange": "BSE" if symbol.startswith("BSE:") else "NSE"}

    def get_nfo_option_chain(self, name, today=None):
        name = name if name in INDEX_SYMBOLS else "NIFTY"
        resp = self.fyers.optionchain({"symbol": INDEX_SYMBOLS[name], "strikecount": 50})
        data = resp.get("data", {})
        legs = data.get("optionsChain", [])
        expiry_data = data.get("expiryData", [])
        if resp.get("s") != "ok" or not expiry_data:
            raise RuntimeError(f"FYERS option chain request failed: {resp}")
        expiry = dt.datetime.strptime(expiry_data[0]["date"], "%d-%m-%Y").date()
        exchange = INDEX_EXCHANGES[name]
        master = self._get_master(exchange)
        by_symbol = {i["symbol"].strip().upper(): i for i in master}
        by_token = {str(i["instrument_token"]): i for i in master}
        chain = []
        for leg in legs:
            typ = leg.get("option_type")
            symbol = leg.get("symbol")
            if typ not in ("CE", "PE") or not symbol:
                continue
            # FYERS's option-chain and symbol-master feeds can use different
            # symbol spellings; fyToken is the stable contract identifier.
            token = str(leg.get("fyToken") or "")
            item = by_token.get(token) or by_symbol.get(symbol.strip().upper())
            if not item:
                continue
            item = dict(item)
            item.update({"instrument_token": token or str(item["instrument_token"]),
                         "strike": float(leg["strike_price"]), "instrument_type": typ,
                         "expiry": expiry, "name": name,
                         "api_exchange": exchange,
                         "legacy_exchange": "BFO" if exchange == "BSE" else "NFO"})
            # Keep the FYERS API symbol from the chain: it is the symbol used
            # for both websocket subscription and order placement.
            item["symbol"] = symbol
            item["tradingsymbol"] = symbol.split(":", 1)[-1]
            self._symbol_by_token[item["instrument_token"]] = symbol
            self._token_by_symbol[symbol] = item["instrument_token"]
            self._option_snapshot[symbol] = {
                "oi": leg.get("oi"), "volume": leg.get("volume"), "last_price": leg.get("ltp"),
            }
            chain.append(item)
        if not chain:
            examples = ", ".join(str(x.get("symbol")) for x in legs[:3] if isinstance(x, dict))
            raise ValueError(f"FYERS option chain returned {len(legs)} entries, but none matched its contract master "
                             f"by fyToken or symbol for {name}; sample symbols: {examples or 'none'}")
        return expiry, chain

    def get_nfo_futures_instrument(self, name, today=None):
        today = today or dt.date.today()
        aliases = {
            "NIFTY": {"NIFTY", "NIFTY50"},
            "BANKNIFTY": {"BANKNIFTY", "NIFTYBANK"},
            "FINNIFTY": {"FINNIFTY", "NIFTYFINSERVICE", "NIFTYFINANCIALSERVICES"},
            "MIDCPNIFTY": {"MIDCPNIFTY", "NIFTYMIDSELECT"},
            "SENSEX": {"SENSEX"},
        }
        normalize = lambda value: "".join(ch for ch in str(value).upper() if ch.isalnum())
        accepted_names = aliases.get(name, {normalize(name)})
        exchange = INDEX_EXCHANGES.get(name, "NSE")
        candidates = [i for i in self._get_master(exchange)
                      if i["instrument_type"] == "FUT" and i["expiry"] >= today
                      and (normalize(i["name"]) in accepted_names
                           or normalize(i["tradingsymbol"]).startswith(normalize(name)))]
        if not candidates:
            raise ValueError(f"No FYERS futures found for {name}")
        item = min(candidates, key=lambda x: x["expiry"])
        self._symbol_by_token[item["instrument_token"]] = item["symbol"]
        self._token_by_symbol[item["symbol"]] = item["instrument_token"]
        return item

    def get_quote(self, exchange_tradingsymbols, include_oi_source=False):
        symbols = [self._format_symbol(s) for s in exchange_tradingsymbols]
        resp = self.fyers.quotes({"symbols": ",".join(symbols)})
        if resp.get("s") != "ok":
            raise RuntimeError(f"FYERS quotes failed: {resp}")
        out = {}
        for row in resp.get("d", []):
            v = row.get("v", {})
            sym = row.get("n") or v.get("symbol")
            if not sym:
                continue
            key = self._legacy_key(sym)
            chain_snapshot = self._option_snapshot.get(sym, {})
            quote_oi = v.get("oi", 0)
            oi_source = "quote" if quote_oi not in (None, 0) else (
                "option_chain_snapshot" if chain_snapshot.get("oi") is not None else "missing")
            out[key] = {"last_price": v.get("lp", 0), "oi": quote_oi, "volume": v.get("volume", 0),
                        "average_price": v.get("atp", 0), "source_timestamp": v.get("tt"),
                        "received_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "ohlc": {"open": v.get("open_price"), "close": v.get("prev_close_price")}}
            # FYERS quotes omit OI; the option-chain snapshot carries OI and volume.
            if out[key].get("oi") in (None, 0) and chain_snapshot.get("oi") is not None:
                out[key]["oi"] = chain_snapshot["oi"]
            if include_oi_source:
                out[key]["oi_source"] = oi_source
                out[key]["quote_oi"] = quote_oi
            if out[key].get("volume") in (None, 0) and chain_snapshot.get("volume") is not None:
                out[key]["volume"] = chain_snapshot["volume"]
        return out

    def _format_symbol(self, symbol):
        if symbol in self._symbol_by_token.values():
            return symbol
        exch, separator, ts = symbol.partition(":")
        if not separator:
            ts = exch
            exch = ""
        index_match = next((api_symbol for api_symbol in INDEX_SYMBOLS.values()
                            if api_symbol.split(":", 1)[-1] == ts), None)
        if index_match:
            return index_match
        if exch.upper() in ("NFO", "BFO"):
            api_exchange = "NSE" if exch.upper() == "NFO" else "BSE"
            matches = [i for i in self._get_master(api_exchange) if i["tradingsymbol"] == ts]
            if matches:
                return matches[0]["symbol"]
            other_exchange = "BSE" if api_exchange == "NSE" else "NSE"
            matches = [i for i in self._get_master(other_exchange) if i["tradingsymbol"] == ts]
            if matches:
                return matches[0]["symbol"]
            return f"{api_exchange}:{ts}"
        if exch.upper() in ("NSE", "BSE") and (ts.endswith("-INDEX") or ts.endswith("-EQ")
                                                  or ts.endswith("-FUT") or ts[-2:] in ("CE", "PE")):
            # Engine uses Kite-style NFO keys; FYERS exposes NSE F&O symbols
            # as NSE:... and BSE F&O symbols as BSE:... .
            return f"{exch.upper()}:{ts}"
        # Resolve exchange-less symbols against the derivatives masters.
        match = next((i["symbol"] for exchange in ("NSE", "BSE")
                      for i in self._get_master(exchange) if i["tradingsymbol"] == ts), None)
        if match:
            return match
        idx = {"NIFTY 50": "NSE:NIFTY50-INDEX", "NIFTY BANK": "NSE:NIFTYBANK-INDEX",
               "NIFTY FIN SERVICE": "NSE:FINNIFTY-INDEX", "NIFTY MID SELECT": "NSE:MIDCPNIFTY-INDEX",
               "INDIA VIX": "NSE:INDIAVIX-INDEX", "SENSEX": "BSE:SENSEX-INDEX",
               "SENSEX-INDEX": "BSE:SENSEX-INDEX"}.get(ts)
        return idx or f"{exch}:{ts}"

    def _legacy_key(self, symbol):
        prefix, _, ts = symbol.partition(":")
        is_derivative = ts.endswith(("CE", "PE", "FUT"))
        if prefix == "BSE":
            return f"{'BFO' if is_derivative else 'BSE'}:{ts}"
        return f"{'NFO' if is_derivative else 'NSE'}:{ts}"

    def _history(self, token, start, end, resolution, oi=False):
        symbol = self._symbol_by_token.get(str(token), str(token))
        if ":" not in symbol:
            symbol = self._format_symbol(f"NFO:{symbol}")
        else:
            symbol = self._format_symbol(symbol)
        data = {"symbol": symbol, "resolution": resolution, "date_format": "1",
                "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"), "cont_flag": "1"}
        if oi:
            data["oi_flag"] = "1"
        resp = self.fyers.history(data=data)
        if resp.get("s") == "no_data":
            return []
        if resp.get("s") != "ok":
            raise RuntimeError(f"FYERS history failed: {resp}")
        return [{"date": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5],
                 **({"oi": c[6]} if len(c) > 6 else {})} for c in resp.get("candles", [])]

    def get_daily_history(self, token, start, end, oi=False):
        return self._history(token, start, end, "D", oi)

    def get_intraday_history(self, token, start, end, interval="5minute"):
        resolution = "1" if interval in ("minute", "1minute") else "5" if "5" in interval else "15"
        return self._history(token, start, end, resolution)

    def get_intraday_history_for_symbol(self, symbol, start, end, interval="1minute"):
        resolution = "1" if interval in ("minute", "1minute") else "5" if "5" in interval else "15"
        api_symbol = self._format_symbol(symbol) if ":" in str(symbol) else self._format_symbol(f"NFO:{symbol}")
        response = self.fyers.history(data={
            "symbol": api_symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        })
        if response.get("s") == "no_data":
            return []
        if response.get("s") != "ok":
            raise RuntimeError(f"FYERS history failed for {api_symbol}: {response}")
        return [{"date": candle[0], "open": candle[1], "high": candle[2], "low": candle[3],
                 "close": candle[4], "volume": candle[5]} for candle in response.get("candles", [])]

    def start_ticker(self, instrument_tokens, token_meta=None, on_tick=None, on_raw_message=None):
        # Load lazily because FYERS's socket module imports pkg_resources.
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError as exc:
            raise RuntimeError(
                "FYERS websocket dependencies are missing. Run `python -m pip install -r requirements.txt`."
            ) from exc
        symbols = []
        for token in instrument_tokens:
            symbol = self._symbol_by_token.get(str(token))
            if not symbol:
                # Tokens for index/futures/options are registered at lookup time.
                continue
            symbols.append(symbol)
        if not symbols:
            raise RuntimeError("No FYERS symbols available for live subscription")
        def on_message(message):
            rows = message if isinstance(message, list) else [message]
            normalized = []
            raw_rows = []
            with self._lock:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    symbol = row.get("symbol") or row.get("n")
                    token = str(row.get("fyToken") or self._token_by_symbol.get(symbol, symbol))
                    oi_fields = [key for key in row if str(key).lower() in {"oi", "open_interest", "openinterest"}]
                    tick = {"instrument_token": token, "last_price": row.get("ltp", row.get("lp", 0)),
                            "oi": row.get("oi"), "oi_field_present": bool(oi_fields),
                            "oi_field_names": oi_fields, "oi_raw_value": row.get("oi"),
                            "volume_traded": row.get("vol_traded_today", row.get("volume", 0)),
                            "average_price": row.get("avg_trade_price", row.get("atp", 0)),
                            "ohlc": {"open": row.get("open_price", 0), "close": row.get("prev_close_price", 0)}}
                    self._tick_store[token] = tick
                    self._last_tick_time = time.time()
                    normalized.append(tick)
                    raw_rows.append(dict(row))
            for tick in normalized:
                if on_tick:
                    try:
                        on_tick(tick)
                    except Exception as exc:
                        self.log(f"[fyers] tick callback error: {exc!r}")
            if on_raw_message:
                for row in raw_rows:
                    try:
                        on_raw_message(row)
                    except Exception as exc:
                        self.log(f"[fyers] raw tick callback error: {exc!r}")
        def on_open():
            self.ticker.subscribe(symbols=symbols, data_type="SymbolUpdate")
            self._ticker_connected.set()
            self.log(f"[fyers] market socket connected; subscribed {len(symbols)} symbols")
        self.ticker = data_ws.FyersDataSocket(access_token=f"{self.client_id}:{self.access_token}", log_path="",
            litemode=False, write_to_file=False, reconnect=True, on_connect=on_open,
            on_close=lambda m: self.log(f"[fyers] ticker closed: {m}"),
            on_error=lambda m: self.log(f"[fyers] ticker error: {m}"), on_message=on_message)
        threading.Thread(target=self.ticker.connect, daemon=True).start()

    def wait_for_ticker(self, timeout=10):
        return self._ticker_connected.wait(timeout)

    def seconds_since_last_tick(self):
        return None if self._last_tick_time is None else time.time() - self._last_tick_time

    def stop_ticker(self):
        if self.ticker:
            try:
                self.ticker.close_connection()
            except Exception:
                pass

    def get_tick(self, token):
        with self._lock:
            return self._tick_store.get(str(token))

    def get_all_ticks(self):
        with self._lock:
            return dict(self._tick_store)

    def place_market_order(self, tradingsymbol, exchange, transaction_type, quantity):
        symbol = self._format_symbol(f"{exchange}:{tradingsymbol}")
        side = 1 if transaction_type in (1, "BUY", "buy") else -1
        if self.settings.get("dry_run", True):
            self.log(f"[DRY_RUN] would place FYERS MARKET order: {symbol} qty={quantity} side={side}")
            return {"dry_run": True, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type, "quantity": quantity}
        response = self.fyers.place_order({"symbol": symbol, "qty": int(quantity), "type": 2, "side": side,
            "productType": "INTRADAY", "limitPrice": 0, "stopPrice": 0, "validity": "DAY",
            "disclosedQty": 0, "offlineOrder": False, "stopLoss": 0, "takeProfit": 0})
        if response.get("s") != "ok":
            raise RuntimeError(f"FYERS order rejected: {response}")
        self.log(f"[fyers] LIVE order placed for {symbol}: {response}")
        return {"dry_run": False, "order_id": response.get("id"), "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type, "quantity": quantity}

    def positions(self):
        response = self.fyers.positions()
        if response.get("s") != "ok":
            raise RuntimeError(f"FYERS positions failed: {response}")
        out = []
        for p in response.get("netPositions", []):
            sym = p.get("symbol", "")
            api_exchange = sym.split(":", 1)[0]
            is_option = sym.endswith(("CE", "PE"))
            legacy_exchange = ("BFO" if api_exchange == "BSE" else "NFO") if is_option else api_exchange
            out.append({"tradingsymbol": sym.split(":")[-1], "exchange": legacy_exchange,
                        "product": "MIS" if p.get("productType") == "INTRADAY" else p.get("productType"),
                        "quantity": p.get("netQty", 0), "average_price": p.get("netAvg", p.get("buyAvg", 0)),
                        "last_price": p.get("ltp"), "pnl": p.get("pl", 0), "symbol": sym})
        return {"net": out}

    def is_fo_market_open(self, exchange="NSE"):
        """Fail closed unless FYERS reports the selected exchange's F&O segment OPEN."""
        response = self.fyers.market_status()
        if response.get("code") != 200:
            raise RuntimeError(f"FYERS market-status check failed: {response}")
        rows = response.get("marketStatus", [])
        exchange_code = 12 if str(exchange).upper() in ("BSE", "BFO") else 10
        segment_code = 12 if exchange_code == 12 else 11
        fo_rows = [row for row in rows
                   if int(row.get("exchange", -1)) == exchange_code
                   and int(row.get("segment", -1)) == segment_code]
        exchange_name = "BSE" if exchange_code == 12 else "NSE"
        if not fo_rows:
            raise RuntimeError(f"FYERS market-status response has no {exchange_name} F&O segment: {response}")
        return any(str(row.get("status", "")).upper() == "OPEN" for row in fo_rows)

    def is_nse_fo_market_open(self):
        """Backward-compatible NSE F&O market-status check."""
        return self.is_fo_market_open("NSE")

    def get_actual_position_qty(self, tradingsymbol, exchange="NFO", product="MIS", expected_qty=None):
        if self.settings.get("dry_run", True):
            return expected_qty
        return next((p["quantity"] for p in self.positions()["net"]
                     if p["tradingsymbol"] == tradingsymbol and p["exchange"] == exchange and p["product"] == product), 0)


