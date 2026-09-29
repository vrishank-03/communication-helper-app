# services/auth.py
"""
Educator authentication and weekly evaluation limits.
"""
from datetime import datetime, timedelta, timezone
from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.config import WEEKLY_EVAL_LIMIT, LIMIT_WINDOW_DAYS, logger

def ensure_educator(educator_id: str, name: str):
    """Upsert an educator record. Returns the educator_id."""
    logger.debug(f"services.auth.ensure_educator - Authenticating {educator_id}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO educators (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
                (educator_id, name),
            )
        conn.commit()
    return educator_id

def get_weekly_evaluations(educator_id: str) -> int:
    """Count completed evaluations inside the rolling window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LIMIT_WINDOW_DAYS)
    logger.debug(f"services.auth.get_weekly_evaluations - Checking limits for {educator_id}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM evaluation_sessions "
                "WHERE educator_id = %s "
                "AND phase IN ('CERTIFIED', 'ESCALATED') "
                "AND created_at >= %s",
                (educator_id, cutoff),
            )
            return int(cur.fetchone()[0])

def find_active_session(educator_id: str):
    """Return the most recent non-terminal session for this educator, or None."""
    logger.debug(f"services.auth.find_active_session - Searching for {educator_id}")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM evaluation_sessions "
                "WHERE educator_id = %s "
                "AND phase NOT IN ('CERTIFIED', 'ESCALATED', 'ERROR') "
                "ORDER BY created_at DESC LIMIT 1",
                (educator_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None