"""
Global CSS injected once at app start.
"""

import streamlit as st

def inject_styles():
    st.markdown("""
<style>
    .stApp { background-color: #FAFBFC; }
    h1, h2, h3 {
        color: #181818;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        font-weight: 600;
    }
    .stButton>button {
        background-color: #0176D3; color: white; border-radius: 6px;
        padding: 8px 20px; font-weight: 500; border: none;
        font-family: 'Inter', -apple-system, sans-serif;
    }
    .stButton>button:hover { background-color: #014486; color: white; }
    .q-card {
        background: #FFFFFF; border-left: 4px solid #0176D3;
        padding: 16px; margin: 12px 0; border-radius: 4px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .a-card {
        background: #F3F4F5; border-left: 4px solid #706E6B;
        padding: 12px 16px; margin: 12px 0; border-radius: 4px;
        font-style: italic;
    }
    .eval-pass {
        background: #FFFFFF; border-left: 4px solid #2E844A;
        padding: 16px; margin: 12px 0; border-radius: 4px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .eval-fail {
        background: #FFFFFF; border-left: 4px solid #C23934;
        padding: 16px; margin: 12px 0; border-radius: 4px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .model-ans {
        background: #F4FBF6; border-left: 4px solid #2E844A;
        padding: 16px; margin: 12px 0; border-radius: 4px;
    }
    .log-line {
        font-family: 'Courier New', monospace;
        font-size: 0.75rem; color: #555; margin-bottom: 2px;
    }
</style>
""", unsafe_allow_html=True)
