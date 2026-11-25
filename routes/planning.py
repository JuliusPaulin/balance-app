"""Planning: the Balance baseline, and the scenarios saved on this machine."""

from datetime import date

from flask import Blueprint, jsonify, request

from core import current_user_id
from data import db
from data.schema import db_conn
from services.planning import clean_plan, default_assumptions, read_baseline

bp = Blueprint("planning", __name__)


def _stored_plan(conn, user_id):
    row = conn.execute(
        "SELECT payload, updated_at FROM planning_plans WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if row is None:
        return None, None
    try:
        payload = db.load_json(row["payload"])
    except (TypeError, ValueError):
        return None, row["updated_at"]
    plan, error = clean_plan(payload)
    if error:
        return None, row["updated_at"]
    return plan, row["updated_at"]


@bp.get("/api/planning")
def get_planning():
    """The figures Balance can fill in, and the plan saved for this user.

    ``plan`` is null until something has been saved. ``defaults`` is a full
    assumption set with those figures already written in, which is what Reset
    puts back. The saved plan is left untouched.
    """
    uid = current_user_id()
    with db_conn() as conn:
        baseline = read_baseline(conn, uid)
        plan, updated_at = _stored_plan(conn, uid)
    return jsonify({
        "baseline": baseline,
        "defaults": default_assumptions(baseline),
        "plan": plan,
        "updated_at": updated_at,
        "start_year": date.today().year,
    })


@bp.put("/api/planning")
def put_planning():
    """Replace the saved plan. A plan that does not check out is refused,
    and the one already stored stays as it was."""
    plan, error = clean_plan(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO planning_plans (user_id, payload, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id) DO UPDATE
            SET payload = excluded.payload, updated_at = now()
            """,
            (uid, db.Json(plan)),
        )
    return jsonify(plan)
