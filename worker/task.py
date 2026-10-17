# worker/task.py
"""
The main Celery task: process_video_submission.

This module is intentionally self-contained in its DB access (doesn't
go through services/session.py) because it runs in a Celery worker
process, not inside Streamlit.

Failure model
-------------
* ContentBlocked          -> session escalated for conduct (video kept)
* UpstreamUnavailable     -> session set to ERROR, UI can retry (video kept)
* SoftTimeLimitExceeded   -> session set to ERROR (video kept)
* ModelOutputError        -> session set to ERROR (video kept)
* anything else           -> session set to ERROR (video kept), then re-raised

Every ERROR records *why* in state_data["last_error"] so the UI and the
admin dashboard can show a reason and an attempt count.
"""

import os
import json
import time
import shutil
import traceback
from datetime import datetime, timezone

from celery.exceptions import SoftTimeLimitExceeded
from psycopg2.extras import RealDictCursor

from core.database import get_conn
from core.config import (
    FAILSAFE_MODEL, FALLBACK_QUESTION,
    MIN_WORDS_INITIAL, MIN_WORDS_FOLLOWUP,
    MAX_TOTAL_VIDEOS, ESCALATION_STREAK,
    RETAINED_VIDEO_DIR, logger
)
from core.models import safe_json

from worker.celery_app import (
    celery_app, gemini_client, call_model,
    UpstreamUnavailable, ContentBlocked
)
from worker.prompts import (
    GUARDRAILS, CONDUCT_RUBRIC,
    TRANSCRIBE_PROMPT, TRANSCRIBE_SCHEMA,
    MODERATION_PROMPT, MODERATION_SCHEMA,
    VIDEO_GRADE_PROMPT, VIDEO_GRADE_SCHEMA,
    ANSWER_GRADE_PROMPT, ANSWER_GRADE_SCHEMA,
    ROUND_DECISION_PROMPT, ROUND_DECISION_SCHEMA,
)
from worker.grading import (
    should_gate, auto_fail_video, auto_fail_answer, build_context,
)


# ── Tunables (env-overridable; move into core.config if you prefer) ──

# Max time to wait for Google to finish processing an uploaded video.
UPLOAD_TIMEOUT_S = int(os.environ.get("UPLOAD_TIMEOUT_S", "180"))
UPLOAD_POLL_S = 3

# Max wall-clock time for one whole job. Checked between stages, so it also
# works under `--pool=solo` where Celery's own time limits are not enforced.
JOB_DEADLINE_S = int(os.environ.get("JOB_DEADLINE_S", "900"))

# Run the conduct screen on EVERY transcript, not only when the transcriber
# labelled it INAPPROPRIATE_OR_OFFENSIVE. Costs one extra model call per video.
MODERATE_ALL_TRANSCRIPTS = os.environ.get("MODERATE_ALL_TRANSCRIPTS", "1") == "1"

# If the job ends in one of these phases the video stays on disk.
RETAIN_PHASES = {"ERROR", "ESCALATED_CONDUCT", "ESCALATED_PLAGIARISM"}

# Session is finished in these phases; a late or duplicate task must not touch it.
TERMINAL_PHASES = {"CERTIFIED", "ESCALATED", "ESCALATED_CONDUCT", "ESCALATED_PLAGIARISM"}

CONDUCT_SEVERITIES = {"MEDIUM", "HIGH", "CRITICAL"}

MASTERY_SCORE = 8      # every pillar must reach this to certify
CRITICAL_SCORE = 4     # any pillar at or below this is a critical failure


class ModelOutputError(RuntimeError):
    """A model response was missing or had an unusable required field."""


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
                "    updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (
                    phase, round_number,
                    json.dumps(conversation, default=str),
                    json.dumps(state_data, default=str),
                    session_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Failed to update session {session_id}.")
        conn.commit()


def _db_error(session_id, reason="internal_error", detail="", task_type=None):
    """
    Set the session to ERROR and record why in state_data["last_error"]:
    {reason, detail, task_type, at, attempt}. `attempt` counts consecutive
    failures; a successful job rewrites state_data and so clears it.
    If recording the reason fails for any reason, we still set ERROR.
    """
    logger.error(f"worker.task._db_error - Setting session {session_id} to ERROR ({reason})")
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT state_data FROM evaluation_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                state = safe_json(row["state_data"], {}) if row else {}
                if not isinstance(state, dict):
                    state = {}
                prev = state.get("last_error")
                prev_attempt = prev.get("attempt", 0) if isinstance(prev, dict) else 0
                try:
                    attempt = int(prev_attempt or 0) + 1
                except (TypeError, ValueError):
                    attempt = 1
                state["last_error"] = {
                    "reason": reason,
                    "detail": str(detail)[:300],
                    "task_type": task_type,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "attempt": attempt,
                }
                cur.execute(
                    "UPDATE evaluation_sessions "
                    "SET phase = 'ERROR', state_data = %s::jsonb, "
                    "    updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (json.dumps(state, default=str), session_id),
                )
            conn.commit()
        return
    except Exception as exc:
        logger.warning(f"worker.task._db_error - could not record reason ({exc}); falling back")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE evaluation_sessions "
                "SET phase = 'ERROR', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (session_id,),
            )
        conn.commit()


def _fail(session_id, reason, exc, task_type, sid):
    """Mark the session ERROR (never raises) and build the task's return value."""
    try:
        _db_error(session_id, reason, exc, task_type)
    except Exception as db_err:
        logger.error(f"[{sid}] Could not write ERROR state: {db_err}")
    return {"session_id": session_id, "phase": "ERROR", "reason": reason}


# ── Validation / decision helpers ────────────────────────────

def _require(d, key, where):
    """Return d[key]; raise ModelOutputError if d isn't a dict or the key is missing/None."""
    if not isinstance(d, dict) or d.get(key) is None:
        raise ModelOutputError(f"{where}: model output is missing '{key}'")
    return d[key]


def _score(d, key, where):
    """Required 0-10 integer score. A missing score is an error, not a zero."""
    raw = _require(d, key, where)
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        raise ModelOutputError(f"{where}: '{key}' is not numeric ({raw!r})")
    return max(0, min(10, value))


def _already_processed(conversation, task_type, round_number, active_q_index):
    """True if this round's result for this task type is already in the conversation."""
    for entry in conversation:
        if not isinstance(entry, dict) or entry.get("round") != round_number:
            continue
        if task_type == "INITIAL" and entry.get("type") == "video_eval":
            return True
        if (task_type == "FOLLOW_UP" and entry.get("type") == "answer_eval"
                and entry.get("index") == active_q_index):
            return True
    return False


def _any_low_confidence(conversation):
    """True if any earlier grading step in this session ran on the failsafe model."""
    for entry in conversation:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "video_eval":
            scores = entry.get("scores")
            if isinstance(scores, dict) and scores.get("_low_confidence"):
                return True
        elif entry.get("type") == "answer_eval" and entry.get("_low_confidence"):
            return True
    return False


def _decide(p, t, c, low_confidence):
    """Deterministic round decision from the three pillar scores."""
    if p >= MASTERY_SCORE and t >= MASTERY_SCORE and c >= MASTERY_SCORE:
        if low_confidence:
            return "ESCALATED", "Fallback AI model used. Master Trainer verification required to confirm scores."
        return "CERTIFIED", ""
    if p <= CRITICAL_SCORE or t <= CRITICAL_SCORE or c <= CRITICAL_SCORE:
        return "ESCALATED", f"Critical failure in one or more core pillars [P:{p}, T:{t}, C:{c}]. Requires 1-on-1 coaching."
    return "ESCALATED", f"Educator did not meet the {MASTERY_SCORE}/10 mastery threshold across all pillars [P:{p}, T:{t}, C:{c}]."


# ── Main task ────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="worker.task.process_video_submission",
    autoretry_for=(),
    max_retries=0,
    # Enforced by the prefork pool only; the stage checks below cover `--pool=solo`.
    soft_time_limit=JOB_DEADLINE_S + 60,
    time_limit=JOB_DEADLINE_S + 120,
)
def process_video_submission(self, session_id, file_path, task_type="INITIAL"):
    """Background AI evaluation of an educator's video submission."""
    sid = str(session_id)[:8]
    logger.info(f"[{sid}] {task_type} JOB STARTED - File: {file_path}")

    remote = None
    retained_file = None
    phase = "ERROR"             # failsafe default
    keep_retained = True        # only flipped to False after a successful final DB write
    tmp_safe_to_delete = False  # only True once the video is safely copied (or not needed)
    conversation = None         # None until the session has been loaded
    state_data = None
    round_number = 1
    deadline = time.monotonic() + JOB_DEADLINE_S

    def _check_time(stage):
        if time.monotonic() > deadline:
            raise UpstreamUnavailable(f"Job deadline of {JOB_DEADLINE_S}s exceeded before {stage}")

    try:
        # ── Validate ─────────────────────────────────────────
        if task_type not in {"INITIAL", "FOLLOW_UP"}:
            raise ValueError(f"Unknown task_type: {task_type}")

        # ── Load session ─────────────────────────────────────
        row = _db_read(session_id)

        # A finished session must never be modified by a late/duplicate task.
        if row.get("phase") in TERMINAL_PHASES:
            logger.warning(f"[{sid}] Session already finished ({row.get('phase')}); skipping.")
            tmp_safe_to_delete = True
            return {"session_id": session_id, "phase": row.get("phase"), "skipped": "session_finished"}

        scenario = row.get("scenario", "")
        conversation = safe_json(row.get("conversation"), [])
        if not isinstance(conversation, list):
            conversation = []
        round_number = int(row.get("round_number") or 1)
        state_data = safe_json(row.get("state_data"), {})
        if not isinstance(state_data, dict):
            state_data = {}

        active_question = state_data.get("active_question")
        active_q_index = int(state_data.get("active_q_index", 0) or 0)
        consecutive_insufficient = int(state_data.get("consecutive_insufficient", 0) or 0)
        total_videos_submitted = int(state_data.get("total_videos_submitted", 0) or 0)

        if task_type == "FOLLOW_UP" and not active_question:
            raise RuntimeError("FOLLOW_UP submission received without an active question.")

        # Same submission delivered twice: this round's result is already stored.
        if _already_processed(conversation, task_type, round_number, active_q_index):
            logger.warning(f"[{sid}] {task_type} already processed for round {round_number}; skipping duplicate.")
            tmp_safe_to_delete = True
            return {"session_id": session_id, "phase": row.get("phase"), "skipped": "duplicate"}

        # ── Video Retention (Safe Recovery) ───────────────────
        os.makedirs(RETAINED_VIDEO_DIR, exist_ok=True)
        retained_file = os.path.join(RETAINED_VIDEO_DIR, f"{session_id}.mp4")

        if file_path and os.path.abspath(file_path) != os.path.abspath(retained_file):
            shutil.copy2(file_path, retained_file)
        tmp_safe_to_delete = True  # copy done (or nothing to copy)

        if not os.path.exists(retained_file):
            raise FileNotFoundError(f"Retained video file missing: {retained_file}")

        # ── Upload video to Google ───────────────────────────
        _check_time("upload")
        logger.info(f"[{sid}] Uploading video to Gemini...")
        remote = gemini_client.files.upload(file=retained_file)

        upload_deadline = time.monotonic() + UPLOAD_TIMEOUT_S
        while True:
            state_name = getattr(getattr(remote, "state", None), "name", None)
            if state_name != "PROCESSING":
                break
            if time.monotonic() > upload_deadline:
                raise UpstreamUnavailable(
                    f"Google did not finish processing the video within {UPLOAD_TIMEOUT_S}s"
                )
            time.sleep(UPLOAD_POLL_S)
            remote = gemini_client.files.get(name=remote.name)

        if state_name == "FAILED":
            raise RuntimeError("Video rejected by Google API.")
        if state_name is None:
            logger.warning(f"[{sid}] Uploaded file has no state; continuing.")
        logger.info(f"[{sid}] Upload complete.")

        # ── Transcribe ───────────────────────────────────────
        _check_time("transcription")
        logger.info(f"[{sid}] Transcribing...")
        step1, transcription_model = call_model(
            [remote, TRANSCRIBE_PROMPT.format(guardrails=GUARDRAILS, rubric=CONDUCT_RUBRIC)],
            TRANSCRIBE_SCHEMA,
        )

        transcript = (step1.get("transcript") or "").strip()
        word_count = int(step1.get("word_count") or 0)
        duration = float(step1.get("duration_seconds") or 0)
        content_type = step1.get("content_type", "OTHER_UNRELATED")
        wpm = round((word_count / duration) * 60) if duration > 0 else 0
        total_videos_submitted += 1

        logger.info(f"[{sid}] {word_count} words | {round(duration)}s | {wpm} WPM | {content_type}")

        # ── Conduct Screen ───────────────────────────────────
        # Runs when the transcriber flagged the content, and (by default) on every
        # non-empty transcript, so one classifier miss can't bypass moderation.
        flagged_by_transcriber = content_type == "INAPPROPRIATE_OR_OFFENSIVE"
        if flagged_by_transcriber or (MODERATE_ALL_TRANSCRIPTS and transcript):
            _check_time("conduct screen")
            if flagged_by_transcriber:
                logger.warning(f"[{sid}] ⚠ Transcriber flagged content. Running conduct screen...")
            else:
                logger.info(f"[{sid}] Running conduct screen...")
            context_str = build_context(conversation) or scenario
            mod_prompt = MODERATION_PROMPT.format(
                prompt_context=context_str,
                transcript=transcript,
                rubric=CONDUCT_RUBRIC
            )
            mod_result, _ = call_model([mod_prompt], MODERATION_SCHEMA)

            # Fail closed: no usable severity means we don't know, so don't carry on grading.
            severity = str(_require(mod_result, "severity", "conduct screen")).strip().upper()

            if severity in CONDUCT_SEVERITIES:
                logger.warning(f"[{sid}] ⚠ Conduct violation confirmed: {severity}")
                conversation.append({
                    "type": "conduct_flag",
                    "round": round_number,
                    "severity": severity,
                    "categories": mod_result.get("categories") or [],
                    "evidence": mod_result.get("evidence") or [],
                    "rationale": mod_result.get("rationale") or "",
                })
                phase = "ESCALATED_CONDUCT"
                _db_write(session_id, phase, round_number, conversation, state_data)
                keep_retained = phase in RETAIN_PHASES
                return {"session_id": session_id, "phase": phase}

        # ═════════════════════════════════════════════════════
        # INITIAL VIDEO
        # ═════════════════════════════════════════════════════
        if task_type == "INITIAL":
            if should_gate(step1, MIN_WORDS_INITIAL):
                logger.warning(f"[{sid}] Content gate -> auto-gated")
                result = auto_fail_video(step1)
                consecutive_insufficient += 1
            else:
                _check_time("initial grading")
                logger.info(f"[{sid}] Grading initial video...")
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
                f"C={result.get('communication_score')} -> FOLLOW_UP"
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
                logger.warning(f"[{sid}] Content gate -> auto-gated")
                answer_eval = auto_fail_answer(step1)
                consecutive_insufficient += 1
            else:
                _check_time("answer grading")
                logger.info(f"[{sid}] Grading follow-up answer...")
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
                if _require(answer_eval, "quality", "answer grading") == "INSUFFICIENT":
                    consecutive_insufficient += 1
                else:
                    consecutive_insufficient = 0

            quality = _require(answer_eval, "quality", "answer grading")
            assessment = _require(answer_eval, "assessment", "answer grading")
            answer_score = _require(answer_eval, "score", "answer grading")

            logger.info(f"[{sid}] Answer result: {quality} ({answer_score}/10)")

            conversation.append({
                "type": "answer_eval",
                "round": round_number,
                "index": active_q_index,
                "transcript": answer_eval.get("transcript", ""),
                "word_count": answer_eval.get("word_count", 0),
                "addresses_question": answer_eval.get("addresses_question", False),
                "quality": quality,
                "assessment": assessment,
                "key_insight": answer_eval.get("key_insight", ""),
                "model_answer": answer_eval.get("model_answer", ""),
                "score": answer_score,
                "_used_model": answer_eval.get("_used_model"),
                "_low_confidence": answer_eval.get("_low_confidence", False),
                "_auto_gated": answer_eval.get("_auto_gated", False),
                "_gate_reason": answer_eval.get("_gate_reason"),
            })

            # ── Safety nets ──────────────────────────────────
            if (consecutive_insufficient >= ESCALATION_STREAK or total_videos_submitted >= MAX_TOTAL_VIDEOS):
                conversation.append({
                    "type": "round_decision",
                    "round": round_number,
                    "decision": "ESCALATED",
                    "summary": "Your responses will be reviewed by a Master Trainer.",
                    "system_note": "Escalated by safety net: consecutive insufficient responses or video limit reached.",
                    "scores": {},
                })
                phase = "ESCALATED"
                active_question = None

            # ── Round decision ───────────────────────────────
            # The model supplies the P/T/C scores; the CERTIFIED/ESCALATED
            # decision itself is computed in Python by _decide().
            else:
                _check_time("round decision")
                logger.info(f"[{sid}] Making round decision...")
                context = build_context(conversation)
                decision_prompt = ROUND_DECISION_PROMPT.format(
                    guardrails=GUARDRAILS,
                    round_number=round_number,
                    context=context,
                )
                decision, decision_model = call_model(
                    [decision_prompt], ROUND_DECISION_SCHEMA,
                )

                p = _score(decision, "pedagogy_score", "round decision")
                t = _score(decision, "tech_accuracy_score", "round decision")
                c = _score(decision, "communication_score", "round decision")

                # Low confidence if ANY model in the chain was the failsafe: this
                # round's decision or transcription, or any earlier grading step.
                low_confidence = (
                    decision_model == FAILSAFE_MODEL
                    or transcription_model == FAILSAFE_MODEL
                    or _any_low_confidence(conversation)
                )

                round_scores = {
                    "pedagogy_score": p,
                    "pedagogy_reasoning": decision.get("pedagogy_reasoning", "No specific pedagogy feedback provided."),
                    "tech_accuracy_score": t,
                    "tech_accuracy_reasoning": decision.get("tech_accuracy_reasoning", "No specific technical feedback provided."),
                    "communication_score": c,
                    "communication_reasoning": decision.get("communication_reasoning", "No specific communication feedback provided."),
                }

                computed_decision, system_note = _decide(p, t, c, low_confidence)

                conversation.append({
                    "type": "round_decision",
                    "round": round_number,
                    "decision": computed_decision,
                    "summary": decision.get("round_summary", ""),
                    "system_note": system_note,
                    "improvement_areas": decision.get("improvement_areas", ""),
                    "scores": round_scores,
                    "_used_model": decision_model,
                })

                phase = computed_decision
                if phase == "CERTIFIED":
                    logger.info(f"[{sid}] CERTIFIED (P:{p} T:{t} C:{c})")
                else:
                    logger.warning(f"[{sid}] ESCALATED (P:{p} T:{t} C:{c}) low_confidence={low_confidence}")

                active_question = None

        # ── Persist ──────────────────────────────────────────
        final_state = {
            "active_question": active_question,
            "active_q_index": active_q_index,
            "consecutive_insufficient": consecutive_insufficient,
            "total_videos_submitted": total_videos_submitted,
        }
        _db_write(session_id, phase, round_number, conversation, final_state)
        keep_retained = phase in RETAIN_PHASES   # only decided once the write has succeeded
        logger.info(f"[{sid}] JOB SUCCESS -> {phase}")

        return {
            "session_id": session_id,
            "phase": phase,
            "round_number": round_number,
            "total_videos_submitted": total_videos_submitted,
        }

    # ── Self-Healing Graceful Escapes ────────────────────────
    except ContentBlocked as exc:
        logger.warning(f"[{sid}] ⚠ Google API Safety Block triggered.")
        if conversation is None:  # blocked before the session was loaded; nothing safe to write
            return _fail(session_id, "content_blocked", exc, task_type, sid)
        conversation.append({
            "type": "conduct_flag",
            "round": round_number,
            "severity": "CRITICAL",
            "categories": ["PROVIDER_SAFETY_BLOCK"],
            "evidence": [],
            "rationale": "The upstream AI provider strictly blocked this video due to severe safety/conduct violations.",
        })
        try:
            _db_write(session_id, "ESCALATED_CONDUCT", round_number, conversation, state_data)
        except Exception as db_err:
            logger.error(f"[{sid}] Could not write conduct escalation: {db_err}")
            _fail(session_id, "db_write_failed", db_err, task_type, sid)
            raise
        phase = "ESCALATED_CONDUCT"
        return {"session_id": session_id, "phase": phase}

    except UpstreamUnavailable as exc:
        logger.warning(f"[{sid}] ⚠ Upstream unavailable ({exc}). Marking as ERROR for UI retry.")
        return _fail(session_id, "upstream_unavailable", exc, task_type, sid)

    except SoftTimeLimitExceeded as exc:
        logger.warning(f"[{sid}] ⚠ Task time limit hit. Marking as ERROR for UI retry.")
        return _fail(session_id, "timeout", exc, task_type, sid)

    except ModelOutputError as exc:
        logger.warning(f"[{sid}] ⚠ Unusable model output: {exc}. Marking as ERROR for UI retry.")
        return _fail(session_id, "malformed_model_output", exc, task_type, sid)

    except Exception as exc:
        logger.error(f"[{sid}] CRITICAL ERROR: {exc}\n{traceback.format_exc()}")
        _fail(session_id, "internal_error", exc, task_type, sid)
        raise

    finally:
        # Cleanup remote file
        try:
            if remote:
                gemini_client.files.delete(name=remote.name)
                logger.info(f"[{sid}] Remote Google file deleted.")
        except Exception as e:
            logger.warning(f"[{sid}] Could not delete remote file: {e}")

        # Cleanup local files
        try:
            # Retained copy: deleted only after a successful final write whose phase
            # doesn't need the video kept. Any error or flagged outcome keeps it.
            if not keep_retained and retained_file and os.path.exists(retained_file):
                os.remove(retained_file)
                logger.info(f"[{sid}] Local retained video deleted.")

            # Streamlit tmp file: deleted once the retained copy exists (or nothing
            # was needed). If the copy itself failed, it's left in place for recovery.
            same_as_retained = (
                file_path and retained_file
                and os.path.abspath(file_path) == os.path.abspath(retained_file)
            )
            if tmp_safe_to_delete and file_path and not same_as_retained and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"[{sid}] Streamlit tmp video deleted.")
        except Exception as e:
            logger.warning(f"[{sid}] Could not delete local video: {e}")

        logger.info(f"[{sid}] Cleanup complete.\n")