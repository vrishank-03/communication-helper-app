# services/session.py
"""
Session persistence helpers for the Streamlit side.
"""
import re
import json
import uuid
import random
import streamlit as st
from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.models import SESSION_DEFAULTS, safe_json
from core.config import logger

def hydrate_from_row(row: dict):
    """Overwrite st.session_state with values from a DB row."""
    if not row:
        return
    logger.debug(f"services.session.hydrate_from_row - Hydrating session {row.get('id')}")
    st.session_state.educator_id = row.get("educator_id")
    st.session_state.session_id = row.get("id")
    st.session_state.phase = row.get("phase") or "LOGIN"
    st.session_state.round_number = int(row.get("round_number") or 1)
    st.session_state.scenario = row.get("scenario")
    st.session_state.conversation = safe_json(row.get("conversation"), [])

    sd = safe_json(row.get("state_data"), {})
    st.session_state.active_question = sd.get("active_question")
    st.session_state.active_q_index = int(sd.get("active_q_index", 0))

def sync_from_db(force: bool = False) -> bool:
    """Poll the DB for phase changes written by Celery."""
    sid = st.session_state.get("session_id")
    if not sid:
        return False

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM evaluation_sessions WHERE id = %s", (sid,))
            row = cur.fetchone()
            if not row:
                return False
            db_phase = row.get("phase", "LOGIN")
            if not force and db_phase == st.session_state.get("phase"):
                return False
            
            logger.info(f"services.session.sync_from_db - Phase transition detected: {db_phase}")
            hydrate_from_row(dict(row))
            return True

def create_session(educator_id: str, educator_name: str, scenario: str) -> str:
    """Insert a fresh evaluation session and return its ID using a NAME_01 format."""
    clean_name = re.sub(r"[^A-Z0-9]", "", (educator_name or "").upper())
    if not clean_name:
        clean_name = re.sub(r"[^A-Z0-9]", "", educator_id.upper())

    nonce = str(random.randint(100000, 999999))
    conversation = [{"type": "scenario", "content": scenario}]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM evaluation_sessions WHERE educator_id = %s",
                (educator_id,),
            )
            count = cur.fetchone()[0] + 1

            # Bump the number until the ID is unused (handles same-name educators)
            while True:
                sid = f"{clean_name}_{count:02d}"
                cur.execute("SELECT 1 FROM evaluation_sessions WHERE id = %s", (sid,))
                if cur.fetchone() is None:
                    break
                count += 1

            logger.info(f"services.session.create_session - Creating new session {sid} for {educator_id}")

            cur.execute(
                "INSERT INTO evaluation_sessions "
                "(id, educator_id, scenario, nonce, phase, round_number, "
                " conversation, state_data) "
                "VALUES (%s, %s, %s, %s, 'AWAITING_VIDEO', 1, "
                " %s::jsonb, %s::jsonb)",
                (sid, educator_id, scenario, nonce,
                 json.dumps(conversation), json.dumps({})),
            )
        conn.commit()

    st.session_state.session_id = sid
    st.session_state.scenario = scenario
    st.session_state.phase = "AWAITING_VIDEO"
    st.session_state.round_number = 1
    st.session_state.conversation = conversation

    return sid

def reset_session():
    """Clear st.session_state back to defaults."""
    logger.debug("services.session.reset_session - Resetting UI state.")
    for key, value in SESSION_DEFAULTS.items():
        if isinstance(value, (list, dict)):
            st.session_state[key] = type(value)(value)
        else:
            st.session_state[key] = value