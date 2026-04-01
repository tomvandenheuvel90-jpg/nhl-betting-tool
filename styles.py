"""
styles.py — BetAnalyzer Dark Pro Theme CSS
Importeer APP_CSS en inject via st.markdown(APP_CSS, unsafe_allow_html=True)
"""

APP_CSS = """
<style>
/* ═══════════════════════════════════════════════════════
   DARK PRO THEME — BetAnalyzer  (contrast-verbeterd)
   Palette:
     bg-deep:      #08081a   (main background)
     bg-surface:   #11112b   (cards / sidepanels)
     bg-raised:    #1a1a3e   (hover / nested)
     primary:      #7c3aed   (violet)
     primary-lg:   #9d5ff5   (hover)
     glow:         rgba(124,58,237,0.18)
     text:         #e8eaf6   (body — iets helderder)
     text-muted:   #a8aace   (secondary — was #7070a0, nu leesbaar)
     text-subtle:  #8888b8   (tertiary — was #6060a0)
     border:       #2e2e56   (subtle border — iets lichter)
     green:        #4ade80
     yellow:       #facc15
     red:          #f87171
═══════════════════════════════════════════════════════ */

/* ── Global background & typography ── */
.stApp {
  background: #08081a !important;
  color: #e8eaf6 !important;
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
}

/* ── Main content container ── */
.block-container {
  max-width: 760px !important;
  padding-top: 1.8rem !important;
  padding-bottom: 3rem !important;
}

/* ── Top Streamlit header bar ── */
[data-testid="stHeader"] {
  background: linear-gradient(135deg, #0d0d25 0%, #12103a 100%) !important;
  border-bottom: 1px solid #2e2e56 !important;
}

/* ── Headings ── */
h1 { color: #c4b5fd !important; letter-spacing: -0.5px; }
h2 { color: #b0a0f8 !important; }
h3 { color: #b0a0f8 !important; }
h4 { color: #c4b5fd !important; }

/* ── Markdown & body text ── */
p, li { color: #e8eaf6 !important; }
.stMarkdown p { color: #e8eaf6 !important; }

/* ── Form labels — waren soms onzichtbaar ── */
label,
.stTextInput label,
.stNumberInput label,
.stSelectbox label,
.stDateInput label,
.stCheckbox label,
[data-testid="stWidgetLabel"] {
  color: #c8caee !important;
  font-weight: 500 !important;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
  background: #11112b !important;
  border-radius: 12px !important;
  padding: 4px !important;
  gap: 2px !important;
  border: 1px solid #2e2e56 !important;
}
.stTabs [data-baseweb="tab"] {
  background: transparent !important;
  color: #a8aace !important;
  border-radius: 8px !important;
  font-weight: 600 !important;
  font-size: 0.88rem !important;
  padding: 8px 18px !important;
  border: none !important;
  transition: all 0.2s ease !important;
}
.stTabs [aria-selected="true"] {
  background: linear-gradient(135deg, #5b21b6 0%, #7c3aed 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 2px 12px rgba(124,58,237,0.4) !important;
}
.stTabs [data-baseweb="tab"]:hover:not([aria-selected="true"]) {
  background: #1a1a3e !important;
  color: #c4b5fd !important;
}
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }
.stTabs [data-baseweb="tab-border"]    { display: none !important; }

/* ── Primary button ── */
.stButton > button[kind="primary"],
.stButton > button[data-testid="baseButton-primary"] {
  background: linear-gradient(135deg, #5b21b6 0%, #7c3aed 100%) !important;
  color: #ffffff !important;
  border: none !important;
  border-radius: 10px !important;
  font-weight: 700 !important;
  font-size: 1rem !important;
  padding: 12px 24px !important;
  box-shadow: 0 4px 20px rgba(124,58,237,0.35) !important;
  transition: all 0.2s ease !important;
  letter-spacing: 0.3px;
}
.stButton > button[kind="primary"]:hover {
  background: linear-gradient(135deg, #6d28d9 0%, #9d5ff5 100%) !important;
  box-shadow: 0 6px 28px rgba(124,58,237,0.55) !important;
  transform: translateY(-1px) !important;
}
.stButton > button[kind="primary"]:disabled {
  background: #2a2a50 !important;
  color: #6868a0 !important;
  box-shadow: none !important;
  transform: none !important;
}

/* ── Secondary / normal buttons ── */
.stButton > button[kind="secondary"],
.stButton > button:not([kind="primary"]) {
  background: #14142e !important;
  color: #c4b5fd !important;
  border: 1px solid #3e3e72 !important;
  border-radius: 8px !important;
  font-weight: 600 !important;
  transition: all 0.2s ease !important;
}
.stButton > button[kind="secondary"]:hover,
.stButton > button:not([kind="primary"]):hover {
  background: #1a1a3e !important;
  border-color: #7c3aed !important;
  color: #ffffff !important;
}

/* ── File uploader ── */
[data-testid="stFileUploaderDropzone"] {
  background: #11112b !important;
  border: 2px dashed #5b21b6 !important;
  border-radius: 12px !important;
  transition: all 0.2s ease !important;
}
[data-testid="stFileUploaderDropzone"]:hover {
  border-color: #7c3aed !important;
  background: #16163a !important;
}
[data-testid="stFileUploaderDropzone"] p,
[data-testid="stFileUploaderDropzone"] span {
  color: #a8aace !important;
}

/* ── Text inputs, number inputs, selectboxes ── */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stSelectbox > div > div,
.stDateInput > div > div > input {
  background: #13132e !important;
  border: 1px solid #2e2e56 !important;
  border-radius: 8px !important;
  color: #e8eaf6 !important;
}
.stTextInput > div > div > input::placeholder,
.stNumberInput > div > div > input::placeholder {
  color: #6868a0 !important;
}
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
  border-color: #7c3aed !important;
  box-shadow: 0 0 0 2px rgba(124,58,237,0.25) !important;
}

/* ── Number input – wrapper + stepper knoppen donker ── */
[data-testid="stNumberInput"] > div > div,
[data-testid="stNumberInput"] [data-baseweb="base-input"] {
  background: #13132e !important;
  border: 1px solid #2e2e56 !important;
  border-radius: 8px !important;
}
[data-testid="stNumberInput"] button {
  background: #13132e !important;
  color: #c4b5fd !important;
  border-color: #2e2e56 !important;
}
[data-testid="stNumberInput"] button:hover {
  background: #1a1a3e !important;
  color: #ffffff !important;
}

/* ── Selectbox dropdown list ── */
[data-baseweb="popover"] {
  background: #13132e !important;
}
[data-baseweb="menu"] {
  background: #13132e !important;
  border: 1px solid #2e2e56 !important;
}
[data-baseweb="option"] {
  background: #13132e !important;
  color: #e8eaf6 !important;
}
[data-baseweb="option"]:hover {
  background: #1a1a3e !important;
  color: #c4b5fd !important;
}

/* ── Expanders ── */
[data-testid="stExpander"] {
  background: #11112b !important;
  border: 1px solid #2e2e56 !important;
  border-radius: 10px !important;
  overflow: hidden !important;  /* zorgt dat border-radius ook header afknipt */
}
[data-testid="stExpander"] summary {
  background: #11112b !important;  /* header zelfde donkere achtergrond als card body */
  color: #c4b5fd !important;
  font-weight: 600 !important;
}
[data-testid="stExpander"] summary:hover {
  background: #16163a !important;  /* subtiele hover-highlight */
  color: #e0d4ff !important;
}
[data-testid="stExpander"] > div > div {
  color: #e8eaf6 !important;
}

/* ── Metrics ── */
[data-testid="stMetric"] {
  background: #11112b !important;
  border: 1px solid #2e2e56 !important;
  border-radius: 10px !important;
  padding: 12px 16px !important;
}
[data-testid="stMetricValue"] { color: #c4b5fd !important; font-weight: 700 !important; }
[data-testid="stMetricLabel"] { color: #a8aace !important; font-weight: 500 !important; }
[data-testid="stMetricDelta"]  { font-weight: 600 !important; }

/* ── Alerts ── */
[data-testid="stAlert"][kind="success"], .stSuccess > div,
div[data-testid="stAlertContainer"][data-baseweb="notification"][kind="positive"] {
  background: rgba(74,222,128,0.12) !important;
  border: 1px solid rgba(74,222,128,0.35) !important;
  border-radius: 10px !important;
  color: #6effa0 !important;
}
[data-testid="stAlert"][kind="warning"], .stWarning > div,
div[data-testid="stAlertContainer"][kind="warning"] {
  background: rgba(250,204,21,0.12) !important;
  border: 1px solid rgba(250,204,21,0.35) !important;
  border-radius: 10px !important;
  color: #ffe566 !important;
}
[data-testid="stAlert"][kind="error"], .stError > div,
div[data-testid="stAlertContainer"][kind="error"] {
  background: rgba(248,113,113,0.12) !important;
  border: 1px solid rgba(248,113,113,0.35) !important;
  border-radius: 10px !important;
  color: #ff9090 !important;
}
[data-testid="stAlert"][kind="info"], .stInfo > div,
div[data-testid="stAlertContainer"][kind="info"] {
  background: rgba(99,179,237,0.12) !important;
  border: 1px solid rgba(99,179,237,0.35) !important;
  border-radius: 10px !important;
  color: #90cff5 !important;
}

/* ── Captions & small text ── */
.stCaption,
[data-testid="stCaptionContainer"],
small {
  color: #8888b8 !important;
}

/* ── Dataframe / table ── */
[data-testid="stDataFrame"] table,
.dvn-scroller {
  background: #11112b !important;
  color: #e8eaf6 !important;
}
[data-testid="stDataFrame"] th {
  background: #1a1a3e !important;
  color: #c4b5fd !important;
  font-weight: 700 !important;
}
[data-testid="stDataFrame"] td {
  color: #dde0f5 !important;
  border-color: #2e2e56 !important;
}

/* ── Status widget (analyse voortgang) ── */
[data-testid="stStatusWidget"] {
  background: #11112b !important;
  border: 1px solid #2e2e56 !important;
  border-radius: 10px !important;
  color: #e8eaf6 !important;
}
[data-testid="stStatusWidget"] p { color: #c8caee !important; }

/* ── Horizontal dividers ── */
hr { border-color: #2e2e56 !important; margin: 1.5rem 0 !important; }

/* ── Spinner ── */
.stSpinner > div > div { border-top-color: #7c3aed !important; }

/* ── Columns gap fix ── */
[data-testid="column"] { gap: 0.75rem !important; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #0a0a1e; }
::-webkit-scrollbar-thumb { background: #3a3a70; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #7c3aed; }

/* ── Rating & EV classes ── */
.rating-strong  { color: #4ade80 !important; font-weight: 700; }
.rating-matig   { color: #facc15 !important; font-weight: 700; }
.rating-vermijd { color: #f87171 !important; font-weight: 700; }
.ev-positive    { color: #4ade80 !important; font-size: 1.3rem; font-weight: 800; }
.ev-low         { color: #facc15 !important; font-size: 1.3rem; font-weight: 800; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
  background: #0d0d28 !important;
  border-right: 1px solid #2e2e56 !important;
}

/* ── Progress bar ── */
[data-testid="stProgressBar"] > div > div {
  background: linear-gradient(90deg, #5b21b6, #7c3aed) !important;
}

/* ── Select slider ── */
[data-testid="stSlider"] > div > div > div > div { background: #7c3aed !important; }

/* ── Checkbox ── */
[data-testid="stCheckbox"] > label > div[role="checkbox"] { border-color: #5b21b6 !important; }
[data-testid="stCheckbox"] span { color: #c8caee !important; }

/* ── Tooltip ── */
[data-testid="stTooltipIcon"] { color: #a8aace !important; }

</style>
"""
