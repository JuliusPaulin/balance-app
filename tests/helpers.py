"""Small helpers shared by the HTTP-level route tests.

The local user is seeded with the default categories, so tests look their
category ids up by name rather than creating duplicates of "Groceries".
"""


def cat_id(client, name, type_="expense"):
    """The seeded category's id. Name AND type — "Other" exists on both sides."""
    for c in client.get("/api/categories").get_json():
        if c["name"] == name and c["type"] == type_:
            return c["id"]
    raise AssertionError(f"no seeded {type_} category named {name!r}")


def add_tx(client, date, store, category_id, amount, type_="expense"):
    """POST a transaction and return the created row."""
    res = client.post("/api/transactions", json={
        "date": date, "store": store, "category_id": category_id,
        "amount": amount, "type": type_,
    })
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def stores(payload):
    """The store names in a /api/transactions response, in the order returned."""
    return [t["store"] for t in payload["items"]]


# ---------------------------------------------------------------------------
# Broker export builders — shared by the parser unit tests and the HTTP tests
# for /api/networth/import-investments.
# ---------------------------------------------------------------------------
def nordnet_csv_bytes(rows, header=None, encoding="utf-16", delimiter="\t"):
    """A Nordnet portfolio export as bytes.

    The real files are UTF-16 LE, TAB-delimited, Finnish headers, decimal comma
    — every one of those is load-bearing in ``parse_nordnet_csv``, so the
    default here is the real shape and a test that wants a different one says
    so explicitly.
    """
    header = header or ["Osakkeet", "Määrä", "Arvo EUR", "Tuotto, %",
                        "Tuotto, EUR", "Valuutta"]
    lines = [delimiter.join(str(c) for c in r) for r in [header, *rows]]
    return "\r\n".join(lines).encode(encoding)


NORDEA_HEADER = ["Type", "Account", "ISIN", "Currency", "Name", "Holdings",
                 "Value in base currency", "Value on account level",
                 "Value change on account level", "Purchase value"]


def nordea_xlsx_bytes(rows, header=None, timestamp="Omistukset 24.5.2026 12:30",
                      sheet="Holdings"):
    """A Nordea ``Omistukset.xlsx`` as bytes: row 0 = export timestamp (the only
    place the snapshot date is written), row 1 = headers, then the data rows."""
    import io as _io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append([timestamp])
    ws.append(list(header if header is not None else NORDEA_HEADER))
    for r in rows:
        ws.append(list(r))
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
