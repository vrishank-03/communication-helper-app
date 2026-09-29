"""
Educator flag / dispute system.
"""

import os
import uuid
from datetime import datetime, timezone

import streamlit as st

from core.database import get_conn

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FLAG_DIR = os.path.join(_APP_DIR, "data", "flag_attachments")
os.makedirs(_FLAG_DIR, exist_ok=True)


def render_flag_system():
    """Render the 'Report an Issue' expander with form."""
    sid = st.session_state.get("session_id")
    if not sid:
        return

    with st.expander("Report an Issue with this Evaluation"):
        st.caption(
            "If you believe the automated assessment mischaracterized "
            "your response, submit a review request for the Master "
            "Trainer team."
        )

        flag_reason = st.selectbox(
            "Category",
            [
                "Feedback does not match what I said",
                "Technical malfunction / Audio cut out",
                "Question was unclear or contradictory",
                "Other discrepancy",
            ],
            key=f"cat_{sid}",
        )
        flag_details = st.text_area(
            "Details",
            placeholder="Explain specifically where the assessment diverged...",
            key=f"det_{sid}",
        )
        uploaded_shots = st.file_uploader(
            "Attach Screenshots (Max 2)",
            type=["png", "jpg", "jpeg"],
            accept_multiple_files=True,
            key=f"files_{sid}",
        )

        if st.button("Submit Report", key=f"btn_{sid}"):
            if not flag_details.strip():
                st.error("Please provide a brief explanation before submitting.")
            elif len(uploaded_shots) > 2:
                st.error("Maximum 2 screenshots allowed.")
            else:
                s1, s2 = None, None
                for idx, file in enumerate(uploaded_shots[:2]):
                    path = os.path.join(
                        _FLAG_DIR,
                        f"{sid[:8]}_shot_{idx}_{file.name}",
                    )
                    with open(path, "wb") as f:
                        f.write(file.read())
                    if idx == 0:
                        s1 = path
                    elif idx == 1:
                        s2 = path

                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO evaluation_flags "
                            "(id, session_id, educator_id, reason, details, "
                            " screenshot_1, screenshot_2, created_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                str(uuid.uuid4()),
                                sid,
                                st.session_state.get("educator_id"),
                                flag_reason,
                                flag_details,
                                s1, s2,
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                    conn.commit()
                st.success(
                    "Report submitted successfully. "
                    "A Master Trainer will audit this session."
                )
