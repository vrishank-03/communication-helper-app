"""
Admin dashboard — password-protected view of all sessions, scores, and flags.
"""

import os
import json
import hmac
from datetime import datetime, timedelta, timezone

import streamlit as st
import pandas as pd

from core.database import get_conn
from core.config import LIMIT_WINDOW_DAYS
from ui.conversation import render_conversation


# ── Status label mapping ─────────────────────────────────────
_STATUS_LABELS = {
    "CERTIFIED": "Certified",
    "ESCALATED": "Under review",
    "ERROR": "Error",
    "NOT_STARTED": "In progress",
    "AWAITING_VIDEO": "In progress",
    "FOLLOW_UP": "In progress",
    "PROCESSING_VIDEO": "In progress",
}


# ── Helpers ──────────────────────────────────────────────────
def _admin_password():
    try:
        pw = st.secrets.get("ADMIN_PASSWORD")
    except Exception:
        pw = None
    return pw or os.getenv("ADMIN_PASSWORD")


def _try_admin_unlock():
    pw = _admin_password()
    entered = st.session_state.get("admin_pw_input", "")
    ok = bool(pw) and hmac.compare_digest(entered.encode(), str(pw).encode())
    st.session_state.admin_ok = ok
    st.session_state.admin_pw_failed = not ok


def _lock_admin():
    st.session_state.admin_ok = False


def _load_admin_data():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, educator_id, scenario, phase, round_number, "
                "conversation, state_data, created_at, updated_at "
                "FROM evaluation_sessions ORDER BY created_at DESC"
            )
            cols = [d[0] for d in cur.description]
            sessions = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute(
                "SELECT id, session_id, educator_id, reason, details, "
                "screenshot_1, screenshot_2, created_at "
                "FROM evaluation_flags ORDER BY created_at DESC"
            )
            fcols = [d[0] for d in cur.description]
            flags = [dict(zip(fcols, r)) for r in cur.fetchall()]

    return sessions, flags


def _safe_conv(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


# ── Main render ──────────────────────────────────────────────
def render_admin_dashboard():
    """Render the full admin dashboard tab."""
    if not _admin_password():
        st.warning(
            "The admin dashboard is disabled until an `ADMIN_PASSWORD` is set "
            "(in `.streamlit/secrets.toml` or as an environment variable)."
        )
        return

    if not st.session_state.get("admin_ok"):
        with st.form("admin_login"):
            st.text_input("Admin password", type="password", key="admin_pw_input")
            st.form_submit_button("Unlock", on_click=_try_admin_unlock)
        if st.session_state.get("admin_pw_failed"):
            st.error("Incorrect password.")
        return

    st.button("Lock dashboard", key="admin_lock_btn", on_click=_lock_admin)

    sessions, flags = _load_admin_data()
    flag_counts = {}
    for f in flags:
        flag_counts[f["session_id"]] = flag_counts.get(f["session_id"], 0) + 1

    statuses = [_STATUS_LABELS.get(s["phase"], s["phase"]) for s in sessions]
    window_start = (datetime.now(timezone.utc) - timedelta(days=LIMIT_WINDOW_DAYS)).isoformat()

    # ── Metrics row ──
    m = st.columns(5)
    m[0].metric("Sessions (all time)", len(sessions))
    m[1].metric(
        f"Last {LIMIT_WINDOW_DAYS} days",
        sum(1 for s in sessions if str(s.get("created_at", "")) >= window_start),
    )
    m[2].metric("Certified", statuses.count("Certified"))
    m[3].metric("Under review", statuses.count("Under review"))
    m[4].metric("Errors", statuses.count("Error"))

    # ── Sessions table ──
    st.markdown("#### Sessions")
    f_status, f_name = st.columns([2, 3])
    pick_status = f_status.multiselect(
        "Status",
        ["Certified", "Under review", "In progress", "Error"],
        key="admin_status_filter",
    )
    name_query = f_name.text_input(
        "Search educator", key="admin_name_filter",
    ).strip().lower()

    view = [
        s for s in sessions
        if (not pick_status or _STATUS_LABELS.get(s["phase"], s["phase"]) in pick_status)
        and (not name_query or name_query in (s.get("educator_id") or "").lower())
    ]

    if not view:
        st.info("No sessions match these filters.")
    else:
        rows = []
        for s in view:
            conv = _safe_conv(s.get("conversation"))
            rows.append({
                "Started": str(s.get("created_at", ""))[:16],
                "Educator": s.get("educator_id") or "Anonymous",
                "Status": _STATUS_LABELS.get(s.get("phase"), s.get("phase")),
                "Videos": sum(1 for e in conv if e.get("type") == "answer"),
                "Reports": flag_counts.get(s["id"], 0),
                "Session": str(s["id"])[:8],
            })
        st.dataframe(rows, hide_index=True)

        # ── Detail view ──
        options = {
            f"{r['Educator']} · {r['Started']} · {r['Status']} · {r['Session']}": s
            for r, s in zip(rows, view)
        }
        choice = st.selectbox("Open a session", list(options), key="admin_session_pick")
        picked = options.get(choice)
        if picked:
            conv = _safe_conv(picked.get("conversation"))
            st.text(f"Session ID: {picked['id']}")
            st.markdown("**What the educator saw**")
            render_conversation(conv)

    # ── Flags ──
    if flags:
        st.markdown("#### Reported Issues")
        for f in flags:
            label = (
                f"{str(f.get('created_at', ''))[:16]} · "
                f"{f.get('educator_id') or 'Anonymous'} · "
                f"{f['reason']}"
            )
            with st.expander(label):
                st.text(f"Session: {f['session_id']}")
                st.text(f.get("details") or "")
                for p in (f.get("screenshot_1"), f.get("screenshot_2")):
                    if p and os.path.exists(p):
                        st.image(p)
