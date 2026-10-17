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

            # 5. Flag resolution tracking (safe to re-run on existing databases)
            cur.execute("""
                ALTER TABLE evaluation_flags
                    ADD COLUMN IF NOT EXISTS status          TEXT NOT NULL DEFAULT 'OPEN',
                    ADD COLUMN IF NOT EXISTS resolution_note TEXT,
                    ADD COLUMN IF NOT EXISTS resolved_at     TIMESTAMP
            """)

            # 6. Video integrity fingerprints (only verified rows are used as references)
            cur.execute("""
                ALTER TABLE submissions
                    ADD COLUMN IF NOT EXISTS frame_hashes   JSONB,
                    ADD COLUMN IF NOT EXISTS face_embedding JSONB,
                    ADD COLUMN IF NOT EXISTS thumbnail      TEXT,
                    ADD COLUMN IF NOT EXISTS verified       BOOLEAN NOT NULL DEFAULT FALSE
            """)

            # 7. Educator suspension (set by admins after a confirmed conduct/integrity violation)
            cur.execute("""
                ALTER TABLE educators
                    ADD COLUMN IF NOT EXISTS suspended_at      TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS suspension_reason TEXT
            """)

            # 8. Attempts an admin has waived from the weekly limit
            cur.execute("""
                ALTER TABLE evaluation_sessions
                    ADD COLUMN IF NOT EXISTS excluded_from_limit BOOLEAN NOT NULL DEFAULT FALSE
            """)

            # 9. Richer ticket metadata (conduct severity, structured evidence, who resolved it and how)
            cur.execute("""
                ALTER TABLE evaluation_flags
                    ADD COLUMN IF NOT EXISTS severity          TEXT,
                    ADD COLUMN IF NOT EXISTS meta              JSONB,
                    ADD COLUMN IF NOT EXISTS resolution_action TEXT,
                    ADD COLUMN IF NOT EXISTS resolved_by       TEXT
            """)

            # 10. One row per worker attempt: feeds the AI/system health view
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processing_events (
                    id                  SERIAL PRIMARY KEY,
                    session_id          TEXT,
                    job_id              TEXT,
                    task_type           TEXT,
                    attempt             INTEGER,
                    outcome             TEXT,
                    error_kind          TEXT,
                    error_message       TEXT,
                    transcription_model TEXT,
                    grading_model       TEXT,
                    model_trace         JSONB,
                    duration_seconds    DOUBLE PRECISION,
                    started_at          TIMESTAMP,
                    finished_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 11. Admin audit trail
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id            SERIAL PRIMARY KEY,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    admin_name    TEXT,
                    action        TEXT NOT NULL,
                    target_type   TEXT,
                    target_id     TEXT,
                    session_id    TEXT,
                    note          TEXT,
                    internal_note TEXT,
                    meta          JSONB
                )
            """)

            # 12. Soft locks so two reviewers don't work the same ticket
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_claims (
                    item_key   TEXT PRIMARY KEY,
                    admin_name TEXT NOT NULL,
                    claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
    logger.info("core.database - Schema bootstrap complete.")