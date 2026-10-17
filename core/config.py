# core/config.py
import os
import logging
import tomllib
from pathlib import Path

# ---------------------------------------------------------
# 1. ATOMIC LOGGING SETUP
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)-8s | %(module)-12s | %(funcName)-20s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("educator_eval")
logger.debug("core.config - Logger initialized. Starting configuration load.")

# ---------------------------------------------------------
# 2. SAFE CONFIGURATION LOADER 
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_PATH = BASE_DIR / ".streamlit" / "secrets.toml"

config_data = {}
if SECRETS_PATH.exists():
    try:
        with open(SECRETS_PATH, "rb") as f:
            config_data = tomllib.load(f)
    except Exception as e:
        logger.error(f"core.config - CRITICAL FAILURE parsing secrets.toml: {e}")

def get_config(key, default=None):
    if key in os.environ:
        return os.environ[key]
    if key in config_data:
        return config_data[key]
    return default

# ---------------------------------------------------------
# 3. DATABASE & SECRETS SETTINGS
# ---------------------------------------------------------
DB_USER = get_config("DB_USER", "postgres")
DB_PASS = get_config("DB_PASS", "postgres") 
DB_HOST = get_config("DB_HOST", "localhost")
DB_PORT = get_config("DB_PORT", "5432")
DB_NAME = get_config("DB_NAME", "oll_eval")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
REDIS_URL = get_config("REDIS_URL", "redis://localhost:6379/0")
GEMINI_API_KEY = get_config("GEMINI_API_KEY", "")

# ---------------------------------------------------------
# 4. ORIGINAL APPLICATION CONSTANTS (PRESERVED)
# ---------------------------------------------------------
# ── Evaluation Flow ──────────────────────────────────────────
MAX_ROUNDS = 2                # max follow-up rounds before forced decision
MAX_TOTAL_VIDEOS = 8          # global cap on video submissions per session
ESCALATION_STREAK = 3         # consecutive INSUFFICIENT answers → escalate
WEEKLY_EVAL_LIMIT = 2         # max evaluations per educator per rolling window
LIMIT_WINDOW_DAYS = 7         # rolling window size in days

# ── Content Gates ────────────────────────────────────────────
MIN_WORDS_INITIAL = 15        # minimum transcript words for initial video
MIN_WORDS_FOLLOWUP = 8        # minimum transcript words for follow-up video

# ── Fallback ─────────────────────────────────────────────────
FALLBACK_QUESTION = (
    "Show us how you would handle this live: pick the trickiest "
    "moment in the scenario and demonstrate exactly what you would "
    "say and do next, as if the students were in front of you."
)

# ── Gemini Model Cascade ────────────────────────────────────
MODEL_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

FAILSAFE_MODEL = MODEL_CHAIN[-1]

# ── Upstream resilience ─────────────────────────────────────
VIDEO_REQUEST_TIMEOUT_MS = 150_000   # per transcription request (video input)
TEXT_REQUEST_TIMEOUT_MS = 60_000     # per text-only grading / moderation request
MODEL_CASCADE_PASSES = 2             # passes over MODEL_CHAIN inside one call
MODEL_CALL_DEADLINE_S = 480          # hard wall-clock budget for one call_model()
MODEL_COOLDOWN_SECONDS = 90          # skip a model for this long after a 429/503/timeout
UPLOAD_MAX_WAIT_S = 300              # max time to wait for Google to finish processing a file

# When every model is overloaded the job is re-queued instead of failing.
# One entry per deferred retry (seconds) -> ~40 minutes of automatic recovery.
DEFERRED_RETRY_SCHEDULE = (60, 180, 420, 900, 1200)
EDUCATOR_SELF_RETRY_LIMIT = 3        # "Retry evaluation" clicks allowed per session
PROCESSING_STALE_SECONDS = 600       # RUNNING/QUEUED longer than this = stuck
RETAINED_VIDEO_DIR = BASE_DIR / "data" / "retained_videos"

# ── Content Types ────────────────────────────────────────────
INVALID_CONTENT_TYPES = {
    "PROMOTIONAL_OR_SCRIPTED",
    "SILENT_OR_INAUDIBLE",
    "OTHER_UNRELATED",
}

# ── Conduct moderation ──────────────────────────────────────
MODERATION_SEVERITIES = ("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL")
CONDUCT_REDO_LIMIT = 1               # MEDIUM incidents allowed before the session is held

# ── Review Workflow ──────────────────────────────────────────
SECURITY_FLAG_REASON = "CRITICAL_SECURITY_FLAG"
CONDUCT_FLAG_REASON = "CONDUCT_FLAG"
SYSTEM_FLAG_REASONS = (SECURITY_FLAG_REASON, CONDUCT_FLAG_REASON)

# Finished attempts (count towards the weekly limit)
COMPLETED_PHASES = ("CERTIFIED", "ESCALATED", "NOT_CERTIFIED", "REJECTED")

# Phases that need an admin before the educator can move on
ADMIN_PENDING_PHASES = ("ESCALATED", "ESCALATED_PLAGIARISM", "ESCALATED_CONDUCT", "ERROR")

# Ticket service-level targets (hours until a ticket is considered overdue)
TICKET_SLA_HOURS = {
    "conduct": 4,
    "hold": 24,
    "error": 4,
    "report": 48,
    "review": 72,
    "conduct_review": 48,
}

# ── Video Integrity ──────────────────────────────────────────
INTEGRITY_DIR = BASE_DIR / "data" / "integrity"   # models, thumbnails, held videos
FRAME_SAMPLE_SECONDS = 2        # one fingerprint frame every N seconds
FRAME_HASH_DISTANCE = 10        # max differing bits (of 64) for two frames to count as the same
DUPLICATE_FRAME_RATIO = 0.6     # share of aligned frames that must match to call it a copy
FACE_MATCH_THRESHOLD = 0.363    # SFace cosine: below this vs own earlier videos = different person
FACE_SHARED_THRESHOLD = 0.5     # SFace cosine: above this vs another educator = same person