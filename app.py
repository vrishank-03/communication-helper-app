# app.py
"""
Streamlit entry-point.
Handles page routing across evaluation phases + admin dashboard.
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

st.set_page_config(
    page_title="OLL Evaluation System",
    page_icon="Untitled design.png",
    layout="wide",
)


@st.cache_resource
def _bootstrap_db():
    init_db()
    return True


_bootstrap_db()
inject_styles()

for _key, _val in SESSION_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = _val

with st.sidebar:
    st.title("OLL Educator Portal")
    if st.session_state.get("educator_id"):
        st.caption("Signed in as")
        st.markdown(f"**{st.session_state.get('educator_name') or ''}**  \n{st.session_state.educator_id}")
        if st.button("Log out", width="stretch"):
            reset_session()
            st.rerun()

st.title("Educator Evaluation Protocol")
tab_eval, tab_admin = st.tabs(["Speech Evaluation", "Admin Dashboard"])

with tab_eval:
    if st.session_state.get("session_id") and st.session_state.phase not in {"LOGIN", "NOT_STARTED"}:
        sync_from_db()

    phase = st.session_state.phase

    if phase == "LOGIN":
        st.markdown("Please log in with your Educator ID to continue.")
        emp_id = st.text_input("Educator ID (e.g., EMP-104)").strip().upper()
        emp_name = st.text_input("Full Name").strip()

        if st.button("Authenticate", type="primary"):
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

        if st.button("Start Assessment", type="primary"):
            if weekly_count >= WEEKLY_EVAL_LIMIT:
                st.error(f"You have reached your limit of {WEEKLY_EVAL_LIMIT} evaluation attempts for this week.")
            else:
                with st.spinner("Generating a unique classroom scenario for you..."):
                    scenario = generate_scenario()

                create_session(
                    st.session_state.educator_id,
                    st.session_state.educator_name,
                    scenario,
                )
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
            submitted = st.button("Submit Video", type="primary", disabled=st.session_state.get("submission_in_progress", False))

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
                # A security hold changes the phase in the DB; show the hold screen right away.
                if sync_from_db():
                    st.session_state.submission_in_progress = False
                    st.rerun()
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

    elif phase == "NOT_CERTIFIED":
        st.warning("Review complete. Your session did not meet the certification standard on this attempt. The reviewer's notes are shown below.")
        render_conversation()
        render_flag_system()

    elif phase == "ESCALATED_PLAGIARISM":
        st.error(
            "Your session is on hold. The video you submitted matches a submission from another educator. "
            "A Master Trainer will review the session before you can continue."
        )
        render_conversation()
        render_flag_system()

    elif phase == "REJECTED":
        st.error("This session has been closed following a submission integrity review.")
        render_conversation()

    elif phase == "ERROR":
        st.error(
            "A system error occurred while evaluating your video. An administrator has been notified "
            "and can reopen your session so you can submit again."
        )
        if st.button("Refresh Page"):
            sync_from_db(force=True)
            st.rerun()

with tab_admin:
    render_admin_dashboard()