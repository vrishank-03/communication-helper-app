# ui/admin.py
"""
Admin dashboard - password-protected review queue, session records and
educator talent pool.
"""

import os
import re
import hmac
from datetime import datetime, timedelta

import streamlit as st
from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.config import LIMIT_WINDOW_DAYS, SECURITY_FLAG_REASON, ADMIN_PENDING_PHASES
from core.models import safe_json
from services.review import ReviewError, NOTE_REQUIRED, resolve_session, resolve_report
from ui.conversation import render_conversation

# ── Labels ───────────────────────────────────────────────────

STATUS_LABELS = {
    "CERTIFIED": "Certified",
    "NOT_CERTIFIED": "Not certified",
    "ESCALATED": "Pending review",
    "ESCALATED_PLAGIARISM": "Security hold",
    "REJECTED": "Integrity violation",
    "ERROR": "Processing error",
}
IN_PROGRESS = "In progress"

PILLARS = (
    ("Pedagogy", "pedagogy"),
    ("Technical accuracy", "tech_accuracy"),
    ("Communication", "communication"),
)

# Review queue categories, in priority order
QUEUE_KINDS = {
    "hold": {
        "title": "Security holds",
        "badge": ("Security hold", "red"),
        "help": "A submitted video matches a file from another educator. The session stays locked until resolved.",
        "actions": [
            ("HOLD_CLEARED", "Clear hold and allow resubmission", "secondary"),
            ("VIOLATION_CONFIRMED", "Confirm violation and close session", "primary"),
        ],
    },
    "report": {
        "title": "Educator reports",
        "badge": ("Educator report", "orange"),
        "help": "Educators who have disputed their evaluation result.",
    },
    "review": {
        "title": "Pending reviews",
        "badge": ("Pending review", "blue"),
        "help": "Sessions that did not reach the automatic certification threshold and need a final decision.",
        "actions": [
            ("NOT_CERTIFIED", "Mark as not certified", "secondary"),
            ("CERTIFIED", "Certify educator", "primary"),
        ],
    },
    "error": {
        "title": "Processing errors",
        "badge": ("Processing error", "gray"),
        "help": "The evaluation worker failed. Reopen the session so the educator can resubmit.",
        "actions": [("REOPENED", "Reopen for resubmission", "primary")],
    },
}
_PHASE_TO_KIND = {"ESCALATED_PLAGIARISM": "hold", "ESCALATED": "review", "ERROR": "error"}

ACTION_LABELS = {
    "CERTIFIED": "Certified",
    "NOT_CERTIFIED": "Not certified",
    "HOLD_CLEARED": "Hold cleared",
    "VIOLATION_CONFIRMED": "Violation confirmed",
    "REOPENED": "Reopened for resubmission",
}
ACTION_NOTICES = {
    "CERTIFIED": "Session {sid} certified.",
    "NOT_CERTIFIED": "Session {sid} marked as not certified.",
    "HOLD_CLEARED": "Hold cleared for session {sid}. The educator can now resubmit.",
    "VIOLATION_CONFIRMED": "Violation confirmed. Session {sid} has been closed.",
    "REOPENED": "Session {sid} reopened for resubmission.",
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
_SOURCE_RE = re.compile(r"Educator (\S+) in session (\S+)")


# ── Formatting helpers ───────────────────────────────────────

def _short_id(sid):
    sid = str(sid or "")
    return sid[:8] if _UUID_RE.match(sid) else sid

def _status(phase):
    return STATUS_LABELS.get(phase, IN_PROGRESS)

def _badge(label, color):
    return f":{color}-badge[{label}]"

def _fmt_dt(value):
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %H:%M")
    return str(value)[:16] if value else "-"

def _fmt_iso(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return "-"

def _educator(record):
    ed_id = record.get("educator_id") or "Unknown"
    name = (record.get("educator_name") or "").strip()
    return f"{name} ({ed_id})" if name and name.lower() != ed_id.lower() else ed_id

def _score(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _latest_scores(conv):
    for entry in reversed(conv):
        if entry.get("type") in ("round_decision", "video_eval"):
            scores = entry.get("scores") or {}
            if any(_score(scores.get(f"{key}_score")) is not None for _, key in PILLARS):
                return scores
    return {}

def _last(conv, etype):
    return next((e for e in reversed(conv) if e.get("type") == etype), None)

def _scores_line(conv):
    scores = _latest_scores(conv)
    parts = [
        f"{label} {_score(scores.get(f'{key}_score'))}/10"
        for label, key in PILLARS
        if _score(scores.get(f"{key}_score")) is not None
    ]
    return ", ".join(parts) or "No scores recorded"


# ── Access control ───────────────────────────────────────────

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


# ── Data ─────────────────────────────────────────────────────

def _load_admin_data():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT s.id, s.educator_id, e.name AS educator_name, s.phase, "
                "s.conversation, s.state_data, s.created_at, s.updated_at "
                "FROM evaluation_sessions s "
                "LEFT JOIN educators e ON e.id = s.educator_id "
                "ORDER BY s.created_at DESC"
            )
            sessions = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT f.id, f.session_id, f.educator_id, e.name AS educator_name, "
                "f.reason, f.details, f.screenshot_1, f.screenshot_2, "
                "f.status, f.resolution_note, f.created_at, f.resolved_at "
                "FROM evaluation_flags f "
                "LEFT JOIN educators e ON e.id = f.educator_id "
                "ORDER BY f.created_at DESC"
            )
            flags = [dict(r) for r in cur.fetchall()]

    flags_by_session = {}
    for f in flags:
        flags_by_session.setdefault(f["session_id"], []).append(f)

    for s in sessions:
        conv = safe_json(s.get("conversation"), [])
        s["conversation"] = conv if isinstance(conv, list) else []
        s["state_data"] = safe_json(s.get("state_data"), {}) or {}
        s["flags"] = flags_by_session.get(s["id"], [])

    return sessions, flags


def _session_item(s):
    kind = _PHASE_TO_KIND[s["phase"]]
    if kind == "hold":
        flag = next((f for f in s["flags"] if f["reason"] == SECURITY_FLAG_REASON), None)
        match = _SOURCE_RE.search((flag or {}).get("details") or "")
        stage = "follow-up" if s["state_data"].get("active_question") else "initial"
        summary = (
            f"The {stage} video matches a file submitted by educator {match.group(1)} "
            f"(session {_short_id(match.group(2))})."
            if match else f"The {stage} video matches a file submitted by another educator."
        )
    elif kind == "review":
        summary = _scores_line(s["conversation"])
        decision = _last(s["conversation"], "round_decision") or {}
        if "Fallback AI model" in (decision.get("summary") or ""):
            summary += ". Scored by the fallback model"
    else:
        summary = "Evaluation failed while processing the latest video."

    return {
        "kind": kind,
        "key": f"{kind}_{s['id']}",
        "session": s,
        "record": s,
        "when": f"Started {_fmt_dt(s.get('created_at'))}",
        "summary": summary,
    }


def _build_queue(sessions, flags):
    by_id = {s["id"]: s for s in sessions}
    latest_for_educator = {}
    for s in sessions:  # newest first
        latest_for_educator.setdefault(s.get("educator_id"), s["id"])

    queue = {kind: [] for kind in QUEUE_KINDS}
    for s in sessions:
        if s["phase"] not in ADMIN_PENDING_PHASES:
            continue
        # A failed session is only actionable if the educator has not started a newer one.
        if s["phase"] == "ERROR" and latest_for_educator.get(s.get("educator_id")) != s["id"]:
            continue
        item = _session_item(s)
        queue[item["kind"]].append(item)

    for f in flags:
        if f["status"] != "OPEN" or f["reason"] == SECURITY_FLAG_REASON:
            continue
        queue["report"].append({
            "kind": "report",
            "key": f"report_{f['id']}",
            "flag": f,
            "session": by_id.get(f["session_id"]),
            "record": f,
            "when": f"Reported {_fmt_dt(f.get('created_at'))}",
            "summary": f["reason"],
        })
    return queue


# ── Actions ──────────────────────────────────────────────────

def _apply(fn, notice):
    try:
        fn()
    except ReviewError as exc:
        st.error(str(exc))
        return
    st.session_state["admin_notice"] = notice
    st.rerun()


def _render_resolution(item):
    kind = item["kind"]
    st.markdown("**Resolution**")
    note = st.text_area(
        "Note to educator",
        key=f"note_{item['key']}",
        placeholder="Explain the decision. This note is shown to the educator.",
        height=90,
    )

    if kind == "report":
        if st.button("Mark report as resolved", key=f"act_{item['key']}", type="primary"):
            _apply(lambda: resolve_report(item["flag"]["id"], note), "Report resolved.")
        return

    actions = QUEUE_KINDS[kind]["actions"]
    if any(action in NOTE_REQUIRED for action, _, _ in actions):
        st.caption("A note is required when the outcome is negative for the educator.")

    sid = item["session"]["id"]
    cols = st.columns(len(actions))
    for col, (action, label, btn_type) in zip(cols, actions):
        if col.button(label, key=f"act_{action}_{item['key']}", type=btn_type, width="stretch"):
            _apply(
                lambda a=action: resolve_session(sid, a, note),
                ACTION_NOTICES[action].format(sid=_short_id(sid)),
            )


# ── Session detail ───────────────────────────────────────────

def _render_assessment(conv):
    scores = _latest_scores(conv)
    if not scores:
        st.info("No evaluation scores have been recorded for this session yet.")
    else:
        cols = st.columns(3)
        for col, (label, key) in zip(cols, PILLARS):
            value = _score(scores.get(f"{key}_score"))
            with col:
                st.metric(label, f"{value}/10" if value is not None else "-", border=True)
                if scores.get(f"{key}_reasoning"):
                    st.caption(scores[f"{key}_reasoning"])

    decision = _last(conv, "round_decision")
    if decision and decision.get("summary"):
        st.markdown("**Outcome summary**")
        st.write(decision["summary"])
        if decision.get("improvement_areas"):
            st.markdown("**Improvement areas**")
            st.write(decision["improvement_areas"])

    video = _last(conv, "video_eval")
    if video:
        v = video.get("scores") or {}
        content = str(v.get("content_type") or "-").replace("_", " ").capitalize()
        st.markdown("**Initial video**")
        st.caption(
            f"{v.get('word_count', 0)} words  ·  {v.get('duration_seconds', 0)} seconds  ·  "
            f"{v.get('wpm', 0)} words per minute  ·  Content: {content}"
        )
        if v.get("_auto_gated"):
            st.warning(f"Automatically gated: {v.get('_gate_reason') or 'content did not meet minimum criteria.'}")
        if v.get("_low_confidence"):
            st.warning("Scored by the fallback model. Verify the scores before relying on them.")

    answers = [e for e in conv if e.get("type") == "answer_eval"]
    if answers:
        st.markdown("**Follow-up responses**")
        for a in answers:
            quality = "Satisfactory" if a.get("quality") == "SATISFACTORY" else "Insufficient"
            gated = " (automatically gated)" if a.get("_auto_gated") else ""
            st.markdown(
                f"- Follow-up {a.get('index', '')}: **{quality}**, "
                f"{a.get('score', '-')}/10{gated}. {a.get('assessment', '')}"
            )

    reviews = [e for e in conv if e.get("type") == "admin_review"]
    if reviews:
        st.markdown("**Review history**")
        for r in reviews:
            note = f": {r['note']}" if r.get("note") else ""
            st.markdown(
                f"- {_fmt_iso(r.get('reviewed_at'))}  ·  "
                f"**{ACTION_LABELS.get(r.get('action'), r.get('action'))}**{note}"
            )


def _render_report(flag):
    with st.container(border=True):
        status = "Resolved" if flag.get("status") == "RESOLVED" else "Open"
        st.markdown(f"**{flag.get('reason')}**  \n{flag.get('details') or 'No details provided.'}")
        st.caption(f"{status}  ·  Reported {_fmt_dt(flag.get('created_at'))}")
        if flag.get("resolution_note"):
            st.caption(f"Resolution: {flag['resolution_note']}")
        shots = [p for p in (flag.get("screenshot_1"), flag.get("screenshot_2")) if p and os.path.exists(p)]
        if shots:
            st.image(shots, width=320)


def _render_session_detail(s):
    conv = s["conversation"]
    st.caption(
        f"Session {s['id']}  ·  Started {_fmt_dt(s.get('created_at'))}  ·  "
        f"Status: {_status(s['phase'])}"
    )
    reports = [f for f in s["flags"] if f["reason"] != SECURITY_FLAG_REASON]

    tab_assess, tab_conv, tab_reports = st.tabs(
        ["Assessment", "Conversation", f"Reports ({len(reports)})"]
    )
    with tab_assess:
        _render_assessment(conv)
    with tab_conv:
        if conv:
            render_conversation(conv)
        else:
            st.info("No conversation recorded.")
    with tab_reports:
        if not reports:
            st.caption("The educator has not reported any issues for this session.")
        for f in reports:
            _render_report(f)


@st.dialog("Case review", width="large")
def _review_dialog(item):
    cfg = QUEUE_KINDS[item["kind"]]
    st.markdown(_badge(*cfg["badge"]))
    st.markdown(f"### {_educator(item['record'])}")
    st.caption(cfg["help"])

    if item["kind"] == "hold":
        st.error(item["summary"])
    elif item["kind"] == "report":
        _render_report(item["flag"])

    if item.get("session"):
        _render_session_detail(item["session"])
    else:
        st.info("The session linked to this report could not be found.")

    st.divider()
    _render_resolution(item)


# ── Tabs ─────────────────────────────────────────────────────

def _render_queue(queue):
    if not any(queue.values()):
        st.success("All clear. No sessions are waiting for action.")
        return

    for kind, items in queue.items():
        if not items:
            continue
        cfg = QUEUE_KINDS[kind]
        st.markdown(f"#### {cfg['title']} ({len(items)})")
        st.caption(cfg["help"])
        for item in items:
            session_id = (item.get("session") or {}).get("id") or item["record"].get("session_id")
            with st.container(border=True):
                info, action = st.columns([5, 1], vertical_alignment="center")
                with info:
                    st.markdown(f"{_badge(*cfg['badge'])} **{_educator(item['record'])}**")
                    st.caption(f"Session {_short_id(session_id)}  ·  {item['when']}  ·  {item['summary']}")
                if action.button("Review", key=f"open_{item['key']}", type="primary", width="stretch"):
                    _review_dialog(item)


def _render_sessions(sessions):
    f_status, f_query = st.columns([2, 3])
    status_options = list(dict.fromkeys([*STATUS_LABELS.values(), IN_PROGRESS]))
    picked = f_status.multiselect(
        "Status", status_options, placeholder="All statuses", key="admin_status_filter"
    )
    query = f_query.text_input(
        "Search", placeholder="Educator ID, name or session ID", key="admin_name_filter"
    ).strip().lower()

    view = [
        s for s in sessions
        if (not picked or _status(s["phase"]) in picked)
        and (not query or query in f"{s.get('educator_id')} {s.get('educator_name')} {s['id']}".lower())
    ]
    if not view:
        st.info("No sessions match these filters.")
        return

    rows = []
    for s in view:
        scores = _latest_scores(s["conversation"])
        rows.append({
            "Started": s.get("created_at"),
            "Educator": s.get("educator_id") or "Unknown",
            "Name": s.get("educator_name") or "",
            "Status": _status(s["phase"]),
            "Pedagogy": _score(scores.get("pedagogy_score")),
            "Technical": _score(scores.get("tech_accuracy_score")),
            "Communication": _score(scores.get("communication_score")),
            "Videos": sum(1 for e in s["conversation"] if e.get("type") == "answer"),
            "Session": _short_id(s["id"]),
        })

    score_col = st.column_config.NumberColumn(format="%d/10")
    event = st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        # New key per filter combination so a stale row selection never points at a different session
        key=f"admin_sessions_{abs(hash((tuple(picked), query)))}",
        column_config={
            "Started": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
            "Pedagogy": score_col,
            "Technical": score_col,
            "Communication": score_col,
        },
    )

    selected = event.selection.rows
    if not selected or selected[0] >= len(view):
        st.caption("Select a row to view the full session.")
        return

    s = view[selected[0]]
    with st.container(border=True):
        head, action = st.columns([4, 1], vertical_alignment="center")
        head.markdown(f"#### {_educator(s)}")
        if s["phase"] in ADMIN_PENDING_PHASES:
            if action.button("Open case review", key=f"open_table_{s['id']}", type="primary", width="stretch"):
                _review_dialog(_session_item(s))
        _render_session_detail(s)


_POOL_GROUPS = (
    ("Master presenters", "Communication 8 or above, no pillar below 5."),
    ("Subject experts", "Technical accuracy 8 or above, no pillar below 5."),
    ("Engaging teachers", "Pedagogy 8 or above, no pillar below 5."),
    ("Needs coaching", "At least one pillar below 5."),
)

def _render_talent_pool(sessions):
    st.caption("Based on each educator's most recent completed evaluation.")

    latest = {}
    for s in sessions:  # newest first
        ed = s.get("educator_id")
        if not ed or ed in latest or s["phase"] not in ("CERTIFIED", "ESCALATED", "NOT_CERTIFIED"):
            continue
        scores = _latest_scores(s["conversation"])
        if scores:
            latest[ed] = (s, scores)

    groups = {title: [] for title, _ in _POOL_GROUPS}
    rows = []
    for s, scores in latest.values():
        p = _score(scores.get("pedagogy_score")) or 0
        t = _score(scores.get("tech_accuracy_score")) or 0
        c = _score(scores.get("communication_score")) or 0
        if min(p, t, c) < 5:
            groups["Needs coaching"].append(s)
        else:
            if c >= 8:
                groups["Master presenters"].append(s)
            if t >= 8:
                groups["Subject experts"].append(s)
            if p >= 8:
                groups["Engaging teachers"].append(s)
        rows.append({
            "Educator": s.get("educator_id"),
            "Name": s.get("educator_name") or "",
            "Pedagogy": p, "Technical": t, "Communication": c,
            "Latest outcome": _status(s["phase"]),
            "Assessed": s.get("created_at"),
        })

    cols = st.columns(len(_POOL_GROUPS))
    for col, (title, desc) in zip(cols, _POOL_GROUPS):
        with col.container(border=True):
            members = groups[title]
            st.metric(title, len(members))
            st.caption(desc)
            if members:
                st.markdown("\n".join(f"- {_educator(m)}" for m in members))

    if rows:
        score_col = st.column_config.NumberColumn(format="%d/10")
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Assessed": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
                "Pedagogy": score_col,
                "Technical": score_col,
                "Communication": score_col,
            },
        )
    else:
        st.info("No completed evaluations yet.")


# ── Entry point ──────────────────────────────────────────────

def _render_login():
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        with st.form("admin_login"):
            st.markdown("#### Administrator access")
            st.caption("Enter the admin password to open the dashboard.")
            st.text_input("Password", type="password", key="admin_pw_input")
            st.form_submit_button("Unlock", on_click=_try_admin_unlock, type="primary", width="stretch")
        if st.session_state.get("admin_pw_failed"):
            st.error("Incorrect password.")


def render_admin_dashboard():
    if not _admin_password():
        st.warning("The admin dashboard is disabled until an ADMIN_PASSWORD is set.")
        return

    if not st.session_state.get("admin_ok"):
        _render_login()
        return

    notice = st.session_state.pop("admin_notice", None)
    if notice:
        st.toast(notice)

    sessions, flags = _load_admin_data()
    queue = _build_queue(sessions, flags)
    pending = sum(len(items) for items in queue.values())

    head, lock = st.columns([5, 1], vertical_alignment="center")
    with head:
        st.markdown("## Admin Dashboard")
        st.caption("Review flagged sessions, record decisions and track educator performance.")
    lock.button("Lock dashboard", key="admin_lock_btn", on_click=_lock_admin, width="stretch")

    week_ago = datetime.now() - timedelta(days=LIMIT_WINDOW_DAYS)
    statuses = [_status(s["phase"]) for s in sessions]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total sessions", len(sessions), border=True)
    m2.metric(
        f"Last {LIMIT_WINDOW_DAYS} days",
        sum(1 for s in sessions if isinstance(s.get("created_at"), datetime) and s["created_at"] >= week_ago),
        border=True,
    )
    m3.metric("Certified", statuses.count("Certified"), border=True)
    m4.metric("Not certified", statuses.count("Not certified"), border=True)
    m5.metric("Awaiting action", pending, border=True)

    tab_queue, tab_sessions, tab_pool = st.tabs(["Action queue", "Sessions", "Talent pool"])
    with tab_queue:
        _render_queue(queue)
    with tab_sessions:
        _render_sessions(sessions)
    with tab_pool:
        _render_talent_pool(sessions)