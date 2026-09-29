# services/submission.py
"""
Video submission: SHA-256 dedup + database registration.
"""
import os
import hashlib
import tempfile
import uuid
import json
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor
from core.database import get_conn
from core.config import logger

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
                    # Duplicate check
                    cur.execute(
                        "SELECT id, session_id FROM submissions "
                        "WHERE file_sha256 = %s LIMIT 1",
                        (file_hash,),
                    )
                    if cur.fetchone():
                        logger.warning(f"services.submission - Duplicate hash detected: {file_hash}")
                        raise ValueError(
                            "SECURITY ALERT: This exact video file has already been "
                            "submitted in another session. Please record a new, "
                            "original response."
                        )

                    # Register submission
                    cur.execute(
                        "INSERT INTO submissions "
                        "(id, session_id, educator_id, file_sha256, task_type) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (str(uuid.uuid4()), session_id, educator_id, file_hash, task_type),
                    )

                    # Append answer marker to conversation & move phase
                    conversation.append({"type": "answer", "content": "[Video queued]"})
                    cur.execute(
                        "UPDATE evaluation_sessions "
                        "SET phase = 'PROCESSING_VIDEO', "
                        "    conversation = %s::jsonb, "
                        "    updated_at = %s "
                        "WHERE id = %s",
                        (
                            json.dumps(conversation),
                            datetime.now(timezone.utc).isoformat(),
                            session_id,
                        ),
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