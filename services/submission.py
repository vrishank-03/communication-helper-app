# services/submission.py
"""
Video submission: SHA-256 dedup + database registration + Integrity Forensics.
"""
import os
import hashlib
import tempfile
import uuid
import json

from psycopg2.extras import RealDictCursor
from core.database import get_conn
from core.config import SECURITY_FLAG_REASON, logger

def submit_video(uploaded_file, session_id: str, educator_id: str, task_type: str, conversation: list) -> str:
    """Register a video submission and return the path to the temp file."""
    if not session_id or not educator_id:
        logger.error("services.submission - Invalid session or unauthenticated user.")
        raise RuntimeError("Invalid session or unauthenticated user.")

    logger.debug(f"services.submission - Processing video for session {session_id}")
    file_bytes = uploaded_file.getvalue()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    extension = os.path.splitext(uploaded_file.name or "")[1].lower()
    if extension not in {".mp4", ".mov", ".webm"}:
        extension = ".mp4"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        with get_conn() as conn:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # ── FORENSICS: Duplicate check ──────────────────────────────
                    cur.execute(
                        "SELECT id, session_id, educator_id FROM submissions "
                        "WHERE file_sha256 = %s LIMIT 1",
                        (file_hash,),
                    )
                    existing = cur.fetchone()
                    
                    if existing:
                        orig_ed = existing.get("educator_id")
                        if orig_ed == educator_id:
                            # Branch A: Honest Mistake
                            logger.warning(f"services.submission - Honest duplicate by {educator_id}")
                            raise ValueError(
                                "It looks like you just uploaded the exact same video file again. "
                                "Please record a fresh response for this question!"
                            )
                        else:
                            # Branch B: Genuine Cheating / Plagiarism
                            logger.critical(f"services.submission - PLAGIARISM: {educator_id} copied {orig_ed}")
                            
                            # Log Critical Flag
                            cur.execute(
                                "INSERT INTO evaluation_flags "
                                "(id, session_id, educator_id, reason, details) "
                                "VALUES (%s, %s, %s, %s, %s)",
                                (
                                    str(uuid.uuid4()), 
                                    session_id, 
                                    educator_id, 
                                    SECURITY_FLAG_REASON, 
                                    f"Video hash matches file originally submitted by Educator {orig_ed} in session {existing['session_id']}"
                                )
                            )
                            # Lock Session
                            cur.execute(
                                "UPDATE evaluation_sessions SET phase = 'ESCALATED_PLAGIARISM', "
                                "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                                (session_id,)
                            )
                            conn.commit()
                            raise ValueError(
                                "This video matches a submission from another educator. "
                                "Your session has been placed on hold for review by a Master Trainer."
                            )

                    # ── Register valid submission ───────────────────────────────
                    cur.execute(
                        "INSERT INTO submissions "
                        "(id, session_id, educator_id, file_sha256, task_type) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (str(uuid.uuid4()), session_id, educator_id, file_hash, task_type),
                    )

                    # Append answer marker to conversation & move phase
                    conversation.append({"type": "answer", "content": "[Video queued]"})
                    
                    # FIXED: Using PostgreSQL CURRENT_TIMESTAMP to eliminate Python/DB timezone mismatches
                    cur.execute(
                        "UPDATE evaluation_sessions "
                        "SET phase = 'PROCESSING_VIDEO', "
                        "    conversation = %s::jsonb, "
                        "    updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = %s",
                        (json.dumps(conversation), session_id),
                    )
                conn.commit()
                logger.info(f"services.submission - Successfully registered video {file_hash}")
            except Exception as e:
                conn.rollback()
                logger.error(f"services.submission - DB transaction failed: {e}")
                raise

        return tmp_path

    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
            logger.debug(f"services.submission - Cleaned up temp file {tmp_path}")
        raise

def revert_submission(session_id: str):
    """Rollback the database state if Celery fails to accept the task or gets stuck."""
    logger.warning(f"services.submission - Reverting stuck submission for session {session_id}")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT conversation FROM evaluation_sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
            if not row: return
            
            conv = row['conversation']
            if isinstance(conv, str):
                try: conv = json.loads(conv)
                except: conv = []
            
            # Pop the "[Video queued]" marker
            if conv and conv[-1].get("type") == "answer":
                conv.pop()
                
            phase = "AWAITING_VIDEO"
            for entry in reversed(conv):
                if entry.get("type") == "question":
                    phase = "FOLLOW_UP"
                    break
                    
            cur.execute(
                "UPDATE evaluation_sessions SET phase = %s, conversation = %s::jsonb WHERE id = %s",
                (phase, json.dumps(conv), session_id)
            )
            
            cur.execute(
                "DELETE FROM submissions WHERE id IN ("
                "SELECT id FROM submissions WHERE session_id = %s ORDER BY created_at DESC LIMIT 1"
                ")",
                (session_id,)
            )
        conn.commit()