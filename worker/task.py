# worker/task.py
"""
The main Celery task: process_video_submission.

This module is intentionally self-contained in its DB access (doesn't
go through services/session.py) because it runs in a Celery worker
process, not inside Streamlit.
"""

import os
import json
import time
import traceback
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.config import (
    FAILSAFE_MODEL, FALLBACK_QUESTION,
    MIN_WORDS_INITIAL, MIN_WORDS_FOLLOWUP,
    MAX_TOTAL_VIDEOS, ESCALATION_STREAK,
    logger
)
from core.models import safe_json

from worker.celery_app import celery_app, gemini_client, call_model
from worker.prompts import (
    GUARDRAILS,
    TRANSCRIBE_PROMPT, TRANSCRIBE_SCHEMA,
    VIDEO_GRADE_PROMPT, VIDEO_GRADE_SCHEMA,
    ANSWER_GRADE_PROMPT, ANSWER_GRADE_SCHEMA,
    ROUND_DECISION_PROMPT, ROUND_DECISION_SCHEMA,
)
from worker.grading import (
    should_gate, auto_fail_video, auto_fail_answer, build_context,
)


# ── DB helpers (worker-side) ─────────────────────────────────

def _db_read(session_id):
    logger.debug(f"worker.task._db_read - Fetching session {session_id}")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, scenario, phase, round_number, "
                "conversation, state_data, updated_at "
                "FROM evaluation_sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Session {session_id} not found.")
            return dict(row)


def _db_write(session_id, phase, round_number, conversation, state_data):
    logger.debug(f"worker.task._db_write - Updating session {session_id} to phase {phase}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE evaluation_sessions "
                "SET phase = %s, round_number = %s, "
                "    conversation = %s::jsonb, "
                "    state_data = %s::jsonb, "
                "    updated_at = %s "
                "WHERE id = %s",
                (
                    phase, round_number,
                    json.dumps(conversation, default=str),
                    json.dumps(state_data, default=str),
                    datetime.now(timezone.utc).isoformat(),
                    session_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Failed to update session {session_id}.")
        conn.commit()


def _db_error(session_id):
    logger.error(f"worker.task._db_error - Setting session {session_id} to ERROR state")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE evaluation_sessions "
                "SET phase = 'ERROR', updated_at = %s WHERE id = %s",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
        conn.commit()


# ── Main task ────────────────────────────────────────────────

# THE FIX: Added name="worker.task.process_video_submission" to perfectly match Streamlit's call
@celery_app.task(bind=True, name="worker.task.process_video_submission", autoretry_for=(), max_retries=0)
def process_video_submission(self, session_id, file_path, task_type="INITIAL"):
    """Background AI evaluation of an educator's video submission."""
    sid = str(session_id)[:8]
    logger.info(f"[{sid}] 🚀 {task_type} JOB STARTED - File: {file_path}")
    remote = None

    try:
        # ── Validate ─────────────────────────────────────────
        if task_type not in {"INITIAL", "FOLLOW_UP"}:
            raise ValueError(f"Unknown task_type: {task_type}")

        # ── Load session ─────────────────────────────────────
        row = _db_read(session_id)
        scenario = row.get("scenario", "")
        conversation = safe_json(row.get("conversation"), [])
        if not isinstance(conversation, list):
            conversation = []
        round_number = int(row.get("round_number") or 1)
        state_data = safe_json(row.get("state_data"), {})

        active_question = state_data.get("active_question")
        active_q_index = int(state_data.get("active_q_index", 0) or 0)
        consecutive_insufficient = int(
            state_data.get("consecutive_insufficient", 0) or 0
        )
        total_videos_submitted = int(
            state_data.get("total_videos_submitted", 0) or 0
        )

        if task_type == "FOLLOW_UP" and not active_question:
            raise RuntimeError(
                "FOLLOW_UP submission received without an active question."
            )

        # ── Upload video to Google ───────────────────────────
        logger.info(f"[{sid}] ⏳ Uploading video to Gemini...")
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"Video file does not exist: {file_path}")

        remote = gemini_client.files.upload(file=file_path)
        while remote.state.name == "PROCESSING":
            time.sleep(3)
            remote = gemini_client.files.get(name=remote.name)
        if remote.state.name == "FAILED":
            raise RuntimeError("Video rejected by Google API.")
        logger.info(f"[{sid}] ✓ Upload complete.")

        # ── Transcribe ───────────────────────────────────────
        logger.info(f"[{sid}] 🧠 Transcribing...")
        step1, transcription_model = call_model(
            [remote, TRANSCRIBE_PROMPT.format(guardrails=GUARDRAILS)],
            TRANSCRIBE_SCHEMA,
        )

        transcript = (step1.get("transcript") or "").strip()
        word_count = int(step1.get("word_count") or 0)
        duration = float(step1.get("duration_seconds") or 0)
        content_type = step1.get("content_type", "OTHER_UNRELATED")
        wpm = round((word_count / duration) * 60) if duration > 0 else 0
        total_videos_submitted += 1

        logger.info(
            f"[{sid}] ✓ {word_count} words | {round(duration)}s "
            f"| {wpm} WPM | {content_type}"
        )

        # ═════════════════════════════════════════════════════
        # INITIAL VIDEO
        # ═════════════════════════════════════════════════════
        if task_type == "INITIAL":
            if should_gate(step1, MIN_WORDS_INITIAL):
                logger.warning(f"[{sid}] ⚠ Content gate → auto-gated")
                result = auto_fail_video(step1)
                consecutive_insufficient += 1
            else:
                logger.info(f"[{sid}] ⚖️ Grading initial video...")
                grade_prompt = VIDEO_GRADE_PROMPT.format(
                    guardrails=GUARDRAILS,
                    scenario=scenario,
                    transcript=transcript,
                    word_count=word_count,
                    duration=round(duration),
                    wpm=wpm,
                )
                result, used_model = call_model(
                    [grade_prompt], VIDEO_GRADE_SCHEMA,
                )
                result.update({
                    "transcript": transcript,
                    "word_count": word_count,
                    "duration_seconds": round(duration),
                    "wpm": wpm,
                    "content_type": content_type,
                    "_used_model": used_model,
                    "_low_confidence": used_model == FAILSAFE_MODEL,
                    "_auto_gated": False,
                    "_gate_reason": None,
                    "_transcription_model": transcription_model,
                })
                consecutive_insufficient = 0

            # HARD RULE: initial video never certifies
            result["decision"] = "FOLLOW_UP"
            logger.info(
                f"[{sid}] Initial scores: "
                f"P={result.get('pedagogy_score')} "
                f"T={result.get('tech_accuracy_score')} "
                f"C={result.get('communication_score')} → FOLLOW_UP"
            )

            conversation.append({
                "type": "video_eval",
                "round": round_number,
                "scores": result,
            })

            # Queue first follow-up question
            next_q = (result.get("next_question") or "").strip()
            if not next_q:
                next_q = FALLBACK_QUESTION

            active_question = next_q
            active_q_index = 1
            conversation.append({
                "type": "question",
                "round": round_number,
                "index": active_q_index,
                "content": active_question,
            })
            phase = "FOLLOW_UP"

        # ═════════════════════════════════════════════════════
        # FOLLOW-UP VIDEO
        # ═════════════════════════════════════════════════════
        else:
            if should_gate(step1, MIN_WORDS_FOLLOWUP):
                logger.warning(f"[{sid}] ⚠ Content gate → auto-gated")
                answer_eval = auto_fail_answer(step1)
                consecutive_insufficient += 1
            else:
                logger.info(f"[{sid}] ⚖️ Grading follow-up answer...")
                context = build_context(conversation)
                answer_prompt = ANSWER_GRADE_PROMPT.format(
                    guardrails=GUARDRAILS,
                    context=context,
                    question=active_question,
                    transcript=transcript,
                    word_count=word_count,
                )
                answer_eval, used_model = call_model(
                    [answer_prompt], ANSWER_GRADE_SCHEMA,
                )
                answer_eval.update({
                    "transcript": transcript,
                    "word_count": word_count,
                    "_used_model": used_model,
                    "_low_confidence": used_model == FAILSAFE_MODEL,
                    "_auto_gated": False,
                    "_gate_reason": None,
                })
                if answer_eval["quality"] == "INSUFFICIENT":
                    consecutive_insufficient += 1
                else:
                    consecutive_insufficient = 0

            logger.info(
                f"[{sid}] Answer result: "
                f"{answer_eval['quality']} ({answer_eval['score']}/10)"
            )

            conversation.append({
                "type": "answer_eval",
                "round": round_number,
                "index": active_q_index,
                "transcript": answer_eval.get("transcript", ""),
                "word_count": answer_eval.get("word_count", 0),
                "addresses_question": answer_eval.get(
                    "addresses_question", False
                ),
                "quality": answer_eval["quality"],
                "assessment": answer_eval["assessment"],
                "key_insight": answer_eval.get("key_insight", ""),
                "model_answer": answer_eval.get("model_answer", ""),
                "score": answer_eval.get("score", 0),
                "_used_model": answer_eval.get("_used_model"),
                "_low_confidence": answer_eval.get("_low_confidence", False),
                "_auto_gated": answer_eval.get("_auto_gated", False),
                "_gate_reason": answer_eval.get("_gate_reason"),
            })

            # ── Safety nets ──────────────────────────────────
            if (
                consecutive_insufficient >= ESCALATION_STREAK
                or total_videos_submitted >= MAX_TOTAL_VIDEOS
            ):
                summary = (
                    "Escalated for human review due to "
                    "consecutive insufficient responses or video limits."
                )
                conversation.append({
                    "type": "round_decision",
                    "round": round_number,
                    "decision": "ESCALATED",
                    "summary": summary,
                    "scores": {},
                })
                phase = "ESCALATED"
                active_question = None

            # ── Round decision ───────────────────────────────
            else:
                logger.info(f"[{sid}] ⚖️ Making round decision...")
                context = build_context(conversation)
                decision_prompt = ROUND_DECISION_PROMPT.format(
                    guardrails=GUARDRAILS,
                    round_number=round_number,
                    context=context,
                )
                decision, decision_model = call_model(
                    [decision_prompt], ROUND_DECISION_SCHEMA,
                )

                low_confidence = decision_model == FAILSAFE_MODEL
                round_scores = {
                    "pedagogy_score": decision["pedagogy_score"],
                    "tech_accuracy_score": decision["tech_accuracy_score"],
                    "communication_score": decision["communication_score"],
                }
                model_decision = decision["decision"]

                if model_decision == "CERTIFIED" and not low_confidence:
                    conversation.append({
                        "type": "round_decision",
                        "round": round_number,
                        "decision": "CERTIFIED",
                        "summary": decision["round_summary"],
                        "improvement_areas": decision.get(
                            "improvement_areas", ""
                        ),
                        "scores": round_scores,
                        "_used_model": decision_model,
                    })
                    phase = "CERTIFIED"
                    logger.info(f"[{sid}] 🏆 CERTIFIED")

                elif model_decision == "CERTIFIED" and low_confidence:
                    summary = (
                        decision["round_summary"]
                        + " (Fallback model — forwarded for confirmation.)"
                    )
                    conversation.append({
                        "type": "round_decision",
                        "round": round_number,
                        "decision": "ESCALATED",
                        "summary": summary,
                        "improvement_areas": decision.get(
                            "improvement_areas", ""
                        ),
                        "scores": round_scores,
                        "_used_model": decision_model,
                    })
                    phase = "ESCALATED"
                    logger.warning(f"[{sid}] ⚠ Fallback certification → ESCALATED")

                else:
                    conversation.append({
                        "type": "round_decision",
                        "round": round_number,
                        "decision": "ESCALATED",
                        "summary": decision["round_summary"],
                        "improvement_areas": decision.get(
                            "improvement_areas", ""
                        ),
                        "scores": round_scores,
                        "_used_model": decision_model,
                    })
                    phase = "ESCALATED"
                    logger.warning(f"[{sid}] ⚠ Escalated by model decision.")

                active_question = None

        # ── Persist ──────────────────────────────────────────
        final_state = {
            "active_question": active_question,
            "active_q_index": active_q_index,
            "consecutive_insufficient": consecutive_insufficient,
            "total_videos_submitted": total_videos_submitted,
        }
        _db_write(
            session_id, phase, round_number, conversation, final_state,
        )
        logger.info(f"[{sid}] ✅ JOB SUCCESS → {phase}")

        return {
            "session_id": session_id,
            "phase": phase,
            "round_number": round_number,
            "total_videos_submitted": total_videos_submitted,
        }

    except Exception as exc:
        logger.error(f"[{sid}] ❌ CRITICAL ERROR: {exc}")
        traceback.print_exc()
        try:
            _db_error(session_id)
        except Exception as db_err:
            logger.error(f"[{sid}] ❌ Could not write ERROR state: {db_err}")
        raise

    finally:
        # Cleanup local + remote files
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            logger.info(f"[{sid}] 🧹 Local video deleted.")
        except Exception as e:
            logger.warning(f"[{sid}] ⚠ Could not delete local video: {e}")
        try:
            if remote:
                gemini_client.files.delete(name=remote.name)
            logger.info(f"[{sid}] 🧹 Remote Google file deleted.")
        except Exception as e:
            logger.warning(f"[{sid}] ⚠ Could not delete remote file: {e}")
        logger.info(f"[{sid}] 🧹 Cleanup complete.\n")