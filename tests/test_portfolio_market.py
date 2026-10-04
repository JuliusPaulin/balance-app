"""Public cache, provider failure, FX and calendar alignment regressions."""
import json
from datetime import datetime, timezone, timedelta
import requests
import pytest
from services import portfolio_market as market


@pytest.fixture(autouse=True)
def isolated_public_cache(user_conn, monkeypatch):
    conn,_=user_conn
    conn.execute('DELETE FROM portfolio_market_cache')
    monkeypatch.setattr(market,'_seed_cache',lambda:{})
    yield
    conn.execute('DELETE FROM portfolio_market_cache')


def test_usd_proxies_converted_with_matching_fx_and_gaps_not_bridged():
    data=market._dataset({'Finland':{'2025-01':100,'2025-02':110,'2025-04':121},'EURUSD':{'2025-01':1,'2025-02':1.1,'2025-04':1.1}},None,[],'ok')
    assert data['returns']['Finland']['2025-02']==pytest.approx(0)
    assert '2025-04' not in data['returns']['Finland']
    assert market._dataset({'World':{'2025-01':100,'2025-03':121}},None,[],'ok')['returns']['World']=={}


def test_timezone_adjusted_close_and_currency_validation():
    stamp=int(datetime(2025,1,1,0,0,tzinfo=timezone(timedelta(hours=1))).timestamp())
    item={'meta':{'currency':'EUR','exchangeTimezoneName':'Europe/Berlin'},'timestamp':[stamp],
          'indicators':{'adjclose':[{'adjclose':[101]}],'quote':[{'close':[900]}]}}
    payload={'chart':{'result':[item]}}
    assert market._series(payload,'EUNL.DE')=={'2025-01':101}
    with pytest.raises(ValueError): market._series(payload,'EFNL')
    del item['indicators']['adjclose']
    with pytest.raises(ValueError): market._series(payload,'EUNL.DE')


def test_success_cache_ttl_suppresses_duplicate_calls(user_conn,monkeypatch):
    conn,_=user_conn;calls=[]
    def fetch(symbol):
        calls.append(symbol)
        return {'2025-01':100,'2025-02':101},{'currency':'USD' if symbol in ('EFNL','ENOR','EWD','AAPL','EURUSD=X') else 'EUR'}
    monkeypatch.setattr(market,'_fetch',fetch)
    market.refresh_market(conn);market.refresh_market(conn)
    assert calls==[p[1] for p in market.PROXIES]


def test_failure_retains_prices_timestamp_and_backoff(user_conn,monkeypatch):
    conn,_=user_conn
    old=(datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
    market._store(conn,'EUNL.DE',{'prices':{'2025-01':100,'2025-02':102},'source_date':old},old,'ok')
    calls=[]
    def fail(symbol):
        calls.append(symbol);raise requests.ConnectionError('offline')
    monkeypatch.setattr(market,'_fetch',fail)
    data=market.refresh_market(conn);again=market.refresh_market(conn)
    assert calls==['EUNL.DE']
    assert data['returns']['World']['2025-02']==pytest.approx(.02)
    assert data['refreshed_at']==old
    assert data['warnings'] and again['warnings']


def test_rate_limit_stops_provider_and_is_not_retried(user_conn,monkeypatch):
    conn,_=user_conn;calls=[]
    def fail(symbol):
        calls.append(symbol);response=requests.Response();response.status_code=429
        raise requests.HTTPError('rate limited',response=response)
    monkeypatch.setattr(market,'_fetch',fail)
    market.refresh_market(conn);market.refresh_market(conn)
    assert calls==['EUNL.DE']
    row=market._read_cache(conn,'EUNL.DE')
    assert row['status']=='error'
    assert json.loads(row['payload'])['error']=='rate limited'
