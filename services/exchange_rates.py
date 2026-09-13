"""Dated ECB reference rates for imports. Amounts never leave this process."""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

import requests


RATE_URL = "https://api.frankfurter.dev/v2/providers/ecb/rate"


class ExchangeRateError(ValueError):
    """The import cannot safely convert an amount."""


def currency_code(value):
    code = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError("Use a three-letter currency code, such as EUR or SEK")
    return code


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid amount") from None


def to_eur(conn, uid, currency, day, memo):
    """Return (rate, rate_date), reusing past lookups saved in this database.

    Today's lookup can change before publication, so only cache a request once
    its own rate exists or the requested date has ended. Refuse rates more
    than seven days old instead of converting with a stale or suspended pair.
    """
    currency = currency_code(currency)
    requested = date.fromisoformat(day)
    if requested > date.today():
        raise ExchangeRateError("Cannot fetch an exchange rate for a future date")
    if currency == "EUR":
        return Decimal("1"), day
    key = (currency, day)
    if key in memo:
        return memo[key]
    cached = conn.execute(
        "SELECT rate, rate_date FROM import_exchange_rates "
        "WHERE user_id = %s AND currency = %s AND requested_date = %s",
        (uid, currency, day),
    ).fetchone()
    if cached:
        memo[key] = (money(cached["rate"]), cached["rate_date"])
        return memo[key]
    try:
        response = requests.get(
            f"{RATE_URL}/{currency.lower()}/eur",
            params={"date": day}, timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
        rate = money(payload["rate"])
        rate_day = date.fromisoformat(payload["date"])
        if (payload["base"] != currency or payload["quote"] != "EUR"
                or rate <= 0 or not 0 <= (requested - rate_day).days <= 7):
            raise ValueError("Unexpected exchange rate")
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise ExchangeRateError(
            f"Could not get an ECB {currency} → EUR rate for {day}. "
            "Check your connection and currency, then try again. "
            "No transactions were imported."
        ) from exc
    rate_date = rate_day.isoformat()
    if requested < date.today() or rate_day == requested:
        conn.execute(
            "INSERT INTO import_exchange_rates "
            "(user_id, currency, requested_date, rate_date, rate) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, currency, requested_date) DO NOTHING",
            (uid, currency, day, rate_date, str(rate)),
        )
    memo[key] = (rate, rate_date)
    return memo[key]


def convert(conn, uid, amount, currency, day, memo, fee=0):
    """Convert an absolute statement amount; retain the source for review."""
    currency = currency_code(currency)
    rate, rate_date = to_eur(conn, uid, currency, day, memo)
    converted = (money(amount) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    details = {
        "amount": str(money(amount)), "currency": currency, "date": day,
        "fee": str(money(fee)), "rate": str(rate), "rate_date": rate_date,
        "amount_eur": str(converted), "provider": "ECB via Frankfurter",
    }
    return float(converted), details
