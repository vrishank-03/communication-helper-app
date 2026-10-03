# services/scenario.py
"""
Scenario generation via Gemini.
"""
import os
import random
from google import genai
from google.genai import types
from core.config import logger

SCENARIO_PROMPT = """\
You are a Master Trainer at OLL generating a certification scenario for a STEM educator who teaches with WitBlox kits.
Generate ONE unique, challenging impromptu classroom scenario.

CRITICAL RULE: You are FORBIDDEN from using the 'angry parent' or 'crying student' tropes. Do not include them.

CATEGORIES — randomly select exactly ONE and build the scenario around it:
A. Abstract Concept Breakdown: the educator must explain a hard concept (e.g. data packet loss, feedback loops, sensor calibration) to a specific grade level, using physical analogies, without jargon.
B. Unseen Hardware Glitch: a WitBlox kit misbehaves for a hidden reason (e.g. a loose connection, a wrong pin, a weak battery, or sensor interference) and must be diagnosed live.
C. Classroom Distraction: something is derailing the lesson (e.g. one group racing ahead, side conversations, students off-task on devices) and the educator must regain focus without stopping the learning.
D. Advanced Student Question: a student asks something beyond the syllabus or challenges the educator's explanation with something they saw online.

Output ONLY the scenario text in 2-3 sentences. No titles, no labels, no markdown, and do not mention the category name or letter.\
"""

# Same fallback order your grading worker uses (taken from the worker logs).
SCENARIO_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

# Per-request timeout in milliseconds, so a 503 doesn't freeze the UI for 15-19s.
REQUEST_TIMEOUT_MS = 10_000

# Static fallbacks, used only if every model fails. One per category (A-D).
FALLBACK_SCENARIOS = [
    (
        "A Grade 6 student's Smart Agriculture project loses its Wi-Fi "
        "connection mid-demonstration. Explain data packet loss to the class "
        "using physical analogies they can see and touch."
    ),
    (
        "Halfway through class, a group's WitBlox soil-moisture sensor keeps "
        "returning zero even though the wiring looks correct. Diagnose the "
        "problem live while keeping the rest of the class engaged."
    ),
    (
        "One group has finished the activity and is racing ahead while two "
        "other groups are chatting and off-task. Regain everyone's focus "
        "without stopping the learning."
    ),
    (
        "A Grade 8 student says they read online that 'AI doesn't really need "
        "sensors, it just guesses.' Respond to the challenge in a way that "
        "connects to what their WitBlox kit is actually doing."
    ),
]

# Kept for any other module that still imports the old name.
FALLBACK_SCENARIO = FALLBACK_SCENARIOS[0]


def generate_scenario(api_key: str | None = None) -> str:
    """Call Gemini to produce a fresh scenario; fall back to a static one on error."""
    logger.debug("services.scenario.generate_scenario - Generating dynamic scenario...")
    try:
        import streamlit as st
        key = api_key or st.secrets.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    except Exception:
        key = api_key or os.getenv("GEMINI_API_KEY")

    # Fallback to our config if the above fails
    if not key:
        from core.config import GEMINI_API_KEY
        key = GEMINI_API_KEY

    # Pick the category in Python; LLMs asked to "randomly select" tend to pick A almost every time.
    category = random.choice("ABCD")
    prompt = f"{SCENARIO_PROMPT}\n\nFor this request, use category {category}."

    try:
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
    except Exception as e:
        logger.error(f"services.scenario - Could not create Gemini client: {e}. Using fallback.")
        return random.choice(FALLBACK_SCENARIOS)

    for model in SCENARIO_MODELS:
        try:
            r = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.9),
            )
            text = (getattr(r, "text", "") or "").strip()
            if text:
                logger.info(f"services.scenario - Successfully generated scenario via {model}.")
                return text
            logger.warning(f"services.scenario - {model} returned empty text → next")
        except Exception as e:
            logger.warning(f"services.scenario - {model} failed: {e} → next")

    logger.error("services.scenario - All Gemini models failed. Using fallback.")
    return random.choice(FALLBACK_SCENARIOS)