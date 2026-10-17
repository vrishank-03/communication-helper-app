# app.py
"""
Streamlit entry-point.
Handles page routing using URL parameters to separate the Educator Portal and Admin Dashboard.
"""

import os
import json
import time
import streamlit as st

from core.database import init_db
from core.config import WEEKLY_EVAL_LIMIT
from core.models import SESSION_DEFAULTS

from services.auth import ensure_educator, get_weekly_evaluations, find_active_session
from services.scenario import generate_scenario
from services.submission import submit_video
from services.session import hydrate_from_row, sync_from_db, create_session, reset_session

from ui.styles import inject_styles
from ui.conversation import render_conversation
from ui.flag_system import render_flag_system
from ui.admin import render_admin_dashboard

from worker.task import process_video_submission

# ═════════════════════════════════════════════════════════════
# INITIALIZATION
# ═════════════════════════════════════════════════════════════
init_db()

st.set_page_config(
    page_title="OLL Evaluation System",
    page_icon="assets/oll_favicon.png", # Using your specific asset
    layout="wide",
)
inject_styles()

for _key, _val in SESSION_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = _val

# ═════════════════════════════════════════════════════════════
# ROUTING (Separate URL for Admin)
# ═════════════════════════════════════════════════════════════
# Access admin via: http://localhost:8501/?view=admin
query_params = st.query_params
is_admin_view = query_params.get("view") == "admin"

if is_admin_view:
    render_admin_dashboard()
    st.stop()

# ═════════════════════════════════════════════════════════════
# EDUCATOR PORTAL
# ═════════════════════════════════════════════════════════════
st.sidebar.title("OLL Educator Portal")
if st.sidebar.button("Logout"):
    reset_session()
    st.rerun()

st.title("Educator Evaluation Protocol")

if st.session_state.get("session_id") and st.session_state.phase not in {"LOGIN", "NOT_STARTED"}:
    sync_from_db()

phase = st.session_state.phase

if phase == "LOGIN":
    st.markdown("Please log in with your Educator ID to continue.")
    emp_id = st.text_input("Educator ID (e.g., EMP-104)").strip().upper()
    emp_name = st.text_input("Full Name").strip()

    if st.button("Authenticate"):
        if not emp_id or not emp_name:
            st.error("Both ID and Name are required.")
        else:
            ensure_educator(emp_id, emp_name)
            active = find_active_session(emp_id)

            st.session_state.educator_id = emp_id
            st.session_state.educator_name = emp_name

            if active:
                hydrate_from_row(active)
                st.success("Welcome back. Resuming your active session.")
                time.sleep(0.5)
                st.rerun()
            else:
                st.session_state.phase = "NOT_STARTED"
                st.rerun()

elif phase == "NOT_STARTED":
    st.markdown(
        f"Welcome, **{st.session_state.educator_name}**. "
        f"When you are ready, start the assessment to receive your scenario."
    )
    weekly_count = get_weekly_evaluations(st.session_state.educator_id)
    st.caption(f"Evaluations processed this week: {weekly_count}/{WEEKLY_EVAL_LIMIT}")

    if st.button("Start Assessment"):
        if weekly_count >= WEEKLY_EVAL_LIMIT:
            st.error(f"You have reached your limit of {WEEKLY_EVAL_LIMIT} evaluation attempts for this week.")
        else:
            with st.spinner("Generating a unique classroom scenario for you..."):
                scenario = generate_scenario()

            create_session(st.session_state.educator_id, st.session_state.educator_name, scenario)
            st.rerun()

elif phase in ("AWAITING_VIDEO", "FOLLOW_UP"):
    render_conversation()
    st.markdown("**Record a 1-3 minute video addressing the prompt above.**")

    upload_box = st.empty()
    with upload_box.container():
        uploaded = st.file_uploader(
            "Upload Video Response",
            type=["mp4", "mov", "webm"],
            key=f"vid_up_{st.session_state.round_number}_{st.session_state.active_q_index}",
        )
        submitted = st.button("Submit Video", disabled=st.session_state.get("submission_in_progress", False))

    if uploaded and submitted:
        st.session_state.submission_in_progress = True
        task_type = "INITIAL" if phase == "AWAITING_VIDEO" else "FOLLOW_UP"
        try:
            with st.spinner("Uploading and securely saving your video..."):
                tmp_path = submit_video(
                    uploaded,
                    st.session_state.session_id,
                    st.session_state.educator_id,
                    task_type,
                    st.session_state.conversation,
                )

            upload_box.empty()
            st.session_state.phase = "PROCESSING_VIDEO"

            process_video_submission.delay(
                session_id=st.session_state.session_id,
                file_path=tmp_path,
                task_type=task_type,
            )
            st.success("Success. Your video is now being reviewed by the system.")
            time.sleep(0.5)
            st.rerun()

        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:
            from services.submission import revert_submission
            revert_submission(st.session_state.session_id)
            st.error("Network connection issue detected. Please click 'Submit Video' again. Your work is saved.")
            time.sleep(2)
            sync_from_db(force=True)
            st.rerun()
        finally:
            st.session_state.submission_in_progress = False

elif phase == "PROCESSING_VIDEO":
    st.info("Your video has been submitted and is currently being evaluated. You may leave this page; your progress is saved.")
    render_conversation()

    @st.fragment(run_every=3)
    def _poll():
        from core.database import get_conn as _gc
        with _gc() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT phase, conversation, state_data, "
                    "EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - updated_at)) "
                    "FROM evaluation_sessions WHERE id = %s",
                    (st.session_state.session_id,),
                )
                row = cur.fetchone()

        if not row: return

        if row[0] != "PROCESSING_VIDEO":
            st.session_state.phase = row[0]
            conv = row[1]
            if isinstance(conv, str):
                try: conv = json.loads(conv)
                except Exception: conv = []
            st.session_state.conversation = conv if isinstance(conv, list) else []

            sd = row[2]
            if isinstance(sd, str):
                try: sd = json.loads(sd)
                except Exception: sd = {}
            if not isinstance(sd, dict): sd = {}
                
            st.session_state.active_question = sd.get("active_question")
            st.session_state.active_q_index = int(sd.get("active_q_index", 0))
            st.rerun()

        elapsed_seconds = float(row[3] or 0)
        if elapsed_seconds > 360:
            st.markdown("---")
            st.warning(
                "The evaluation worker has exceeded normal processing time. "
                "The job may have experienced an upstream timeout. You can continue waiting or retry your submission safely."
            )
            if st.button("Reset Submission and Try Again"):
                from services.submission import revert_submission
                revert_submission(st.session_state.session_id)
                sync_from_db(force=True)
                st.rerun()
    _poll()

elif phase == "CERTIFIED":
    st.success("Evaluation completed. You have passed the certification standard.")
    render_conversation()
    render_flag_system()

elif phase == "ESCALATED":
    st.warning("Evaluation concluded. Your session requires manual review by a Master Trainer to finalize certification.")
    render_conversation()
    render_flag_system()

elif phase == "ESCALATED_PLAGIARISM":
    st.error("Security Hold: Integrity Violation Detected")
    st.markdown("Your submission has been flagged for using a video file previously submitted by another user. This session is locked pending administrative review.")

elif phase == "ESCALATED_CONDUCT":
    st.error("Session Hold: Professional Conduct Review")
    st.markdown("Your submission has been flagged by our automated moderation system for language or content that may violate our professional educator standards. A Master Trainer will review this session manually.")
    st.info("Transcript withheld pending review.")

elif phase == "ERROR":
    st.error("Upstream Network Timeout: All AI models are temporarily overloaded.")
    st.markdown("Your video is safely stored. You do not need to re-record it. Click below to re-queue your video for processing.")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Retry Processing (Use same video)"):
            st.session_state.phase = "PROCESSING_VIDEO"
            # Re-triggering the task without re-uploading requires the worker script to handle the retained video path
            from worker.task import process_video_submission
            process_video_submission.delay(
                session_id=st.session_state.session_id,
                file_path=None, # Signal worker to use retained video
                task_type="INITIAL" if st.session_state.round_number == 1 else "FOLLOW_UP"
            )
            st.rerun()
    with col2:
        if st.button("Start Fresh (Upload new video)"):
            from services.submission import revert_submission
            revert_submission(st.session_state.session_id)
            sync_from_db(force=True)
            st.rerun()