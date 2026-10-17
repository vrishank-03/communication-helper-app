# worker/celery_app.py
"""
Celery application instance + Gemini client + resilient model cascade.

Both `tasks.py` (root) and `worker/task.py` import from here.

call_model() raises typed errors so the task can react correctly:
    UpstreamUnavailable  every model failed for transient reasons (503/429/timeout)
                         -> the task re-queues itself later instead of failing
    ContentBlocked       the provider's safety system refused the content
                         -> treated as a conduct signal, never as a system error
    ModelCallError       anything else (bad key, invalid request, ...)
"""

import os
import json
import time
import random
import tomllib

import httpx
from celery import Celery
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from core.config import (
    MODEL_CHAIN, MODEL_CASCADE_PASSES, MODEL_CALL_DEADLINE_S,
    MODEL_COOLDOWN_SECONDS, TEXT_REQUEST_TIMEOUT_MS, logger,
)
from core.models import now_iso

# ── Celery ───────────────────────────────────────────────────
celery_app = Celery(
    "evaluation_tasks",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6380/0"),
)
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 7200},
)

# ── API Key ──────────────────────────────────────────────────
API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    try:
        _secrets = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".streamlit", "secrets.toml",
        )
        with open(_secrets, "rb") as _f:
            API_KEY = tomllib.load(_f).get("GEMINI_API_KEY")
    except Exception:
        pass

if not API_KEY:
    raise ValueError("GEMINI_API_KEY not found in environment or .streamlit/secrets.toml")

gemini_client = genai.Client(api_key=API_KEY)


# ── Errors ───────────────────────────────────────────────────
class ModelCallError(RuntimeError):
    def __init__(self, message, *, trace=None):
        super().__init__(message)
        self.trace = trace or []

class UpstreamUnavailable(ModelCallError):
    pass

class ContentBlocked(ModelCallError):
    def __init__(self, message, *, reasons=(), trace=None):
        super().__init__(message, trace=trace)
        self.reasons = list(reasons)

class _Blocked(Exception):
    pass

class _Malformed(Exception):
    pass

TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
COOLDOWN_HTTP_CODES = {429, 503, 504}
BLOCK_FINISH_REASONS = {
    "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
}

# Force Google to bypass its own filters so OUR custom moderation rubric can evaluate it
SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

_cooldown_until: dict[str, float] = {}

def _enum_name(value):
    if value is None: return None
    return getattr(value, "name", None) or str(value)

def _response_text(response) -> str:
    """Extract text, safely turning provider safety blocks into ``_Blocked``."""
    try:
        text = response.text
    except ValueError:
        # The SDK throws a ValueError if it was blocked by safety filters and returned empty
        candidates = getattr(response, "candidates", None) or []
        finish = _enum_name(getattr(candidates[0], "finish_reason", None)) if candidates else None
        if finish in BLOCK_FINISH_REASONS:
            raise _Blocked(f"response blocked ({finish})")
        raise _Blocked("response blocked or returned empty due to safety filters.")

    feedback = getattr(response, "prompt_feedback", None)
    block = _enum_name(getattr(feedback, "block_reason", None)) if feedback else None
    if block and block != "BLOCKED_REASON_UNSPECIFIED":
        raise _Blocked(f"prompt blocked ({block})")

    candidates = getattr(response, "candidates", None) or []
    finish = _enum_name(getattr(candidates[0], "finish_reason", None)) if candidates else None
    if finish in BLOCK_FINISH_REASONS:
        raise _Blocked(f"response blocked ({finish})")

    text = (text or "").strip()
    if not text:
        raise _Malformed(f"empty response (finish_reason={finish or 'none'})")
    if finish == "MAX_TOKENS":
        raise _Malformed("response truncated (MAX_TOKENS)")
    return text

def classify_exception(exc) -> str:
    if isinstance(exc, (_Blocked, ContentBlocked)):
        return "blocked"
    if isinstance(exc, UpstreamUnavailable):
        return "transient"
    if isinstance(exc, (_Malformed, json.JSONDecodeError)):
        return "malformed"
    if isinstance(exc, genai_errors.APIError):
        return "transient" if getattr(exc, "code", None) in TRANSIENT_HTTP_CODES else "fatal"
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)):
        return "transient"
    return "fatal"

def _short_error(exc) -> str:
    if isinstance(exc, genai_errors.APIError):
        return f"{getattr(exc, 'code', '?')} {getattr(exc, 'status', '') or ''}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    return (str(exc).split("{", 1)[0].strip() or type(exc).__name__)[:160]

def _candidates():
    now = time.monotonic()
    ready = [m for m in MODEL_CHAIN if _cooldown_until.get(m, 0) <= now]
    return ready or list(MODEL_CHAIN)

# ── Model cascade ────────────────────────────────────────────
def call_model(contents, schema, *, timeout_ms=TEXT_REQUEST_TIMEOUT_MS,
               passes=MODEL_CASCADE_PASSES, deadline_s=MODEL_CALL_DEADLINE_S,
               trace=None, label="call"):
    trace = trace if trace is not None else []
    started = time.monotonic()
    outcomes = []
    block_reasons = []

    config = types.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=schema,
        safety_settings=SAFETY_SETTINGS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        http_options=types.HttpOptions(timeout=int(timeout_ms)),
    )

    for pass_no in range(passes):
        for model_name in _candidates():
            if time.monotonic() - started > deadline_s:
                break
            t0 = time.monotonic()
            entry = {"label": label, "model": model_name, "pass": pass_no + 1, "at": now_iso()}
            try:
                response = gemini_client.models.generate_content(
                    model=model_name, contents=contents, config=config,
                )
                parsed = json.loads(_response_text(response))
                if not isinstance(parsed, dict):
                    raise _Malformed("response was not a JSON object")
                entry.update(outcome="ok", ms=int((time.monotonic() - t0) * 1000))
                trace.append(entry)
                return parsed, model_name

            except Exception as exc:
                kind = classify_exception(exc)
                code = getattr(exc, "code", None)
                entry.update(
                    outcome=kind, code=code, error=_short_error(exc),
                    ms=int((time.monotonic() - t0) * 1000),
                )
                trace.append(entry)
                outcomes.append(kind)
                if kind == "blocked":
                    block_reasons.append(str(exc))
                if code in COOLDOWN_HTTP_CODES or isinstance(exc, httpx.TimeoutException):
                    _cooldown_until[model_name] = time.monotonic() + MODEL_COOLDOWN_SECONDS
                logger.warning(f"       [!] {label}: {model_name} -> {kind}: {entry['error']}")

        if time.monotonic() - started > deadline_s:
            logger.warning(f"       [!] {label}: deadline of {deadline_s}s reached")
            break
        if pass_no < passes - 1:
            delay = min(30.0, 4.0 * (2 ** pass_no)) + random.uniform(0, 2)
            logger.warning(f"       [!] {label}: all models failed, next pass in {delay:.0f}s")
            time.sleep(delay)

    if block_reasons:
        raise ContentBlocked(
            f"{label}: content refused by the provider safety system",
            reasons=sorted(set(block_reasons)), trace=trace,
        )
        
    if not outcomes or any(k in ("transient", "malformed") for k in outcomes):
        raise UpstreamUnavailable(
            f"{label}: all Gemini models are temporarily unavailable", trace=trace,
        )
    raise ModelCallError(f"{label}: all Gemini models failed", trace=trace)