"""Per-month notes: read, write, and which months the app marks as noted."""

from helpers import add_tx, cat_id  # noqa: F401  (kept for symmetry with siblings)


def test_a_month_with_no_note_reads_as_empty(client, login, make_user):
    """The note box opens on every month, so a missing row is not a 404."""
    make_user()
    res = client.get("/api/notes/2026-04")
    assert res.status_code == 200
    assert res.get_json() == {"month": "2026-04", "note": ""}


def test_write_then_read(client, login, make_user):
    make_user()
    saved = client.put("/api/notes/2026-04", json={"note": "Bought a bike"})
    assert saved.status_code == 200
    assert saved.get_json()["note"] == "Bought a bike"
    assert client.get("/api/notes/2026-04").get_json()["note"] == "Bought a bike"


def test_writing_twice_replaces_rather_than_duplicates(client, login, make_user):
    make_user()
    client.put("/api/notes/2026-04", json={"note": "first"})
    client.put("/api/notes/2026-04", json={"note": "second"})
    assert client.get("/api/notes/2026-04").get_json()["note"] == "second"
    assert client.get("/api/notes").get_json() == ["2026-04"]


def test_the_month_list_skips_emptied_notes(client, login, make_user):
    """Clearing a note has to clear the marker with it, or the dot outlives it."""
    make_user()
    client.put("/api/notes/2026-04", json={"note": "something"})
    client.put("/api/notes/2026-05", json={"note": "else"})
    client.put("/api/notes/2026-04", json={"note": ""})

    assert client.get("/api/notes").get_json() == ["2026-05"]
