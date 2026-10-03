# services/auth.py
"""
Educator authentication and weekly evaluation limits.
"""
from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.config import LIMIT_WINDOW_DAYS, COMPLETED_PHASES, logger

# Sessions in these phases are finished; the educator may start a new one
TERMINAL_PHASES = set(COMPLETED_PHASES) | {"ERROR"}

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
    logger.debug(f"services.auth.get_weekly_evaluations - Checking limits for {educator_id}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM evaluation_sessions "
                "WHERE educator_id = %s "
                "AND phase = ANY(%s) "
                "AND created_at >= CURRENT_TIMESTAMP - make_interval(days => %s)",
                (educator_id, list(COMPLETED_PHASES), LIMIT_WINDOW_DAYS),
            )
            return int(cur.fetchone()[0])

def find_active_session(educator_id: str):
    """
    Return the most recent non-terminal session.
    If the latest session is terminal but just finished (within 15 mins), 
    return it so the user can review their results without being kicked to the start screen.
    """
    logger.debug(f"services.auth.find_active_session - Searching for {educator_id}")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT *, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - updated_at)) AS age_seconds "
                "FROM evaluation_sessions "
                "WHERE educator_id = %s "
                "ORDER BY updated_at DESC LIMIT 1",
                (educator_id,),
            )
            row = cur.fetchone()
            
            if not row:
                return None

            row = dict(row)
            age_seconds = float(row.pop("age_seconds") or 0)
            if row.get("phase") not in TERMINAL_PHASES:
                return row

            if age_seconds < 900:
                return row

            return None