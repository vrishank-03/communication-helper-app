# core/database.py
"""
PostgreSQL connection management and schema initialization.
"""
import psycopg2
from core.config import DATABASE_URL, logger

def get_conn():
    """Return a fresh psycopg2 connection. Callers must close or use `with`."""
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logger.critical(f"core.database - Database connection failed: {e}")
        raise

def init_db():
    """Create all tables if they don't already exist."""
    logger.info("core.database - Initializing database schema...")
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Identity layer
            cur.execute("""
                CREATE TABLE IF NOT EXISTS educators (
                    id    TEXT PRIMARY KEY,
                    name  TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. Evaluation state-machine
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evaluation_sessions (
                    id            TEXT PRIMARY KEY,
                    educator_id   TEXT REFERENCES educators(id),
                    scenario      TEXT NOT NULL,
                    nonce         TEXT,
                    phase         TEXT NOT NULL DEFAULT 'AWAITING_VIDEO',
                    round_number  INTEGER NOT NULL DEFAULT 1,
                    conversation  JSONB NOT NULL DEFAULT '[]'::jsonb,
                    state_data    JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 3. Anti-replay / integrity layer
            cur.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    id           TEXT PRIMARY KEY,
                    session_id   TEXT REFERENCES evaluation_sessions(id),
                    educator_id  TEXT REFERENCES educators(id),
                    file_sha256  TEXT UNIQUE NOT NULL,
                    task_type    TEXT NOT NULL,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 4. Educator flag / dispute system
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evaluation_flags (
                    id           TEXT PRIMARY KEY,
                    session_id   TEXT REFERENCES evaluation_sessions(id),
                    educator_id  TEXT REFERENCES educators(id),
                    reason       TEXT NOT NULL,
                    details      TEXT,
                    screenshot_1 TEXT,
                    screenshot_2 TEXT,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
    logger.info("core.database - Schema bootstrap complete.")