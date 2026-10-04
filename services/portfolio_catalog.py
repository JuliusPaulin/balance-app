"""Public instrument metadata used by portfolio analysis.

This module deliberately contains no account, holding, or valuation data.  An
empty ``countries`` mapping means that a complete, dated country breakdown was
not verified for the exact share class; callers must not infer one from the
instrument's label.
"""

CATALOG_RETRIEVED_AT = "2026-10-04"


def _entry(*, mandate, annual_cost, countries, source_url,
           composition_date, estimated, note, cost_source, cost_date):
    return {
        "mandate": mandate,
        "asset_class": "Equities",
        "annual_cost": annual_cost,
        "countries": countries,
        "source_url": source_url,
        "composition_date": composition_date,
        "retrieved_at": CATALOG_RETRIEVED_AT,
        "estimated": estimated,
        "note": note,
        "cost_source": cost_source,
        "cost_date": cost_date,
    }


NORDEA = "https://www.nordeafunds.com/fi/rahastot/nordea-maailma-enhanced-a"
NORDNET_LIST = (
    "https://www.nordnet.fi/rahastot/lista?fundBrandingCompany=Nordnet&"
    "fundType=INDEX&sortField=fund_yearly_fee&sortOrder=ascending"
)
ISHARES_WORLD = (
    "https://www.ishares.com/uk/individual/en/products/251882/"
    "ishares-core-msci-world-ucits-etf?siteEntryPassthrough=true"
)
ISHARES_SP500 = "https://www.ishares.com/de/privatanleger/de/produkte/253743/ishares-core-s-p-500-ucits-etf"
ISHARES_EM = "https://www.ishares.com/de/privatanleger/de/produkte/264659/"
ISHARES_EUROPE = "https://www.ishares.com/de/privatanleger/de/produkte/251861/SMEA"


CATALOG = {
    "Nordea Maailma Enhanced A": _entry(
        mandate="World", annual_cost=0.006, countries={"United States": 0.2416},
        source_url=NORDEA, composition_date="2026-08-31", estimated=False,
        note="Exact ISIN FI4000261300 is known. Verified partial country exposure is the six largest US equity holdings (NVIDIA, Apple, Alphabet, Microsoft, Amazon and Broadcom); cash and the remaining holdings are residual unknown.",
        cost_source=NORDEA, cost_date="2026-10-04"),
    "iShares Core MSCI World UCITS ETF USD (Acc)": _entry(
        mandate="World", annual_cost=0.002, countries={
            "United States": 0.7301, "Japan": 0.0586,
            "United Kingdom": 0.0332, "Canada": 0.0328,
            "France": 0.0220, "Switzerland": 0.0210,
            "Germany": 0.0204, "Australia": 0.0148,
            "Netherlands": 0.0145,
        },
        source_url=ISHARES_WORLD, composition_date="2026-10-02", estimated=True,
        note="Name match to EUNL.DE / IE00B4L5Y983; Balance does not store the ISIN. Issuer geography totals 94.74%; the remaining 5.28% is Other (4.92%) plus Cash and/or Derivatives (0.36%), retained as unknown rather than assigned to a country.",
        cost_source=ISHARES_WORLD, cost_date="2026-10-02"),
    "iShares Core S&P 500 ETF USD Acc": _entry(
        mandate="US", annual_cost=0.0007, countries={},
        source_url=ISHARES_SP500, composition_date="2026-10-01", estimated=True,
        note="Name match to SXR8.DE / IE00B5BMR087; Balance does not store the ISIN. The index mandate is US large cap, but no country fraction is fabricated.",
        cost_source=ISHARES_SP500, cost_date="2026-10-02"),
    "Xtrackers S&P 500 ETF 4C": _entry(
        mandate="US", annual_cost=None, countries={},
        source_url="https://etf.dws.com/", composition_date=None, estimated=True,
        note="S&P 500 name match only; exact ISIN/share class and current cost were not verified.",
        cost_source=None, cost_date=None),
    "iShares Core MSCI EM IMI UCITS ETF USD (Acc)": _entry(
        mandate="EM", annual_cost=0.0018, countries={},
        source_url=ISHARES_EM, composition_date="2026-09-18", estimated=True,
        note="Name match to IS3N.DE / IE00BKM4GZ66; Balance does not store the ISIN. Broad emerging-market index includes China.",
        cost_source=ISHARES_EM, cost_date="2026-09-29"),
    "iShares Core MSCI Europe UCITS ETF EUR (Acc)": _entry(
        mandate="Europe", annual_cost=None, countries={},
        source_url=ISHARES_EUROPE, composition_date=None, estimated=True,
        note="EUNK name match, but the official page identifies multiple Europe share classes; exact ISIN is not stored in Balance. Cost and country fractions require share-class confirmation.",
        cost_source=None, cost_date=None),
    "Amundi MSCI Emerging Ex China UCITS ETF Acc": _entry(
        mandate="EM", annual_cost=None, countries={},
        source_url="https://www.amundietf.com/", composition_date=None, estimated=True,
        note="Product name is not sufficient to distinguish Amundi's broad ex-China and ESG-selection products; obtain the ISIN before assigning costs or country weights.",
        cost_source=None, cost_date=None),
    "Nordnet USA Indeksi": _entry(
        mandate="US", annual_cost=0.0025, countries={},
        source_url=NORDNET_LIST, composition_date=None, estimated=True,
        note="Nordnet list verifies the 0.25% annual cost and US large-cap mandate. Exact country holdings were not verified; do not substitute SXR8 country data.",
        cost_source=NORDNET_LIST, cost_date="2026-10-04"),
    "Nordnet Eurooppa Indeksi": _entry(
        mandate="Europe", annual_cost=0.0025, countries={},
        source_url=NORDNET_LIST, composition_date=None, estimated=True,
        note="Nordnet list verifies the 0.25% annual cost. Exact index country weights were not verified.",
        cost_source=NORDNET_LIST, cost_date="2026-10-04"),
    "Nordnet Kehittyvät Markkinat Indeksi": _entry(
        mandate="EM", annual_cost=0.0039, countries={},
        source_url=NORDNET_LIST, composition_date=None, estimated=True,
        note="Nordnet list verifies the 0.39% annual cost. Exact index country weights were not verified.",
        cost_source=NORDNET_LIST, cost_date="2026-10-04"),
    "Nordnet Suomi Indeksi": _entry(
        mandate="Finland", annual_cost=0.0, countries={},
        source_url="https://www.nordnet.fi/rahastot/lista/nordnet-suomi-indeksi-eur-a401761d",
        composition_date="2026-08-31", estimated=True,
        note="Nordnet reports 89.63% euro area and 10.37% Europe ex-euro, with Finnish top holdings; a Finland=100% assumption would be wrong.",
        cost_source=NORDNET_LIST, cost_date="2026-10-04"),
    "Nordnet Norge Indeks": _entry(
        mandate="Norway", annual_cost=0.0, countries={},
        source_url=NORDNET_LIST, composition_date=None, estimated=True,
        note="Nordnet list verifies the 0% annual cost and Norway mandate. Exact country weights were not verified.",
        cost_source=NORDNET_LIST, cost_date="2026-10-04"),
    "Nordnet Sverige Index": _entry(
        mandate="Sweden", annual_cost=0.0, countries={},
        source_url=NORDNET_LIST, composition_date=None, estimated=True,
        note="Nordnet list verifies the 0% annual cost and Sweden mandate. Exact country weights were not verified.",
        cost_source=NORDNET_LIST, cost_date="2026-10-04"),
    "Apple Inc.": _entry(
        mandate="Apple", annual_cost=0.0, countries={"United States": 1.0},
        source_url="https://investor.apple.com/", composition_date=None, estimated=False,
        note="Direct common stock; no fund expense ratio.",
        cost_source=None, cost_date=None),
}


__all__ = ["CATALOG", "CATALOG_RETRIEVED_AT"]

CATALOG['iShares Core MSCI Europe UCITS ETF EUR (Acc)'].update({'countries': {'United Kingdom': 0.2228, 'France': 0.1466, 'Switzerland': 0.1419, 'Germany': 0.1364, 'Netherlands': 0.09630000000000001, 'Spain': 0.061500000000000006, 'Sweden': 0.053, 'Italy': 0.05, 'Denmark': 0.0245, 'Finland': 0.018600000000000002, 'Belgium': 0.017}, 'source_url': 'https://www.ishares.com/uk/individual/en/products/251861/ishares-msci-europe-ucits-etf-acc-fund', 'composition_date': '2026-10-02', 'estimated': True, 'note': 'Issuer country breakdown; name matched to accumulating share class because the broker snapshot omits ISIN. Other countries and cash/derivatives remain unknown.'})

CATALOG['iShares Core MSCI EM IMI UCITS ETF USD (Acc)'].update({'countries': {'Taiwan': 0.28800000000000003, 'South Korea': 0.2109, 'China': 0.1801, 'India': 0.11599999999999999, 'Brazil': 0.0361, 'South Africa': 0.028999999999999998, 'Saudi Arabia': 0.0218, 'Mexico': 0.0154, 'Poland': 0.011899999999999999, 'United Arab Emirates': 0.011899999999999999, 'Thailand': 0.011200000000000002, 'Malaysia': 0.0108}, 'source_url': 'https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf', 'composition_date': '2026-10-02', 'estimated': True, 'note': 'Issuer country breakdown; name matched to accumulating share class because the broker snapshot omits ISIN. Other countries and cash/derivatives remain unknown.'})
