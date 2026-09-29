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

# ── Content Types ────────────────────────────────────────────
INVALID_CONTENT_TYPES = {
    "PROMOTIONAL_OR_SCRIPTED",
    "SILENT_OR_INAUDIBLE",
    "OTHER_UNRELATED",
}