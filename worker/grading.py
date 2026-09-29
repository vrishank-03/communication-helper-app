"""
Content-gate helpers, auto-fail factories, and conversation context builder.
"""

from core.config import (
    FALLBACK_QUESTION,
    INVALID_CONTENT_TYPES,
    MIN_WORDS_INITIAL,
    MIN_WORDS_FOLLOWUP,
)


# ══════════════════════════════════════════════════════════════
# Content Gate
# ══════════════════════════════════════════════════════════════

def should_gate(step1: dict, min_words: int) -> bool:
    """Return True if the transcription result should be auto-failed."""
    transcript = (step1.get("transcript") or "").strip()
    word_count = int(step1.get("word_count") or 0)
    content_type = step1.get("content_type", "OTHER_UNRELATED")

    return (
        not transcript
        or word_count < min_words
        or content_type in INVALID_CONTENT_TYPES
    )


def gate_reason(step1: dict, min_words: int) -> str:
    """Precise audit-trail reason (admin dashboard only)."""
    transcript = (step1.get("transcript") or "").strip()
    word_count = int(step1.get("word_count") or 0)
    content_type = step1.get("content_type", "OTHER_UNRELATED")

    if not transcript:
        return "No sufficiently intelligible speech was detected."
    if word_count < min_words:
        return (
            f"Only {word_count} words detected. "
            f"At least {min_words} words required."
        )
    reasons = {
        "PROMOTIONAL_OR_SCRIPTED":
            "Submission appears promotional/scripted rather than a classroom response.",
        "SILENT_OR_INAUDIBLE":
            "No usable speech audio detected.",
        "OTHER_UNRELATED":
            "Spoken content does not address the educational scenario.",
    }
    return reasons.get(content_type, "Content did not meet minimum criteria.")


# ══════════════════════════════════════════════════════════════
# Auto-fail factories
# ══════════════════════════════════════════════════════════════

def auto_fail_video(step1: dict) -> dict:
    """Build a full video-eval result dict for a gated initial video."""
    reason = gate_reason(step1, MIN_WORDS_INITIAL)
    duration = float(step1.get("duration_seconds") or 0)
    word_count = int(step1.get("word_count") or 0)
    wpm = round((word_count / duration) * 60) if duration > 0 else 0

    return {
        "transcript": step1.get("transcript", ""),
        "word_count": word_count,
        "duration_seconds": round(duration),
        "wpm": wpm,
        "content_type": step1.get("content_type", "OTHER_UNRELATED"),
        "filler_count": 0,
        "filler_details": "",
        "pacing_verdict": "Not evaluated.",
        "tone_verdict": "Not evaluated.",
        "tone_evidence": reason,
        "pedagogy_score": 1,
        "tech_accuracy_score": 1,
        "communication_score": 1,
        "strengths": "No valid evaluation possible.",
        "weaknesses": reason,
        "actionable_fixes": [
            "Re-record the response with clear audio.",
            "Address the scenario directly.",
        ],
        "phrase_original": "",
        "phrase_upgrade": "",
        "next_question": FALLBACK_QUESTION,
        "decision": "FOLLOW_UP",
        "_used_model": None,
        "_low_confidence": False,
        "_auto_gated": True,
        "_gate_reason": reason,
    }


def auto_fail_answer(step1: dict) -> dict:
    """Build a follow-up answer-eval result dict for a gated submission."""
    reason = gate_reason(step1, MIN_WORDS_FOLLOWUP)
    return {
        "transcript": step1.get("transcript", ""),
        "word_count": int(step1.get("word_count") or 0),
        "addresses_question": False,
        "quality": "INSUFFICIENT",
        "score": 1,
        "assessment": reason,
        "key_insight":
            "Provide a clear, direct response so your reasoning can be evaluated.",
        "model_answer":
            "Answer the question directly first, then explain the practical "
            "classroom action you would take.",
        "_used_model": None,
        "_low_confidence": False,
        "_auto_gated": True,
        "_gate_reason": reason,
    }


# ══════════════════════════════════════════════════════════════
# Context builder (for follow-up and round-decision prompts)
# ══════════════════════════════════════════════════════════════

def build_context(conversation: list) -> str:
    """Serialize the conversation into a text block for Gemini."""
    parts = []

    for entry in conversation:
        etype = entry.get("type")

        if etype == "scenario":
            parts.append(f"SCENARIO:\n{entry.get('content', '')}")

        elif etype == "video_eval":
            scores = entry.get("scores") or {}
            block = (
                f"VIDEO EVALUATION (Round {entry.get('round')}):\n"
                f"Pedagogy: {scores.get('pedagogy_score', 'N/A')}/10\n"
                f"Technical Accuracy: {scores.get('tech_accuracy_score', 'N/A')}/10\n"
                f"Communication: {scores.get('communication_score', 'N/A')}/10\n"
                f"Strengths: {scores.get('strengths', '')}\n"
                f"Weaknesses: {scores.get('weaknesses', '')}\n"
            )
            if scores.get("actionable_fixes"):
                block += "Actionable Fixes: " + "; ".join(scores["actionable_fixes"]) + "\n"
            if scores.get("transcript"):
                block += f'Transcript:\n"{scores["transcript"]}"'
            parts.append(block)

        elif etype == "question":
            parts.append(
                f"QUESTION Q{entry.get('index')} (Round {entry.get('round')}):\n"
                f"{entry.get('content', '')}"
            )

        elif etype == "answer_eval":
            block = (
                f"ANSWER EVALUATION Q{entry.get('index')} "
                f"(Round {entry.get('round')}):\n"
                f"Quality: {entry.get('quality', 'N/A')}\n"
                f"Score: {entry.get('score', 'N/A')}/10\n"
                f"Addresses Question: {entry.get('addresses_question', 'N/A')}\n"
                f"Assessment: {entry.get('assessment', '')}\n"
                f"Key Insight: {entry.get('key_insight', '')}\n"
            )
            if entry.get("model_answer"):
                block += f"Model Answer:\n{entry['model_answer']}\n"
            if entry.get("transcript"):
                block += f'Transcript:\n"{entry["transcript"]}"'
            parts.append(block)

        elif etype == "round_decision":
            scores = entry.get("scores") or {}
            parts.append(
                f"ROUND {entry.get('round')} DECISION: {entry.get('decision')}\n"
                f"Summary: {entry.get('summary', '')}\n"
                f"Pedagogy: {scores.get('pedagogy_score', 'N/A')}/10\n"
                f"Technical Accuracy: {scores.get('tech_accuracy_score', 'N/A')}/10\n"
                f"Communication: {scores.get('communication_score', 'N/A')}/10"
            )

    return "\n\n".join(parts)
