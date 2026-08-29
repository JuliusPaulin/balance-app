"""Per-month free-text notes."""

from flask import Blueprint, request, jsonify
from database import db_conn
from core import current_user_id

bp = Blueprint("notes", __name__)


@bp.route("/api/notes/<month>")
def get_note(month):
    uid = current_user_id()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM month_notes WHERE user_id = %s AND month = %s",
            (uid, month),
        ).fetchone()
    return jsonify(dict(row) if row else {"month": month, "note": ""})


@bp.route("/api/notes/<month>", methods=["PUT"])
def save_note(month):
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO month_notes (user_id, month, note) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, month)
            DO UPDATE SET note = excluded.note, updated_at = now()
        """, (uid, month, data.get("note", "")))
        row = conn.execute(
            "SELECT * FROM month_notes WHERE user_id = %s AND month = %s",
            (uid, month),
        ).fetchone()
    return jsonify(dict(row))


@bp.route("/api/notes")
def get_all_notes():
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT month FROM month_notes WHERE user_id = %s AND note != ''",
            (uid,),
        ).fetchall()
    return jsonify([r["month"] for r in rows])
