# services/scenario.py
"""
Scenario generation via Gemini.
"""
import os
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

FALLBACK_SCENARIO = (
    "A Grade 6 student's Smart Agriculture project loses its Wi-Fi "
    "connection mid-demonstration. Explain data packet loss to the class "
    "using physical analogies they can see and touch."
)

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

    try:
        client = genai.Client(api_key=key)
        r = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=SCENARIO_PROMPT,
            config=types.GenerateContentConfig(temperature=0.9),
        )
        text = getattr(r, "text", "").strip()
        logger.info("services.scenario - Successfully generated scenario via Gemini.")
        return text if text else FALLBACK_SCENARIO
    except Exception as e:
        logger.error(f"services.scenario - Gemini generation failed: {e}. Using fallback.")
        return FALLBACK_SCENARIO