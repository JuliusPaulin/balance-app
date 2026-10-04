"""Market proxy data for portfolio analysis.

The service deliberately has no personal portfolio inputs.  It stores the
small, public Yahoo monthly series needed by the analysis in the local DB.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
from typing import Any

import requests
import threading
from functools import lru_cache
from pathlib import Path
import sys
from data import db


PROXIES = (
    ("World", "EUNL.DE", "EUR"), ("US", "SXR8.DE", "EUR"),
    ("Europe", "EUNK.DE", "EUR"), ("EM", "IS3N.DE", "EUR"),
    ("Finland", "EFNL", "USD"), ("Norway", "ENOR", "USD"),
    ("Sweden", "EWD", "USD"), ("Apple", "AAPL", "USD"),
    ("Bonds", "AGGH.AS", "EUR"), ("EURUSD", "EURUSD=X", "USD"),
)
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TTL_SECONDS = 86400
_REFRESH_LOCK = threading.Lock()


def _valid_price(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _month(value: Any, tz=timezone.utc) -> str:
    return datetime.fromtimestamp(int(value), tz=tz).strftime("%Y-%m")


def _series(payload: dict, symbol=None) -> dict[str, float]:
    result = payload.get("chart", {}).get("result") or []
    if not result:
        raise ValueError("Yahoo returned no chart data")
    item = result[0]
    meta = item.get("meta", {})
    expected = next((c for _, s, c in PROXIES if s == symbol), None)
    if symbol and meta.get("currency") != expected:
        raise ValueError(f"Yahoo returned unexpected currency for {symbol}")
    if symbol == "EURUSD=X" and meta.get("quoteCurrency") not in {None, "USD"}:
        raise ValueError("Yahoo returned unexpected EURUSD quote currency")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(item.get("meta", {}).get("exchangeTimezoneName", "UTC"))
    except Exception:
        tz = timezone.utc
    timestamps = item.get("timestamp") or []
    indicators = item.get("indicators", {})
    adjusted = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    if not adjusted:
        raise ValueError("Yahoo did not return adjusted closes")
    values: dict[str, float] = {}
    for stamp, close in zip(timestamps, adjusted):
        if close is None or not _valid_price(close):
            continue
        key = _month(stamp, tz)
        values[key] = float(close)  # interval=1mo has one observation per month
    current = date.today().strftime("%Y-%m")
    values.pop(current, None)
    if not values:
        raise ValueError("Yahoo returned no complete monthly prices")
    return values


def _fetch(symbol: str):
    response = requests.get(
        YAHOO_URL.format(symbol=symbol),
        params={"range": "5y", "interval": "1mo", "events": "div,splits"},
        headers={"User-Agent": "Balance portfolio/1.0"},
        timeout=(5, 15),
    )
    response.raise_for_status()
    payload = response.json()
    return _series(payload, symbol), payload.get("chart", {}).get("result", [{}])[0].get("meta", {})


@lru_cache(maxsize=1)
def _seed_cache():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    try:
        data = json.loads((root / 'services' / 'portfolio_seed.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError): return {}


def _read_cache(conn, symbol: str):
    try:
        row = conn.execute(
            "SELECT payload, refreshed_at, status FROM portfolio_market_cache "
            "WHERE cache_key = %s", (symbol,)
        ).fetchone()
        return row or _seed_cache().get(symbol)
    except db.DatabaseError:
        return _seed_cache().get(symbol)


def _decode(row):
    if not row:
        return None
    try:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            return None
        if "prices" in payload:
            payload = payload["prices"]
        clean = {}
        for month, value in payload.items():
            if isinstance(month, str) and len(month) == 7 and month[4] == "-" and _valid_price(value):
                clean[month] = float(value)
        return clean or None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _dataset(series: dict[str, dict[str, float]], refreshed_at, warnings, status):
    groups = {key: value for key, value in series.items() if key != "EURUSD"}
    # Convert USD-denominated proxies to EUR using the aligned FX level.
    fx = series.get("EURUSD", {})
    for group, symbol, currency in PROXIES:
        if currency != "USD" or group not in groups:
            continue
        groups[group] = {month: value / fx[month] for month, value in groups[group].items()
                         if month in fx and _valid_price(fx[month])}
    returns = {}
    for group, prices in groups.items():
        ordered = sorted(prices.items())
        values = {}
        for i, (month, value) in enumerate(ordered):
            if i and ((int(month[5:]) == int(ordered[i - 1][0][5:]) % 12 + 1) and
                      int(month[:4]) == int(ordered[i - 1][0][:4]) + (1 if month[5:] == "01" else 0)):
                values[month] = value / ordered[i - 1][1] - 1.0
        returns[group] = values
    proxies = [{"group": g, "symbol": s, "currency": c,
                "source": "Yahoo Finance adjusted close", "refreshed_at": refreshed_at,
                "status": status} for g, s, c in PROXIES if g != "EURUSD"]
    return {"returns": returns, "proxies": proxies, "refreshed_at": refreshed_at,
            "warnings": warnings, "status": status}


def load_market(conn):
    """Load cached market data without network access; missing cache is valid."""
    series, warnings, refreshed = {}, [], []
    for group, symbol, _currency in PROXIES:
        row = _read_cache(conn, symbol)
        values = _decode(row)
        if values:
            series[group] = values
            envelope = json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row["payload"]
            source_date = envelope.get("source_date") if isinstance(envelope, dict) else None
            if source_date: refreshed.append(source_date)
            if row.get("status") == "error": warnings.append(f"{symbol}: last refresh failed")
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["refreshed_at"].replace("Z", "+00:00"))).total_seconds()
                if age >= TTL_SECONDS:
                    warnings.append(f"{symbol}: cached market data is stale")
            except (TypeError, ValueError, KeyError):
                warnings.append(f"{symbol}: cache timestamp is invalid")
        else:
            warnings.append(f"{symbol}: no cached market data")
    provider = _read_cache(conn, '__provider__')
    if provider and provider['status'] == 'error' and _recent(provider, datetime.now(timezone.utc)):
        warnings.append('Public provider unavailable; refresh stopped. Cached prices are retained.')
    status = "ok" if series and not warnings else ("partial" if series else "unavailable")
    return _dataset(series, min(refreshed) if refreshed else None, warnings, status)


def _recent(row, now):
    if not row: return False
    try:
        stamp = datetime.fromisoformat(row["refreshed_at"].replace("Z", "+00:00"))
        return 0 <= (now-stamp).total_seconds() < TTL_SECONDS
    except (TypeError, ValueError, KeyError): return False


def _envelope(row):
    if not row: return {}
    try:
        value = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        return value if isinstance(value, dict) else {}
    except (ValueError,TypeError): return {}


def _store(conn,symbol,envelope,attempt,status):
    conn.execute("INSERT INTO portfolio_market_cache (cache_key,payload,refreshed_at,status) "
                 "VALUES (%s,%s,%s,%s) ON CONFLICT(cache_key) DO UPDATE SET "
                 "payload=excluded.payload,refreshed_at=excluded.refreshed_at,status=excluded.status",
                 (symbol,json.dumps(envelope),attempt,status))


def refresh_market(conn):
    """One bounded public refresh; failures retain prices and suppress retries."""
    if not _REFRESH_LOCK.acquire(blocking=False):
        data=load_market(conn);data['warnings'].append('A market refresh is already running.');return data
    try:
        now=datetime.now(timezone.utc);stamp=now.isoformat()
        provider=_read_cache(conn,'__provider__')
        if _recent(provider,now) and provider['status']=='error':
            data=load_market(conn);data['warnings'].append('Provider failed recently; another refresh is available after 24 hours.');return data
        for _group,symbol,_currency in PROXIES:
            row=_read_cache(conn,symbol)
            if _recent(row,now): continue
            try:
                values,meta=_fetch(symbol)
                _store(conn,symbol,{'prices':values,'source_date':stamp,'last_attempt':stamp,'error':None,
                                   'meta':{'currency':meta.get('currency'),'quoteCurrency':meta.get('quoteCurrency')}},stamp,'ok')
            except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
                old=_envelope(row)
                envelope={'prices':_decode(row) or {},'source_date':old.get('source_date'),
                          'last_attempt':stamp,'error':str(exc),'meta':old.get('meta',{})}
                _store(conn,symbol,envelope,stamp,'error')
                if isinstance(exc,requests.RequestException):
                    _store(conn,'__provider__',{'error':'Public provider unavailable; refresh stopped.'},stamp,'error')
                    break
        return load_market(conn)
    finally:
        _REFRESH_LOCK.release()
