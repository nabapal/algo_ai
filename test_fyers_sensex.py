from datetime import date

from fyers_client import FyersClient, INDEX_SYMBOLS


class FakeFyers:
    def __init__(self, optionchain=None, market_status=None):
        self.optionchain_response = optionchain or {}
        self.market_status_response = market_status or {}

    def optionchain(self, payload):
        assert payload["symbol"] == "BSE:SENSEX-INDEX"
        return self.optionchain_response

    def market_status(self):
        return self.market_status_response


def _client():
    client = FyersClient({"fyers_client_id": "test"}, log=lambda *_: None)
    return client


def test_sensex_uses_bse_index_symbol():
    assert INDEX_SYMBOLS["SENSEX"] == "BSE:SENSEX-INDEX"
    client = _client()
    client.fyers = FakeFyers()
    quote_result = {"d": [{"v": {"fyToken": "sensex-index-token"}}]}
    client.fyers.quotes = lambda request: quote_result
    instrument = client.get_index_instrument("SENSEX-INDEX")
    assert instrument["symbol"] == "BSE:SENSEX-INDEX"
    assert instrument["api_exchange"] == "BSE"
    assert client._format_symbol("SENSEX-INDEX") == "BSE:SENSEX-INDEX"


def test_option_chain_matches_bse_master_by_token_and_keeps_lot_metadata():
    client = _client()
    client.fyers = FakeFyers({
        "s": "ok",
        "data": {
            "expiryData": [{"date": "15-10-2026"}],
            "optionsChain": [
                {"symbol": "BSE:SENSEX26O1572500CE", "fyToken": "ce-token", "option_type": "CE",
                 "strike_price": 72500, "oi": 100},
                {"symbol": "BSE:SENSEX26O1572500PE", "fyToken": "pe-token", "option_type": "PE",
                 "strike_price": 72500, "oi": 120},
            ],
        },
    })
    client._get_master = lambda exchange="NSE": [
        {"symbol": "BSE:SENSEX26O1572500CE", "tradingsymbol": "SENSEX26O1572500CE",
         "instrument_token": "ce-token", "lot_size": 20, "expiry": date(2026, 10, 15),
         "strike": 72500, "instrument_type": "CE", "name": "SENSEX"},
        {"symbol": "BSE:SENSEX26O1572500PE", "tradingsymbol": "SENSEX26O1572500PE",
         "instrument_token": "pe-token", "lot_size": 20, "expiry": date(2026, 10, 15),
         "strike": 72500, "instrument_type": "PE", "name": "SENSEX"},
    ]

    expiry, chain = client.get_nfo_option_chain("SENSEX")

    assert expiry == date(2026, 10, 15)
    assert len(chain) == 2
    assert {row["instrument_token"] for row in chain} == {"ce-token", "pe-token"}
    assert {row["legacy_exchange"] for row in chain} == {"BFO"}
    assert {row["lot_size"] for row in chain} == {20}
    assert client._option_snapshot["BSE:SENSEX26O1572500CE"]["oi"] == 100


def test_bfo_aliases_normalize_to_bse_symbols_and_market_segment():
    client = _client()
    master_rows = [{"symbol": "BSE:SENSEX26O1572500CE", "tradingsymbol": "SENSEX26O1572500CE"}]
    client._get_master = lambda exchange="NSE": master_rows if exchange == "BSE" else []
    assert client._format_symbol("BFO:SENSEX26O1572500CE") == "BSE:SENSEX26O1572500CE"
    assert client._legacy_key("BSE:SENSEX26O1572500CE") == "BFO:SENSEX26O1572500CE"

    client.fyers = FakeFyers(market_status={"code": 200, "marketStatus": [
        {"exchange": 10, "segment": 11, "status": "OPEN"},
        {"exchange": 12, "segment": 12, "status": "CLOSED"},
    ]})
    assert client.is_fo_market_open("NSE") is True
    assert client.is_fo_market_open("BSE") is False
