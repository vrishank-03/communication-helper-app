# core/models.py
import json
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
    "submission_in_progress": False
}

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