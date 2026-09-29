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
# Restored the correct dynamic generator
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
    page_icon="assets/favicon.png",
    layout="wide",
)
inject_styles()

for _key, _val in SESSION_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = _val

# ═════════════════════════════════════════════════════════════
# SIDEBAR
# ═════════════════════════════════════════════════════════════
st.sidebar.title("OLL Educator Portal")
if st.sidebar.button("Logout"):
    reset_session()
    st.rerun()

# ═════════════════════════════════════════════════════════════
# MAIN LAYOUT
# ═════════════════════════════════════════════════════════════
st.title("Educator Evaluation Protocol")
tab_eval, tab_admin = st.tabs(["Speech Evaluation", "Admin Dashboard"])

# ─────────────────────────────────────────────────────────────
# TAB 1: EVALUATION
# ─────────────────────────────────────────────────────────────
with tab_eval:
    if st.session_state.get("session_id") and st.session_state.phase not in {"LOGIN", "NOT_STARTED"}:
        sync_from_db()

    phase = st.session_state.phase

    if phase == "LOGIN":
        st.markdown("Please log in with your Educator ID to continue.")
        emp_id = st.text_input("Educator ID (e.g., EMP-104)").strip()
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
                    st.success("Draft located. Resuming your active session...")
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
        st.caption(f"Completed this week: {weekly_count}/{WEEKLY_EVAL_LIMIT}")

        if st.button("Start Assessment"):
            if weekly_count >= WEEKLY_EVAL_LIMIT:
                st.error(f"You have already completed your {WEEKLY_EVAL_LIMIT} required evaluations for this week. Great job!")
            else:
                with st.spinner("Generating your evaluation scenario..."):
                    # Restored dynamic Gemini call
                    scenario = generate_scenario()

                create_session(st.session_state.educator_id, scenario)
                st.rerun()

    elif phase in ("AWAITING_VIDEO", "FOLLOW_UP"):
        render_conversation()
        st.markdown("**Record a 1–3 minute video addressing the prompt above.**")

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
                with st.spinner("Uploading and securely registering your video..."):
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
                st.success("Video submitted successfully. Your evaluation is now processing.")
                time.sleep(0.5)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Could not submit the video: {exc}")
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
                        "SELECT phase, conversation, state_data "
                        "FROM evaluation_sessions WHERE id = %s",
                        (st.session_state.session_id,),
                    )
                    row = cur.fetchone()

            if row and row[0] != "PROCESSING_VIDEO":
                st.session_state.phase = row[0]
                
                conv = row[1]
                if isinstance(conv, str):
                    try:
                        conv = json.loads(conv)
                    except:
                        conv = []
                st.session_state.conversation = conv if isinstance(conv, list) else []

                sd = row[2]
                if isinstance(sd, str):
                    try:
                        sd = json.loads(sd)
                    except:
                        sd = {}
                if not isinstance(sd, dict):
                    sd = {}
                    
                st.session_state.active_question = sd.get("active_question")
                st.session_state.active_q_index = int(sd.get("active_q_index", 0))
                st.rerun()

        _poll()

    elif phase == "CERTIFIED":
        st.success("🎉 Evaluation completed successfully. You have been CERTIFIED under the current protocol.")
        render_conversation()
        render_flag_system()

    elif phase == "ESCALATED":
        st.warning("Your evaluation has been forwarded for human review.")
        st.markdown("The automated evaluation could not complete the certification decision with sufficient confidence.")
        render_conversation()
        render_flag_system()

    elif phase == "ERROR":
        st.error("An error occurred while processing your evaluation. Please contact the administrator.")
        if st.button("Refresh Session"):
            sync_from_db(force=True)
            st.rerun()

# ─────────────────────────────────────────────────────────────
# TAB 2: ADMIN DASHBOARD
# ─────────────────────────────────────────────────────────────
with tab_admin:
    render_admin_dashboard()