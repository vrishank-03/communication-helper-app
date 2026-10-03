"""
Global CSS injected once at app start.
"""

import streamlit as st

def inject_styles():
    st.markdown("""
<style>
    /* Hide the entire top header (removes Stop, Deploy, and the hamburger menu) */
    header {visibility: hidden;}
        
    /* Hide the default Streamlit footer "Made with Streamlit" */
    footer {visibility: hidden;}
    
    /* Hide the main menu */
    #MainMenu {visibility: hidden;}
    
    .stApp { background-color: #FAFBFC; }
    h1, h2, h3, h4 {
        color: #181818;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        font-weight: 600;
    }
    /* Button colours come from [theme] primaryColor in .streamlit/config.toml */
    .stButton>button, .stFormSubmitButton>button {
        border-radius: 6px; font-weight: 500;
        font-family: 'Inter', -apple-system, sans-serif;
    }
    [data-testid="stMetric"] { background: #FFFFFF; }
    [data-testid="stMetricLabel"] p { color: #5A6472; font-size: 0.8rem; font-weight: 500; }
    [data-testid="stMetricValue"] { font-size: 1.6rem; font-weight: 600; color: #181818; }
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
