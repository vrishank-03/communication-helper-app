# services/review.py
"""
Admin review actions: decide escalated sessions, resolve security holds,
reopen failed sessions and close educator reports.

Each action runs in one transaction, locks the session row and checks its
current phase, so the same item can never be resolved twice.
"""
import json
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.models import safe_json
from core.config import SECURITY_FLAG_REASON, logger

# action -> (phase the session must be in, resulting phase; None = resume the evaluation)
SESSION_ACTIONS = {
    "CERTIFIED":           ("ESCALATED", "CERTIFIED"),
    "NOT_CERTIFIED":       ("ESCALATED", "NOT_CERTIFIED"),
    "VIOLATION_CONFIRMED": ("ESCALATED_PLAGIARISM", "REJECTED"),
    "HOLD_CLEARED":        ("ESCALATED_PLAGIARISM", None),
    "REOPENED":            ("ERROR", None),
}

# Negative outcomes must carry an explanation for the educator
NOTE_REQUIRED = {"NOT_CERTIFIED", "VIOLATION_CONFIRMED"}


class ReviewError(Exception):
    """Raised when a review action cannot be applied."""


def _resume_phase(state_data: dict) -> str:
    return "FOLLOW_UP" if state_data.get("active_question") else "AWAITING_VIDEO"


def resolve_session(session_id: str, action: str, note: str = "") -> str:
    """Apply an admin decision to a session and return its new phase."""
    if action not in SESSION_ACTIONS:
        raise ReviewError(f"Unknown review action: {action}")

    note = (note or "").strip()
    if action in NOTE_REQUIRED and not note:
        raise ReviewError("Please add a note for the educator explaining this decision.")

    expected_phase, target_phase = SESSION_ACTIONS[action]

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT phase, conversation, state_data FROM evaluation_sessions "
                "WHERE id = %s FOR UPDATE",
                (session_id,),
            )
            row = cur.fetchone()
            if not row or row["phase"] != expected_phase:
                raise ReviewError("This session has already been handled. Refresh to see its current status.")

            conversation = safe_json(row["conversation"], [])
            state_data = safe_json(row["state_data"], {})

            if action == "REOPENED" and conversation and conversation[-1].get("type") == "answer":
                # Drop the unprocessed video marker and its hash so the same file can be resubmitted.
                conversation.pop()
                cur.execute(
                    "DELETE FROM submissions WHERE id IN ("
                    "SELECT id FROM submissions WHERE session_id = %s "
                    "ORDER BY created_at DESC LIMIT 1)",
                    (session_id,),
                )

            new_phase = target_phase or _resume_phase(state_data)
            conversation.append({
                "type": "admin_review",
                "action": action,
                "note": note,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            })

            cur.execute(
                "UPDATE evaluation_sessions "
                "SET phase = %s, conversation = %s::jsonb, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (new_phase, json.dumps(conversation, default=str), session_id),
            )

            if expected_phase == "ESCALATED_PLAGIARISM":
                cur.execute(
                    "UPDATE evaluation_flags "
                    "SET status = 'RESOLVED', resolution_note = %s, resolved_at = CURRENT_TIMESTAMP "
                    "WHERE session_id = %s AND reason = %s AND status = 'OPEN'",
                    (note or action.replace("_", " ").capitalize(), session_id, SECURITY_FLAG_REASON),
                )
        conn.commit()

    logger.info(f"services.review - Session {session_id}: {action} -> {new_phase}")
    return new_phase


def resolve_report(flag_id: str, note: str = "") -> None:
    """Close an educator report with an optional note shown to the educator."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE evaluation_flags "
                "SET status = 'RESOLVED', resolution_note = %s, resolved_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND status = 'OPEN'",
                ((note or "").strip() or None, flag_id),
            )
            if cur.rowcount != 1:
                raise ReviewError("This report has already been resolved.")
        conn.commit()
    logger.info(f"services.review - Report {flag_id} resolved")
