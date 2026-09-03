"""Unit tests for services/investment_import.py — the Nordnet CSV / Nordea xlsx parsers.

These are pure-function tests: no Flask, no database. They matter because the
input is a file format nobody here controls. A broker renaming a column or
switching a decimal separator does not raise anything — it quietly parses to
nothing, or to the wrong number, and lands in the net-worth total as fact. So
the assertions below are about the specific things the real exports do:
UTF-16 with TAB delimiters, Finnish headers, decimal commas, and a snapshot
date that lives in the *filename* rather than the file.

Sample files are built by ``tests/helpers.py`` in the real shape.
"""
import pytest

from services import investment_import as ii
from helpers import NORDEA_HEADER, nordea_xlsx_bytes, nordnet_csv_bytes

STOCKS_FILE = "Osaketaulukko salkkunro 18318444 24.5.2026.csv"


# ── parse_num ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("450,50", 450.50),          # Finnish decimal comma
    ("450.50", 450.50),          # and the English point
    ("1 234,56", 1234.56),       # space thousands separator
    ("1\xa0234,56", 1234.56),    # ...which is really a non-breaking space
    ("1.234,56", 1234.56),       # both separators, comma last -> comma decides
    ("1,234.56", 1234.56),       # both separators, point last -> point decides
    ("-99,9", -99.9),
    (1234, 1234.0),              # already a number (xlsx gives floats)
    (0, 0.0),
    ("", None),
    ("   ", None),
    ("-", None),                 # Nordnet writes a bare dash for "no value"
    (None, None),
    ("n/a", None),
])
def test_parse_num(raw, expected):
    assert ii.parse_num(raw) == expected


def test_parse_num_zero_is_not_none():
    """0 is a value, not a blank — a holding worth nothing still exists."""
    assert ii.parse_num("0,00") == 0.0
    assert ii.parse_num("0,00") is not None


# ── Nordnet CSV ──────────────────────────────────────────────────────
def test_nordnet_stocks_parses_utf16_tab_decimal_comma():
    pf = ii.parse_nordnet_csv(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"],
        ["Sampo", "12,5", "1 234,56", "-3,1", "-39,50", "EUR"],
    ]))
    assert pf.source == "nordnet_stocks"
    # The file carries no date — it comes from the filename, DD.M.YYYY.
    assert pf.as_of == "2026-05-24"
    assert pf.warnings == []

    (acct,) = pf.accounts
    assert (acct.broker, acct.kind) == ("Nordnet", "investment")
    assert acct.label == "Nordnet 18318444"
    assert acct.external_id == "csv:nordnet:18318444"
    assert acct.total_eur == 1685.06

    nokia, sampo = acct.holdings
    assert (nokia.name, nokia.units, nokia.value_eur) == ("Nokia", 100.0, 450.50)
    assert (nokia.return_pct, nokia.return_eur, nokia.currency) == (2.5, 11.0, "EUR")
    assert (sampo.units, sampo.value_eur, sampo.return_pct) == (12.5, 1234.56, -3.1)


def test_nordnet_funds_recognised_by_first_header_cell():
    pf = ii.parse_nordnet_csv(
        "Rahastotaulukko salkkunro 18318444 24.5.2026.csv",
        nordnet_csv_bytes(
            [["Nordea Suomi Passiivinen", "512,3456", "8 010,00", "7", "520,00", "EUR"]],
            header=["Rahastot", "Määrä", "Arvo EUR", "Tuotto, %", "Tuotto, EUR", "Valuutta"],
        ),
    )
    assert pf.source == "nordnet_funds"
    assert pf.accounts[0].holdings[0].units == 512.3456


def test_nordnet_without_portfolio_number_in_filename():
    """No "salkku NNN" to key on: one generic Nordnet account, not a crash."""
    pf = ii.parse_nordnet_csv("export.csv", nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"],
    ]))
    assert pf.as_of is None
    assert pf.accounts[0].label == "Nordnet"
    assert pf.accounts[0].external_id == "csv:nordnet"


def test_nordnet_falls_back_to_utf8_and_detects_the_delimiter():
    pf = ii.parse_nordnet_csv("export.csv", nordnet_csv_bytes(
        [["Nokia", "100", "450,50", "2,5", "11,00", "EUR"]],
        encoding="utf-8", delimiter=";",
    ))
    assert pf.accounts[0].holdings[0].value_eur == 450.50


def test_nordnet_skips_rows_with_no_name_or_no_value():
    """Export footers and spacer rows are rows too — they must not become
    holdings worth 0, which would be indistinguishable from a real one."""
    pf = ii.parse_nordnet_csv(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"],
        ["", "", "", "", "", ""],
        ["Yhteensä", "", "", "", "", ""],
        ["Kesken", "1", "-", "", "", "EUR"],
    ]))
    assert [h.name for h in pf.accounts[0].holdings] == ["Nokia"]


def test_nordnet_short_rows_do_not_raise():
    pf = ii.parse_nordnet_csv(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50"],
    ]))
    h = pf.accounts[0].holdings[0]
    assert (h.value_eur, h.return_pct, h.currency) == (450.50, None, None)


def test_nordnet_header_only_file_warns_rather_than_failing():
    pf = ii.parse_nordnet_csv(STOCKS_FILE, nordnet_csv_bytes([]))
    assert pf.accounts[0].holdings == []
    assert pf.warnings and "no holdings parsed" in pf.warnings[0]


def test_nordnet_without_value_column_is_refused():
    """No "Arvo EUR" means the layout is not what we think it is. Parsing on
    would import a portfolio of zeroes."""
    with pytest.raises(ii.ImportError_, match="Arvo EUR"):
        ii.parse_nordnet_csv(STOCKS_FILE, nordnet_csv_bytes(
            [["Nokia", "100", "450,50"]],
            header=["Osakkeet", "Määrä", "Markkina-arvo"],
        ))


def test_nordnet_empty_file_is_refused():
    with pytest.raises(ii.ImportError_, match="empty CSV"):
        ii.parse_nordnet_csv(STOCKS_FILE, "".encode("utf-16"))


# ── Nordea Omistukset.xlsx ───────────────────────────────────────────
CUSTODY_ROWS = [
    ["Custody", "Nordea salkku 123", "FI0009000681", "EUR", "Nokia",
     100, 1000.0, None, 200.0, 800.0],
    ["Custody", "Nordea salkku 123", "US0378331005", "USD", "Apple",
     5, 2000.0, None, 500.0, None],
]
CASH_ROW = ["CashAccount", "FI21 1234 5600 0007 85", "", "EUR", "",
            None, None, 1500.0, None, None]


def test_nordea_xlsx_groups_custody_by_account_and_dates_from_row_zero():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes(CUSTODY_ROWS))
    assert pf.source == "nordea_xlsx"
    assert pf.as_of == "2026-05-24"      # from the export timestamp in row 0

    (acct,) = pf.accounts
    assert (acct.broker, acct.kind, acct.label) == (
        "Nordea", "investment", "Nordea salkku 123")
    assert acct.external_id == "csv:nordea:Nordeasalkku123"
    assert acct.total_eur == 3000.0
    assert [h.name for h in acct.holdings] == ["Nokia", "Apple"]
    assert (acct.holdings[0].isin, acct.holdings[0].currency) == ("FI0009000681", "EUR")
    assert acct.holdings[0].units == 100


def test_nordea_return_pct_from_purchase_value_then_from_change():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes(CUSTODY_ROWS))
    nokia, apple = pf.accounts[0].holdings
    assert nokia.return_pct == 25.0      # (1000 - 800) / 800
    # No purchase value: derived from the change instead, against the cost basis
    # (2000 - 500), not against today's value.
    assert apple.return_pct == 33.33
    assert (nokia.return_eur, apple.return_eur) == (200.0, 500.0)


def test_nordea_cash_account_keyed_by_iban():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes([CASH_ROW]))
    (cash,) = pf.accounts
    assert cash.kind == "cash"
    assert cash.cash_value == 1500.0 and cash.total_eur == 1500.0
    assert cash.holdings == []
    # Spaces stripped, so the same account matches however the bank spaces it.
    assert cash.external_id == "csv:nordea:FI2112345600000785"


def test_nordea_investments_come_before_cash():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx",
                              nordea_xlsx_bytes([CASH_ROW, *CUSTODY_ROWS]))
    assert [a.kind for a in pf.accounts] == ["investment", "cash"]


def test_nordea_skips_unnamed_or_valueless_custody_rows():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes([
        *CUSTODY_ROWS,
        ["Custody", "Nordea salkku 123", "", "EUR", "", None, 50.0, None, None, None],
        ["Custody", "Nordea salkku 123", "", "EUR", "Ghost", None, None, None, None, None],
        [None, None, None, None, None, None, None, None, None, None],
    ]))
    assert [h.name for h in pf.accounts[0].holdings] == ["Nokia", "Apple"]


def test_nordea_without_timestamp_has_no_date():
    """The snapshot date is then the user's to supply in the review step."""
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx",
                              nordea_xlsx_bytes(CUSTODY_ROWS, timestamp="Omistukset"))
    assert pf.as_of is None


def test_nordea_empty_sheet_warns():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes(
        [["Something", None, None, None, None, None, None, None, None, None]]))
    assert pf.accounts == []
    assert pf.warnings and "no holdings or cash accounts parsed" in pf.warnings[0]


def test_nordea_unexpected_layout_is_refused():
    with pytest.raises(ii.ImportError_, match="unexpected Holdings layout"):
        ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes(
            CUSTODY_ROWS, header=["Laji", "Tili", *NORDEA_HEADER[2:]]))


def test_nordea_too_few_rows_is_refused():
    with pytest.raises(ii.ImportError_, match="too few rows"):
        ii.parse_nordea_xlsx("Omistukset.xlsx", nordea_xlsx_bytes([]))


def test_nordea_reads_the_first_sheet_when_holdings_is_missing():
    pf = ii.parse_nordea_xlsx("Omistukset.xlsx",
                              nordea_xlsx_bytes(CUSTODY_ROWS, sheet="Sheet1"))
    assert pf.accounts[0].total_eur == 3000.0


# ── dispatch ─────────────────────────────────────────────────────────
def test_detect_and_parse_routes_by_extension():
    csv_pf = ii.detect_and_parse(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"]]))
    xlsx_pf = ii.detect_and_parse("Omistukset.XLSX", nordea_xlsx_bytes(CUSTODY_ROWS))
    assert csv_pf.source == "nordnet_stocks"
    assert xlsx_pf.source == "nordea_xlsx"


def test_detect_and_parse_refuses_other_file_types():
    with pytest.raises(ii.ImportError_, match="unsupported file type"):
        ii.detect_and_parse("portfolio.pdf", b"%PDF-1.4")
