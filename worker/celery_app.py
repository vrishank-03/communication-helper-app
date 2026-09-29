"""
Celery application instance + Gemini client + model cascade.

Both `tasks.py` (root) and `worker/task.py` import from here.
"""

import os
import json
import time
import tomllib

from celery import Celery
from google import genai
from google.genai import types

from core.config import MODEL_CHAIN, FAILSAFE_MODEL

# ── Celery ───────────────────────────────────────────────────
celery_app = Celery(
    "evaluation_tasks",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6380/0"),
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
    raise ValueError(
        "GEMINI_API_KEY not found in environment or .streamlit/secrets.toml"
    )

gemini_client = genai.Client(api_key=API_KEY)


# ── Model cascade ────────────────────────────────────────────
def call_model(contents, schema, max_retries=2):
    """Try each model in the chain.  Returns ``(parsed_dict, model_name)``."""
    last_error = None

    for attempt in range(max_retries):
        for model_name in MODEL_CHAIN:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                )
                text = (getattr(response, "text", None) or "").strip()
                if not text:
                    raise ValueError("Gemini returned an empty response.")

                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise ValueError("Response was not a JSON object.")

                return parsed, model_name

            except Exception as exc:
                last_error = exc
                short = str(exc).split("{", 1)[0].strip()
                print(f"      [!] {model_name}: {short} → next")

        if attempt < max_retries - 1:
            print("      [!] All models failed. Retrying in 5s...")
            time.sleep(5)

    raise RuntimeError("All Gemini models exhausted after retries.") from last_error
