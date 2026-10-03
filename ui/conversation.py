"""
Render the evaluation conversation as structured UI cards.

This is what the EDUCATOR sees — no internal scores, no audit metadata.
"""

import streamlit as st


def render_conversation(conversation=None):
    """Render the full conversation as educator-facing coaching cards."""
    entries = conversation if conversation is not None else st.session_state.get("conversation", [])

    for entry in entries:
        t = entry.get("type")

        if t == "scenario":
            st.markdown("### Practical Scenario")
            st.info(entry.get("content"))

        elif t == "video_eval":
            s = entry.get("scores", {})
            st.markdown("### Initial Delivery Feedback")
            with st.container():
                st.markdown(
                    f"**Communication Approach:** "
                    f"{s.get('tone_verdict', 'Professional')}"
                )
                col_s, col_w = st.columns(2)
                with col_s:
                    st.success(
                        f"**Core Strengths:**\n"
                        f"{s.get('strengths', 'Clear communication observed.')}"
                    )
                with col_w:
                    st.warning(
                        f"**Areas for Growth:**\n"
                        f"{s.get('weaknesses', 'Review prompt criteria.')}"
                    )
                if s.get("actionable_fixes"):
                    st.markdown("**Recommended Adjustments:**")
                    for i, fix in enumerate(s["actionable_fixes"], 1):
                        st.markdown(f"{i}. {fix}")
                if s.get("phrase_original") and s.get("phrase_upgrade"):
                    st.markdown("**Language Enhancement:**")
                    st.markdown(
                        f"Instead of: _{s['phrase_original']}_\n\n"
                        f"Try: **{s['phrase_upgrade']}**"
                    )
            if s.get("transcript"):
                with st.expander("View Transcript"):
                    st.text(s["transcript"])
            st.markdown("---")

        elif t == "question":
            st.markdown(
                f"<div class='q-card'>"
                f"<strong>Follow-up Demonstration:</strong> <br>"
                f"{entry.get('content')}</div>",
                unsafe_allow_html=True,
            )

        elif t == "answer":
            st.markdown(
                "<div class='a-card'>"
                "<em>Response submitted for evaluation.</em>"
                "</div>",
                unsafe_allow_html=True,
            )

        elif t == "answer_eval":
            is_pass = entry.get("quality") == "SATISFACTORY"
            css = "eval-pass" if is_pass else "eval-fail"
            label = "Effective Approach" if is_pass else "Needs Refinement"

            st.markdown(
                f"<div class='{css}'>"
                f"<strong>[{label}]</strong> "
                f"{entry.get('assessment', '')}<br><br>"
                f"<strong>Key Takeaway:</strong> "
                f"<em>{entry.get('key_insight', '')}</em>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if not is_pass and entry.get("model_answer"):
                st.markdown(
                    f"<div class='model-ans'>"
                    f"<strong>Recommended Classroom Response:</strong><br>"
                    f"{entry['model_answer']}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            if entry.get("transcript"):
                with st.expander("View Transcript"):
                    st.text(entry["transcript"])

        elif t == "round_decision":
            dec = entry.get("decision")
            if dec == "CERTIFIED":
                st.success(
                    f"**Evaluation Status: Certified.** "
                    f"{entry.get('summary', '')}"
                )
            else:
                st.info(
                    f"**Evaluation Status: Under Review.** "
                    f"{entry.get('summary', '')}"
                )
            st.markdown("---")

        elif t == "admin_review":
            title, box = _REVIEW_MESSAGES.get(
                entry.get("action"), ("Reviewer update.", st.info)
            )
            note = (entry.get("note") or "").strip()
            box(f"**{title}**" + (f"\n\nReviewer note: {note}" if note else ""))


_REVIEW_MESSAGES = {
    "CERTIFIED": (
        "Reviewer decision: Certified. A Master Trainer has reviewed your session and confirmed certification.",
        st.success,
    ),
    "NOT_CERTIFIED": (
        "Reviewer decision: Not certified. A Master Trainer has reviewed your session.",
        st.warning,
    ),
    "HOLD_CLEARED": (
        "Review complete. The hold on your session has been lifted. Please record and submit an original response to continue.",
        st.info,
    ),
    "VIOLATION_CONFIRMED": (
        "This session was closed following a submission integrity review.",
        st.error,
    ),
    "REOPENED": (
        "Your session has been reopened. Please submit your response again.",
        st.info,
    ),
}
