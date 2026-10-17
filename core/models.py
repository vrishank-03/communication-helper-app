# core/models.py
import json
from datetime import datetime, timezone
from core.config import logger

logger.debug("core.models - Loading session defaults and helpers.")

SESSION_DEFAULTS = {
    "session_id": None,
    "educator_id": None,
    "educator_name": None,
    "phase": "LOGIN",
    "scenario": None,
    "conversation": [],
    "active_question": None,
    "active_q_index": 0,
    "round_number": 1,
    "submission_in_progress": False,
    "state_data": {},
}


def now_iso() -> str:
    """UTC timestamp for conversation / state entries."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    """Parse an ISO timestamp into a *local naive* datetime (matches DB TIMESTAMP columns)."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt

def safe_json(val, default=None):
    """Safely parse JSON from PostgreSQL JSONB columns."""
    if val is None:
        return default if default is not None else {}
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception as e:
            logger.error(f"core.models.safe_json - Failed to parse JSON: {e}")
            return default if default is not None else {}
    return default