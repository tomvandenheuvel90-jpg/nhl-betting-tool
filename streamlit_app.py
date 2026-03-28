#!/usr/bin/env python3
"""
Bet Analyzer — Streamlit Cloud versie
Ondersteunt: NHL · NBA · MLB · Voetbal (EPL/La Liga/Bundesliga/Serie A/Ligue 1)
"""

import streamlit as st
import os
import json
import re
import hashlib
import base64
import tempfile
import datetime
from pathlib import Path
from typing import Optional, List, Dict

# ─── Secrets injecteren vóór import van sports modules ────────────────────────
try:
    os.environ.setdefault(
        "FOOTBALL_DATA_API_KEY",
        st.secrets.get("FOOTBALL_DATA_TOKEN", ""),
    )
except Exception:
    pass

# ─── Sports modules ───────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent))

from sports import nhl, nba, mlb, soccer, odds_api
from scorer import composite_score, ev, rating
import db

try:
    soccer.API_KEY = st.secrets.get("FOOTBALL_DATA_TOKEN", "")
except Exception:
    pass

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
try:
    import injuries as _injuries_mod
    _INJURIES_AVAILABLE = True
except ImportError:
    _INJURIES_AVAILABLE = False


# ─── PIL / HEIC support ───────────────────────────────────────────────────────
try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PILImage = None
    _PIL_AVAILABLE = False

try:
    import pillow_heif as _pillow_heif
    _pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    _HEIF_AVAILABLE = False

# ─── Constanten ───────────────────────────────────────────────────────────────

SOCCER_COMPS = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}

# NHL-teamnamen en afkortingen voor fallback-detectie
_NHL_TEAM_KEYWORDS = {
    # Volledige namen
    "maple leafs", "bruins", "canadiens", "senators", "sabres", "red wings",
    "panthers", "lightning", "hurricanes", "capitals", "rangers", "islanders",
    "devils", "flyers", "penguins", "blue jackets", "blackhawks", "predators",
    "blues", "wild", "jets", "oilers", "flames", "canucks", "ducks", "kings",
    "sharks", "golden knights", "kraken", "avalanche", "stars", "coyotes",
    "canes", "caps", "leafs", "habs",
    # Afkortingen
    "tor", "bos", "mtl", "ott", "buf", "det", "fla", "tbl", "car", "wsh",
    "nyr", "nyi", "njd", "phi", "pit", "cbj", "chi", "nsh", "stl", "min",
    "wpg", "edm", "cgy", "van", "ana", "lak", "sjs", "vgk", "sea", "col",
    "dal", "ari", "uta",
}


def _is_nhl_match(m: dict) -> bool:
    """Detecteer of een Flashscore-wedstrijd NHL is, ook als competitie-veld ontbreekt."""
    sport_comp = (m.get("sport") or m.get("competition") or "").lower()
    if "nhl" in sport_comp or "hockey" in sport_comp:
        return True
    # Fallback: controleer of teamnamen overeenkomen met bekende NHL-teams
    home = (m.get("home_team") or "").lower()
    away = (m.get("away_team") or "").lower()
    return (
        any(kw in home for kw in _NHL_TEAM_KEYWORDS) or
        any(kw in away for kw in _NHL_TEAM_KEYWORDS)
    )


# Referentie-odds voor auto-gegenereerde props (geen Linemate)
_REF_ODDS = {
    "shots":       1.85,
    "anytime":     2.50,
    "points":      1.85,
    "rebounds":    1.85,
    "assists":     1.90,
    "threes":      2.00,
    "hits":        1.80,
    "total_bases": 1.85,
    "strikeouts":  1.90,
}

# Scenario labels
SCENARIO_LABELS = {
    1: "📊 Analyse op basis van historische data (geen Linemate)",
    2: "📊 Analyse op basis van Linemate + historische data (gecombineerd)",
    3: "📊 Analyse op basis van Linemate data",
}

# Scoring weights per scenario
SCENARIO_WEIGHTS = {
    1: (0.00, 0.70),   # (linemate_weight, season_weight)
    2: (0.42, 0.28),   # 60% linemate-deel + 40% historisch-deel van 70%
    3: (0.35, 0.35),   # standaard
}

EXTRACT_PROMPT = """
Je ziet één of meerdere screenshots van Linemate en/of Flashscore.

Geef een JSON object terug met twee arrays:

1. "bets": ALLE Linemate spelersprops. Elk item:
   - "player": naam (bijv. "S. Rinzel" of "Connor McDavid")
   - "sport": "NHL", "NBA", "MLB", "EPL", "La Liga", "Bundesliga", "Serie A" of "Ligue 1"
   - "team": teamafkorting indien zichtbaar (bijv. "CHI"), anders null
   - "bet_type": bijv. "Over 1.5 Shots on Goal" of "Anytime Goal Scorer"
   - "linemate_odds": odds als decimaal getal (number)
   - "hit_rate": percentage als decimaal: 100%→1.0, 92.3%→0.923 (number)
   - "sample": bijv. "12/13" (string)
   - "sample_n": totaal aantal wedstrijden als getal (number)

   BELANGRIJK: Extraheer ALLE spelers en props die zichtbaar zijn in de afbeelding,
   ook onderaan de lijst. Scroll mentaal door de hele afbeelding. Mis geen enkele prop.

2. "matches": ALLE wedstrijden zichtbaar in de screenshot. Elk item:
   - "home_team": volledige naam thuisploeg (string), bijv. "Florida Panthers"
   - "away_team": volledige naam uitploeg (string), bijv. "Minnesota Wild"
   - "sport": "NHL", "NBA", "MLB", of voetbalcompetitie zoals "EPL"
   - "competition": competitienaam indien zichtbaar (bijv. "NHL", "Premier League"), of null
   - "time": aanvangstijd indien zichtbaar (bijv. "00:00" of "01:00"), of null
   - "date": datum indien zichtbaar (bijv. "2025-03-25"), of null
   - "status": "gepland", "bezig" of "afgelopen"
   - "score": score indien zichtbaar (bijv. "2-1"), of null
   - "screenshot_odds": drie odds zichtbaar naast de wedstrijd als object
       {"home": 3.20, "draw": 4.20, "away": 1.95} of null als niet zichtbaar
   - "home_form": laatste 5 resultaten thuisploeg (bijv. "WWDLW"), of null
   - "away_form": idem voor uitploeg, of null
   - "h2h": korte H2H samenvatting indien zichtbaar (bijv. "Arsenal won 3/5"), of null

   LET OP: Een Flashscore NHL-overzichtsscherm toont MEERDERE wedstrijden met teamlogos
   en drie odds ernaast. Extraheer ELKE wedstrijd als apart item. Mis geen enkele wedstrijd.

Als er geen Linemate screenshots zijn, geef dan een lege array voor "bets".
Als er geen Flashscore screenshots zijn, geef dan een lege array voor "matches".
Geef ALLEEN het JSON object terug, geen andere tekst.
"""

FLASHSCORE_PROMPT = """
Je bent een expert sportsbetting analist. Analyseer de volgende wedstrijden en props in het Nederlands.

## WEDSTRIJDEN (Flashscore — verrijkt met API-data waar beschikbaar)
{matches_json}

## PROPS (Linemate — al gescoord)
{bets_json}

## INSTRUCTIES
- Als "home_form" of "away_form" gevuld zijn (bijv. "WWDLL"): gebruik deze data.
- Als form data null is maar je wel teamnamen hebt: geef aan "Beperkte data beschikbaar"
  en analyseer op basis van competitiecontext en bekende teamprestaties.
- Schrijf NOOIT "GEEN DATA" — altijd een redenering geven, ook bij beperkte data.
- Wees specifiek: noem altijd beide teamnamen en competitie.

## STAP 2 — FLASHSCORE ANALYSE
Geef een scoretabel voor de wedstrijden:
| Wedstrijd | Thuis vorm | Uit vorm | H2H | Advies |
|---|---|---|---|---|

Daarna: **Top 3 wedstrijden** om op te focussen, met 1-zin uitleg per wedstrijd.
Bij beperkte data: markeer met ⚠️ en geef een contextuele redenering.

{combo_section}

## STAP 5 — TE VERMIJDEN
- Welke wedstrijden vermijd je en waarom? (max 3 bullets)
- Welke props vermijd je en waarom? (max 3 bullets)

## DISCLAIMER
Dit is een statistische analyse ter ondersteuning van je eigen beslissing. Wedden brengt financiële risico's met zich mee. Speel verantwoord.
"""

COMBO_SECTION = """## STAP 4 — COMBINATIE ADVIES
Koppel de beste props aan de beste wedstrijden:
- Welke speler props passen bij de aanbevolen wedstrijden?
- Geef een definitief **Top 3 advies** met onderbouwing (speler + wedstrijd + motivatie)
"""

# ─── Pagina configuratie ──────────────────────────────────────────────────────

st.set_page_config(
    page_title="Bet Analyzer",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
/* ═══════════════════════════════════════════════════════
   DARK PRO THEME — BetAnalyzer
   Palette:
     bg-deep:    #08081a   (main background)
     bg-surface: #11112b   (cards / sidepanels)
     bg-raised:  #1a1a3e   (hover / nested)
     primary:    #7c3aed   (violet)
     primary-lg: #9d5ff5   (hover)
     glow:       rgba(124,58,237,0.18)
     text:       #dde0f5   (body)
     text-muted: #7070a0   (secondary)
     border:     #2a2a50   (subtle border)
     green:      #4ade80
     yellow:     #facc15
     red:        #f87171
═══════════════════════════════════════════════════════ */

/* ── Global background & typography ── */
.stApp {
  background: #08081a !important;
  color: #dde0f5 !important;
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
  border-bottom: 1px solid #2a2a50 !important;
}

/* ── App title / h1 ── */
h1 { color: #c4b5fd !important; letter-spacing: -0.5px; }
h2 { color: #a78bfa !important; }
h3 { color: #a78bfa !important; }
h4 { color: #c4b5fd !important; }

/* ── Markdown text ── */
p, li, label { color: #dde0f5 !important; }
.stMarkdown p { color: #dde0f5 !important; }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
  background: #11112b !important;
  border-radius: 12px !important;
  padding: 4px !important;
  gap: 2px !important;
  border: 1px solid #2a2a50 !important;
}
.stTabs [data-baseweb="tab"] {
  background: transparent !important;
  color: #7070a0 !important;
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
/* Remove default underline indicator */
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }
.stTabs [data-baseweb="tab-border"]    { display: none !important; }

/* ── Primary button (Analyseer) ── */
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
  color: #4a4a70 !important;
  box-shadow: none !important;
  transform: none !important;
}

/* ── Secondary / normal buttons ── */
.stButton > button[kind="secondary"],
.stButton > button:not([kind="primary"]) {
  background: #11112b !important;
  color: #c4b5fd !important;
  border: 1px solid #3a3a70 !important;
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
  color: #8080c0 !important;
}

/* ── Text inputs & password ── */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stSelectbox > div > div {
  background: #11112b !important;
  border: 1px solid #2a2a50 !important;
  border-radius: 8px !important;
  color: #dde0f5 !important;
}
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
  border-color: #7c3aed !important;
  box-shadow: 0 0 0 2px rgba(124,58,237,0.25) !important;
}

/* ── Metrics ── */
[data-testid="stMetric"] {
  background: #11112b !important;
  border: 1px solid #2a2a50 !important;
  border-radius: 10px !important;
  padding: 12px 16px !important;
}
[data-testid="stMetricValue"] { color: #c4b5fd !important; font-weight: 700 !important; }
[data-testid="stMetricLabel"] { color: #7070a0 !important; }
[data-testid="stMetricDelta"]  { font-weight: 600 !important; }

/* ── Success / Warning / Error alerts ── */
[data-testid="stAlert"][kind="success"],
.stSuccess > div {
  background: rgba(74,222,128,0.12) !important;
  border: 1px solid rgba(74,222,128,0.3) !important;
  border-radius: 10px !important;
  color: #4ade80 !important;
}
[data-testid="stAlert"][kind="warning"],
.stWarning > div {
  background: rgba(250,204,21,0.10) !important;
  border: 1px solid rgba(250,204,21,0.25) !important;
  border-radius: 10px !important;
  color: #facc15 !important;
}
[data-testid="stAlert"][kind="error"],
.stError > div {
  background: rgba(248,113,113,0.10) !important;
  border: 1px solid rgba(248,113,113,0.25) !important;
  border-radius: 10px !important;
  color: #f87171 !important;
}

/* ── Caption / small text ── */
.stCaption, [data-testid="stCaptionContainer"] { color: #6060a0 !important; }

/* ── Horizontal dividers ── */
hr { border-color: #2a2a50 !important; margin: 1.5rem 0 !important; }

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

/* ── Sidebar (if ever shown) ── */
[data-testid="stSidebar"] {
  background: #0d0d28 !important;
  border-right: 1px solid #2a2a50 !important;
}

/* ── Progress bar ── */
[data-testid="stProgressBar"] > div > div {
  background: linear-gradient(90deg, #5b21b6, #7c3aed) !important;
}

/* ── Select slider ── */
[data-testid="stSlider"] > div > div > div > div {
  background: #7c3aed !important;
}

/* ── Checkbox ── */
[data-testid="stCheckbox"] > label > div[role="checkbox"] {
  border-color: #5b21b6 !important;
}

/* ── Tooltip ── */
[data-testid="stTooltipIcon"] { color: #7070a0 !important; }
</style>
""", unsafe_allow_html=True)


# ─── Wachtwoord ───────────────────────────────────────────────────────────────

def _check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True
    st.markdown("""
<div style="
  background: linear-gradient(135deg, #12103a 0%, #1a1550 100%);
  border: 1px solid #3a2a70;
  border-radius: 16px;
  padding: 2rem 2.5rem 1.5rem;
  margin-bottom: 1.5rem;
  text-align: center;
  box-shadow: 0 8px 32px rgba(124,58,237,0.2);
">
  <div style="font-size:2.8rem; margin-bottom:0.3rem;">🎯</div>
  <div style="font-size:2rem; font-weight:800; color:#c4b5fd; letter-spacing:-0.5px;">Bet Analyzer</div>
  <div style="color:#7070a0; font-size:0.9rem; margin-top:0.5rem;">NHL · NBA · MLB · Voetbal</div>
</div>
""", unsafe_allow_html=True)
    pwd = st.text_input("Wachtwoord", type="password", key="pwd_input", label_visibility="collapsed", placeholder="🔒 Wachtwoord invoeren...")
    if st.button("Inloggen", use_container_width=True, type="primary"):
        try:
            correct = st.secrets.get("PASSWORD", "jullie_wachtwoord")
        except Exception:
            correct = "jullie_wachtwoord"
        if pwd == correct:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Onjuist wachtwoord.")
    return False


if not _check_password():
    st.stop()


# ─── Geschiedenis helpers (gedelegeerd aan db.py) ─────────────────────────────

def load_history() -> list:
    return db.load_history()

def save_to_history(enriched: list, alle_props: Optional[list] = None, parlay_suggesties: Optional[list] = None):
    db.save_to_history(enriched, alle_props=alle_props, parlay_suggesties=parlay_suggesties)


# ─── Extractie via Claude Haiku ───────────────────────────────────────────────

_EXTRACT_MODEL = "claude-haiku-4-5"
_MEDIA_MAP = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".webp": "image/webp"}


def _image_content_block(path: str) -> dict:
    """Bouw een Anthropic image-content-blok van een lokaal bestandspad."""
    media_type = _MEDIA_MAP.get(Path(path).suffix.lower(), "image/jpeg")
    with open(path, "rb") as fh:
        img_b64 = base64.b64encode(fh.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": img_b64},
    }


def _parse_json_from_text(text: str):
    """
    Probeert op 4 manieren een JSON-object uit de tekst te extraheren:
      1. Directe json.loads()
      2. Extraheer uit ```json … ``` blok
      3. Extraheer uit ``` … ``` blok
      4. Zoek het eerste volledige { … } object in de tekst
    Geeft None terug als niets werkt.
    """
    # Strategie 1: directe parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategie 2: ```json ... ```
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Strategie 3: ``` ... ```
    m = re.search(r"```\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Strategie 4: zoek eerste volledig JSON-object { ... }
    start = text.find("{")
    if start != -1:
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    return None


def _convert_heic_to_jpeg(path: str) -> str:
    """
    Converteert HEIC/HEIF naar JPEG. Geeft het nieuwe pad terug.
    Als conversie niet mogelijk is (geen PIL/heif), geeft het originele pad terug.
    """
    suffix = Path(path).suffix.lower()
    if suffix not in (".heic", ".heif"):
        return path
    if not _PIL_AVAILABLE:
        return path
    try:
        img = _PILImage.open(path)
        jpeg_path = path.rsplit(".", 1)[0] + "_converted.jpg"
        img = img.convert("RGB")
        img.save(jpeg_path, "JPEG", quality=90)
        return jpeg_path
    except Exception:
        return path


def _split_tall_image(path: str) -> list:
    """
    Als de afbeelding hoger is dan 1500px: splits in twee overlappende helften.
    Geeft [top_path, bottom_path] terug, anders [path].
    """
    if not _PIL_AVAILABLE:
        return [path]
    try:
        img = _PILImage.open(path)
        w, h = img.size
        if h <= 1500:
            return [path]
        # 10% overlap zodat props op de naad niet gemist worden
        overlap = int(h * 0.10)
        mid = h // 2

        top  = img.crop((0, 0,            w, mid + overlap))
        bot  = img.crop((0, mid - overlap, w, h))

        top_path = path + "_top.jpg"
        bot_path = path + "_bot.jpg"
        top.convert("RGB").save(top_path, "JPEG", quality=90)
        bot.convert("RGB").save(bot_path, "JPEG", quality=90)
        return [top_path, bot_path]
    except Exception:
        return [path]


def _deduplicate_bets(bets: list) -> list:
    """Verwijder dubbele bets op basis van (player, bet_type)."""
    seen = set()
    result = []
    for b in bets:
        key = (str(b.get("player", "")).lower(), str(b.get("bet_type", "")).lower())
        if key not in seen:
            seen.add(key)
            result.append(b)
    return result


def _deduplicate_matches(matches: list) -> list:
    """Verwijder dubbele wedstrijden op basis van (home_team, away_team)."""
    seen = set()
    result = []
    for m in matches:
        key = (
            str(m.get("home_team", "")).lower(),
            str(m.get("away_team", "")).lower(),
        )
        if key not in seen:
            seen.add(key)
            result.append(m)
    return result


def extract_bets(client, image_paths: list):
    """
    Stuurt afbeeldingen naar Claude Haiku en extraheert bets + matches als JSON.
    - Grote afbeeldingen (> 1500px) worden gesplitst in twee overlappende delen.
    - Slaat de ruwe response op in st.session_state['_dbg_raw'] voor debugging.
    """
    # Verwerk elke afbeelding: splits grote afbeeldingen in overlappende helften
    expanded_paths = []
    for p in image_paths:
        parts = _split_tall_image(p)
        expanded_paths.extend(parts)

    content = [_image_content_block(p) for p in expanded_paths]
    content.append({"type": "text", "text": EXTRACT_PROMPT})

    response = client.messages.create(
        model=_EXTRACT_MODEL,
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()

    # Sla ruwe response op zodat we kunnen debuggen als parsing mislukt
    st.session_state["_dbg_raw"]   = raw
    st.session_state["_dbg_model"] = _EXTRACT_MODEL

    data = _parse_json_from_text(raw)
    if data is None:
        # JSON niet gevonden — raw response opgeslagen voor diagnose
        return [], []
    if isinstance(data, list):
        return _deduplicate_bets(data), []
    bets    = _deduplicate_bets(data.get("bets", []))
    matches = _deduplicate_matches(data.get("matches", []))

    # Debug: log hit_rate per prop voor diagnosedoeleinden
    st.session_state["_dbg_hit_rates"] = [
        {
            "player":   b.get("player", "?"),
            "bet_type": b.get("bet_type", "?"),
            "hit_rate": b.get("hit_rate"),   # None = niet gevonden
            "odds":     b.get("linemate_odds"),
            "sample":   b.get("sample"),
        }
        for b in bets
    ]

    return bets, matches


# ─── Soccer wedstrijd-verrijking met vorm-data ────────────────────────────────

def _enrich_soccer_matches_form(matches: list) -> list:
    """
    Probeert thuis- en uitploeg vorm-data op te halen via de Football-Data API.
    Als de API-key niet beschikbaar is of een team niet gevonden wordt,
    worden de wedstrijden ongewijzigd teruggegeven.
    """
    if not getattr(soccer, "API_KEY", ""):
        return matches

    enriched = []
    for m in matches:
        m = dict(m)   # werkkopie

        # Al verrijkt? Sla over.
        if m.get("home_form") and m.get("away_form"):
            enriched.append(m)
            continue

        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        comp_raw  = (m.get("competition") or "").strip()
        # Gebruik beste gok voor competitie als die ontbreekt
        comp = comp_raw if comp_raw else "EPL"

        # Thuisploeg
        if not m.get("home_form") and home_name:
            try:
                stats = soccer.get_team_stats_for_match(home_name, comp)
                if stats.get("form"):
                    m["home_form"]       = stats["form"]
                if stats.get("avg_goals_for"):
                    m["home_gf_avg"]     = stats["avg_goals_for"]
                if stats.get("avg_goals_against"):
                    m["home_ga_avg"]     = stats["avg_goals_against"]
            except Exception:
                pass

        # Uitploeg
        if not m.get("away_form") and away_name:
            try:
                stats = soccer.get_team_stats_for_match(away_name, comp)
                if stats.get("form"):
                    m["away_form"]       = stats["form"]
                if stats.get("avg_goals_for"):
                    m["away_gf_avg"]     = stats["avg_goals_for"]
                if stats.get("avg_goals_against"):
                    m["away_ga_avg"]     = stats["avg_goals_against"]
            except Exception:
                pass

        enriched.append(m)
    return enriched


# ─── Flashscore analyse via Claude Haiku ──────────────────────────────────────

def analyze_flashscore(client, matches: list, enriched_bets: list) -> str:
    has_bets = bool(enriched_bets)
    bets_summary = [{
        "player":    b.get("player"),
        "sport":     b.get("sport"),
        "bet_type":  b.get("bet_type"),
        "odds":      b.get("odds"),
        "ev":        b.get("ev"),
        "rating":    b.get("rating"),
        "composite": b.get("composite"),
    } for b in enriched_bets]

    prompt = FLASHSCORE_PROMPT.format(
        matches_json=json.dumps(matches, ensure_ascii=False, indent=2),
        bets_json=json.dumps(bets_summary, ensure_ascii=False, indent=2) if has_bets else "(geen props)",
        combo_section=COMBO_SECTION if has_bets else "",
    )
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ─── NHL Wedstrijd-analyse (1X2 three-way) ────────────────────────────────────

import math as _math

_LEAGUE_GF_AVG  = 3.05   # NHL-seizoensgemiddelde goals per team per wedstrijd
_HOME_ICE_FACTOR = 1.08  # thuisploeg scoort ~8% meer in NHL
_OT_BASE_RATE   = 0.235  # ~23.5% van NHL-wedstrijden gaat naar OT/SO


def _poisson_p(k: int, lam: float) -> float:
    """P(X = k) voor Poisson-verdeling met parameter lam."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (_math.exp(-lam) * lam ** k) / _math.factorial(k)


def _nhl_match_probs(home_form: dict, away_form: dict) -> dict:
    """
    Bereken 1X2 kansen via een eenvoudig Poisson-model op basis van team stats.

    Geeft terug:
      p_home  — kans thuiswinst (regulatie)
      p_draw  — kans OT/SO (gelijkspel na regulatie)
      p_away  — kans uitwinst (regulatie)
      lH, lA  — verwachte goals thuis / uit
    """
    # Verwachte goals per team (Dixon-Coles benadering zonder Dixon-Coles)
    lH = home_form.get("gf_avg", _LEAGUE_GF_AVG) * \
         (away_form.get("ga_avg", _LEAGUE_GF_AVG) / _LEAGUE_GF_AVG) * \
         _HOME_ICE_FACTOR
    lA = away_form.get("gf_avg", _LEAGUE_GF_AVG) * \
         (home_form.get("ga_avg", _LEAGUE_GF_AVG) / _LEAGUE_GF_AVG)

    # Begrens op realistisch bereik
    lH = max(1.5, min(lH, 5.0))
    lA = max(1.5, min(lA, 5.0))

    p_home = p_draw = p_away = 0.0
    for h in range(9):
        for a in range(9):
            p = _poisson_p(h, lH) * _poisson_p(a, lA)
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p

    # Normaliseer (afrondingsfouten)
    tot = p_home + p_draw + p_away or 1.0
    return {
        "p_home": round(p_home / tot, 4),
        "p_draw": round(p_draw / tot, 4),
        "p_away": round(p_away / tot, 4),
        "lH":     round(lH, 2),
        "lA":     round(lA, 2),
    }


def _match_ev(model_prob: float, odds: float) -> float:
    """EV = p × (odds - 1) - (1 - p)"""
    if not odds or odds <= 1.0:
        return -1.0
    return round(model_prob * (odds - 1) - (1 - model_prob), 4)


def _match_rating(ev_val: float) -> str:
    if ev_val >= 0.05:
        return "✅ Waarde"
    if ev_val >= -0.05:
        return "⚠️ Neutraal"
    return "❌ Vermijd"


def analyze_nhl_matches(matches: list) -> list:
    """
    Analyseert een lijst van NHL-wedstrijden (geëxtraheerd uit Flashscore screenshot).

    Voor elke wedstrijd:
      1. Team form ophalen (NHL API standings)
      2. 1X2 odds ophalen van Bet365 (via Odds API; screenshot-odds als fallback)
      3. Poisson kansen berekenen
      4. EV en rating per optie (thuis / OT / uit) berekenen

    Geeft lijst van match-analyse dicts terug.
    """
    results = []
    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper()
        if "NHL" not in sport and "HOCKEY" not in sport:
            continue  # alleen NHL

        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        if not home_name or not away_name:
            continue

        # 1. Team form (NHL API)
        home_form = {}
        away_form = {}
        try:
            home_form = nhl.get_team_form(home_name)
        except Exception:
            pass
        try:
            away_form = nhl.get_team_form(away_name)
        except Exception:
            pass

        # 2. Odds ophalen: Bet365 API → screenshot → None
        scr_odds   = m.get("screenshot_odds") or {}
        b365_odds  = {}
        odds_bron  = "screenshot"
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                b365_odds = odds_api.get_match_odds_h2h("NHL", home_name, away_name)
                if b365_odds.get("source") == "bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass

        def _odds(key_b365, key_scr):
            v = b365_odds.get(key_b365) or scr_odds.get(key_scr)
            return float(v) if v else None

        home_odds = _odds("home_odds", "home")
        draw_odds = _odds("draw_odds", "draw")
        away_odds = _odds("away_odds", "away")

        # 3. Poisson kansen
        probs = {}
        if home_form and away_form:
            probs = _nhl_match_probs(home_form, away_form)
        elif home_form or away_form:
            # Één team onbekend — gebruik league-gemiddelden
            fallback = {
                "gf_avg": _LEAGUE_GF_AVG,
                "ga_avg": _LEAGUE_GF_AVG,
            }
            probs = _nhl_match_probs(
                home_form or fallback,
                away_form or fallback,
            )

        # 4. EV per optie
        def _option(prob_key, odds_val, label):
            p = probs.get(prob_key, 0.0)
            ev_val = _match_ev(p, odds_val) if odds_val else None
            return {
                "label":  label,
                "prob":   p,
                "odds":   odds_val,
                "ev":     ev_val,
                "rating": _match_rating(ev_val) if ev_val is not None else "—",
            }

        options = [
            _option("p_home", home_odds, f"🏠 {home_name} wint"),
            _option("p_draw", draw_odds, "🔄 OT / SO"),
            _option("p_away", away_odds, f"✈️ {away_name} wint"),
        ]

        # Beste optie op basis van EV
        best = max(
            (o for o in options if o["ev"] is not None),
            key=lambda o: o["ev"],
            default=None,
        )

        results.append({
            "home_team":  home_name,
            "away_team":  away_name,
            "time":       m.get("time"),
            "home_form":  home_form,
            "away_form":  away_form,
            "probs":      probs,
            "odds_bron":  odds_bron,
            "options":    options,
            "best":       best,
        })

    return results


# ─── Scenario detectie ────────────────────────────────────────────────────────

def detect_scenario(bets: list, matches: list) -> int:
    """
    1 = Alleen Flashscore  → auto-props van schema
    2 = Flashscore + Linemate → gecombineerde scoring
    3 = Alleen Linemate (of niets)
    """
    if matches and not bets:
        return 1
    if matches and bets:
        return 2
    return 3


# ─── Sport detectie uit wedstrijden ───────────────────────────────────────────

def _detect_sports_from_matches(matches: list) -> set:
    """Detecteer welke sporten aanwezig zijn op basis van competitienaam."""
    sports = set()
    for m in matches:
        comp = (m.get("competition") or "").lower()
        if not comp:
            continue
        if any(k in comp for k in ("nhl", "hockey")):
            sports.add("NHL")
        elif any(k in comp for k in ("nba", "basketball")):
            sports.add("NBA")
        elif any(k in comp for k in ("mlb", "baseball")):
            sports.add("MLB")
        elif any(k in comp for k in (
            "premier", "la liga", "bundesliga", "serie a", "ligue", "epl"
        )):
            sports.add("SOCCER")
    # Als geen sport herkend → probeer NHL + NBA + MLB (meest voorkomend)
    if not sports:
        sports = {"NHL", "NBA", "MLB"}
    return sports


# ─── Auto-props genereren (Scenario 1) ────────────────────────────────────────

def _nhl_auto_props(progress_cb=None) -> list:
    """NHL props voor alle spelers die vandaag spelen."""
    try:
        today_teams = nhl.get_today_teams()
    except Exception:
        return []
    if not today_teams:
        return []

    props = []
    for team_abbrev in today_teams:
        try:
            players = nhl.get_team_players(team_abbrev, n=7)
        except Exception:
            continue
        for p in players:
            if progress_cb:
                progress_cb(f"NHL — {p['name']} ({team_abbrev})")
            try:
                stats = nhl.get_player_stats(p["id"])
            except Exception:
                continue

            avg_shots = stats.get("avg_shots") or stats.get("hist_shots_avg", 0)
            avg_goals = stats.get("avg_goals") or stats.get("hist_goals_avg", 0)
            games_n   = stats.get("games_sampled", 0)

            # Kies schot-lijn vlak onder het gemiddelde
            for line in (4.5, 3.5, 2.5, 1.5):
                if avg_shots > line:
                    props.append({
                        "player":        p["name"],
                        "sport":         "NHL",
                        "team":          team_abbrev,
                        "bet_type":      f"Over {line} Shots on Goal",
                        "linemate_odds": _REF_ODDS["shots"],
                        "hit_rate":      None,
                        "sample":        "auto",
                        "sample_n":      games_n,
                    })
                    break  # één shot-prop per speler

            if avg_goals >= 0.18:
                props.append({
                    "player":        p["name"],
                    "sport":         "NHL",
                    "team":          team_abbrev,
                    "bet_type":      "Anytime Goal Scorer",
                    "linemate_odds": _REF_ODDS["anytime"],
                    "hit_rate":      None,
                    "sample":        "auto",
                    "sample_n":      games_n,
                })
    return props


def _nba_auto_props(progress_cb=None) -> list:
    """NBA props voor spelers die vandaag spelen (max 4 wedstrijden × 3 spelers)."""
    try:
        today_games = nba.get_today_games()
    except Exception:
        return []
    if not today_games:
        return []

    team_ids_done: set = set()
    props: list = []

    for game in today_games[:4]:
        for tid in (game.get("home_team_id"), game.get("away_team_id")):
            if not tid or tid in team_ids_done:
                continue
            team_ids_done.add(tid)
            try:
                players = nba.get_team_players(tid, n=3)
            except Exception:
                continue
            for p in players:
                if progress_cb:
                    progress_cb(f"NBA — {p['name']}")
                try:
                    stats = nba.get_player_stats(p["id"])
                except Exception:
                    continue

                avg_pts = stats.get("avg_points", 0)
                avg_reb = stats.get("avg_rebounds", 0)
                games_n = stats.get("games_sampled", 0)

                if avg_pts >= 15:
                    # lijn ≈ 80% van gemiddelde, afgerond op .5
                    import math
                    line = math.floor(avg_pts * 0.82) + 0.5
                    props.append({
                        "player": p["name"], "sport": "NBA", "team": str(tid),
                        "bet_type": f"Over {line:.1f} Points",
                        "linemate_odds": _REF_ODDS["points"], "hit_rate": None,
                        "sample": "auto", "sample_n": games_n,
                    })
                if avg_reb >= 6:
                    line = math.floor(avg_reb * 0.80) + 0.5
                    props.append({
                        "player": p["name"], "sport": "NBA", "team": str(tid),
                        "bet_type": f"Over {line:.1f} Rebounds",
                        "linemate_odds": _REF_ODDS["rebounds"], "hit_rate": None,
                        "sample": "auto", "sample_n": games_n,
                    })
    return props


def _mlb_auto_props(progress_cb=None) -> list:
    """MLB props voor batters die vandaag spelen (max 4 wedstrijden × 4 batters)."""
    try:
        today_games = mlb.get_today_games()
    except Exception:
        return []
    if not today_games:
        return []

    team_ids_done: set = set()
    props: list = []

    for game in today_games[:4]:
        for tid in (game.get("home_team_id"), game.get("away_team_id")):
            if not tid or tid in team_ids_done:
                continue
            team_ids_done.add(tid)
            try:
                players = mlb.get_team_players(tid, n=4)
            except Exception:
                continue
            for p in players:
                if progress_cb:
                    progress_cb(f"MLB — {p['name']}")
                try:
                    stats = mlb.get_player_stats(p["id"])
                except Exception:
                    continue

                avg_hits = stats.get("avg_hits", 0)
                avg_tb   = stats.get("avg_total_bases", 0)
                games_n  = stats.get("games_sampled", 0)

                if avg_hits >= 0.75:
                    props.append({
                        "player": p["name"], "sport": "MLB", "team": str(tid),
                        "bet_type": "Over 0.5 Hits",
                        "linemate_odds": _REF_ODDS["hits"], "hit_rate": None,
                        "sample": "auto", "sample_n": games_n,
                    })
                if avg_tb >= 1.4:
                    props.append({
                        "player": p["name"], "sport": "MLB", "team": str(tid),
                        "bet_type": "Over 1.5 Total Bases",
                        "linemate_odds": _REF_ODDS["total_bases"], "hit_rate": None,
                        "sample": "auto", "sample_n": games_n,
                    })
    return props


def generate_auto_props(matches: list, progress_cb=None) -> list:
    """
    Genereer automatische props voor sporten gedetecteerd uit Flashscore wedstrijden.
    Enkel gebruikt in Scenario 1 (geen Linemate).
    """
    sports = _detect_sports_from_matches(matches)
    all_props: list = []

    if "NHL" in sports:
        if progress_cb:
            progress_cb("🏒 NHL schema + spelersstats ophalen...")
        all_props.extend(_nhl_auto_props(progress_cb))

    if "NBA" in sports:
        if progress_cb:
            progress_cb("🏀 NBA schema + spelersstats ophalen...")
        all_props.extend(_nba_auto_props(progress_cb))

    if "MLB" in sports:
        if progress_cb:
            progress_cb("⚾ MLB schema + spelersstats ophalen...")
        all_props.extend(_mlb_auto_props(progress_cb))

    if "SOCCER" in sports and progress_cb:
        progress_cb("⚽ Voetbal: wedstrijdanalyse via Flashscore (automatische props niet beschikbaar)")

    return all_props


# ─── Bet verrijken ────────────────────────────────────────────────────────────

def enrich_bet(bet: dict, cache: dict,
               linemate_weight: float = 0.35,
               season_weight:   float = 0.35) -> dict:
    sport       = (bet.get("sport") or "").upper().strip()
    player_name = bet.get("player", "")
    team_hint   = bet.get("team") or ""
    bet_type    = bet.get("bet_type", "")
    sample_n    = bet.get("sample_n") or 5

    player_stats   = {}
    opponent_stats = {}
    opponent_name  = None

    cache_key = f"{sport}::{player_name}"

    if cache_key in cache:
        cached         = cache[cache_key]
        player_stats   = cached.get("player_stats", {})
        opponent_name  = cached.get("opponent")
        opponent_stats = cached.get("opponent_stats", {})
    else:
        try:
            if sport == "NHL":
                player_id, team = nhl.find_player(player_name)
                if player_id:
                    player_stats  = nhl.get_player_stats(player_id)
                    opponent_name = nhl.get_opponent(team) if team else None
                    if opponent_name:
                        opponent_stats = nhl.get_team_defense(opponent_name)
            elif sport == "NBA":
                player = nba.find_player(player_name)
                if player:
                    player_stats = nba.get_player_stats(player["id"])
            elif sport == "MLB":
                player = mlb.find_player(player_name)
                if player:
                    pos_code = player.get("primaryPosition", {}).get("code", "")
                    pos_type = "pitching" if pos_code == "1" else "hitting"
                    player_stats = mlb.get_player_stats(player.get("id"), position_type=pos_type)
            elif sport in SOCCER_COMPS:
                comp = sport if sport != "VOETBAL" else "EPL"
                player = soccer.find_player(player_name, team_hint=team_hint, competition=comp)
                if player:
                    player_stats   = soccer.get_player_stats(player.get("id"), player.get("team_id"), comp)
                    opponent_stats = soccer.get_team_defense(player.get("team_id")) if player.get("team_id") else {}
        except Exception as e:
            st.warning(f"⚠️ Datafout voor {player_name}: {e}")

        cache[cache_key] = {
            "player_stats":   player_stats,
            "opponent":       opponent_name,
            "opponent_stats": opponent_stats,
        }

    # Hit rate — None = niet geëxtraheerd uit screenshot (auto-prop of Haiku miste het)
    _raw_hr   = bet.get("hit_rate")   # None als ontbrekend, 0.0 als expliciet 0%
    _hr_ok    = _raw_hr is not None   # True als Claude Haiku een waarde teruggaf
    _lm_hr    = float(_raw_hr) if _hr_ok else 0.0
    # Als hit_rate ontbreekt: zet linemate_weight op 0 zodat het EV niet beïnvloedt
    _eff_lm_w = linemate_weight if _hr_ok else 0.0

    odds  = bet.get("linemate_odds", 1.0)
    score = composite_score(
        linemate_hit_rate=_lm_hr,
        sample_size=sample_n,
        bet_type=bet_type,
        player_stats=player_stats,
        opponent_stats=opponent_stats,
        sport=sport,
        linemate_weight=_eff_lm_w,
        season_weight=season_weight,
    )
    ev_score = ev(score["composite"], odds)
    rat      = rating(ev_score, score["composite"])

    return {
        "player":         player_name,
        "sport":          bet.get("sport", "?"),
        "team":           team_hint,
        "bet_type":       bet_type,
        "odds":           odds,
        "sample":         bet.get("sample", "?"),
        "linemate_hr":    score["linemate_hr"],
        "season_hr":      score["season_hr"],
        "composite":      score["composite"],
        "ev":             ev_score,
        "rating":         rat,
        "opponent":       opponent_name,
        "gaa":            opponent_stats.get("goals_against_avg"),
        "source":         player_stats.get("source", ""),
        "bet365":         {},   # wordt ingevuld na enrichment
        "no_linemate_hr": not _hr_ok,  # True als geen hit_rate uit screenshot
    }


# ─── Favorieten & Resultaten helpers (gedelegeerd aan db.py) ──────────────────

def _make_fav_id(player: str, bet_type: str) -> str:
    return db.make_fav_id(player, bet_type)

def load_favorieten() -> list:
    return db.load_favorieten()

def save_favorieten(favs: list) -> None:
    db.save_favorieten(favs)

def add_favoriet(bet: dict) -> None:
    fid = _make_fav_id(bet["player"], bet["bet_type"])
    db.add_favoriet(fid, bet)

def remove_favoriet(fav_id: str) -> None:
    db.remove_favoriet(fav_id)

def load_resultaten() -> list:
    return db.load_resultaten()

def save_resultaten(results: list) -> None:
    db.save_resultaten(results)

def upsert_resultaat(fav_id: str, fav: dict, uitkomst: str, inzet: float) -> None:
    db.upsert_resultaat(fav_id, fav, uitkomst, inzet)

def remove_resultaat(fav_id: str) -> None:
    db.remove_resultaat(fav_id)


# ─── Resultaten renderen ──────────────────────────────────────────────────────

SPORT_ICONS = {"NHL": "🏒", "NBA": "🏀", "MLB": "⚾"}

def _rating_color(rat: str) -> str:
    if "Sterk" in rat:
        return "green"
    if "Matig" in rat:
        return "orange"
    return "red"


def render_flashscore(text: str):
    st.markdown("---")
    st.markdown("### 📺 Flashscore Analyse")
    st.markdown(text)


def render_nhl_match_cards(match_analyses: list):
    """Toon kaartjes voor geanalyseerde NHL-wedstrijden."""
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### 🏒 NHL Wedstrijd-analyse")

    # Top 3 aanbevolen wedstrijden (gesorteerd op beste EV)
    _top3 = sorted(
        [ma for ma in match_analyses if ma.get("best") and ma["best"].get("ev") is not None],
        key=lambda ma: ma["best"]["ev"],
        reverse=True,
    )[:3]

    if _top3:
        st.markdown("#### 🏆 Top 3 aanbevolen NHL wedstrijden")
        for _i, _ma in enumerate(_top3, 1):
            _hf  = _ma.get("home_form", {})
            _af  = _ma.get("away_form", {})
            _b   = _ma["best"]
            _ev_s = f"+{_b['ev']:.3f}" if _b["ev"] >= 0 else f"{_b['ev']:.3f}"
            _h_l10 = _hf.get("last10", "—") if _hf else "—"
            _a_l10 = _af.get("last10", "—") if _af else "—"
            _h_gf  = _hf.get("gf_avg", 0) if _hf else 0
            _a_gf  = _af.get("gf_avg", 0) if _af else 0
            _h_rec = _hf.get("home_record", "—") if _hf else "—"
            _a_rec = _af.get("road_record", "—") if _af else "—"
            _streak_h = _hf.get("streak", "") if _hf else ""
            _streak_a = _af.get("streak", "") if _af else ""
            st.markdown(
                f"**{_i}. {_ma['home_team']} vs {_ma['away_team']}**  \n"
                f"Beste inzet: {_b['label']} \u00a0·\u00a0 EV `{_ev_s}` \u00a0·\u00a0 {_b['rating']}  \n"
                f"Thuis L10: `{_h_l10}` | Uit L10: `{_a_l10}`  \n"
                f"Thuisrecord: {_h_rec} | Uitrecord: {_a_rec}  \n"
                f"Gem. goals: {_h_gf:.2f} \u2013 {_a_gf:.2f}"
                + (f"  \nReeks: {_ma['home_team']} {_streak_h} | {_ma['away_team']} {_streak_a}"
                   if _streak_h or _streak_a else "")
            )
        st.markdown("---")

    for ma in match_analyses:
        home = ma["home_team"]
        away = ma["away_team"]
        time_str = ma.get("time") or ""
        home_f   = ma.get("home_form", {})
        away_f   = ma.get("away_form", {})
        probs    = ma.get("probs", {})
        best     = ma.get("best")
        odds_src = ma.get("odds_bron", "")

        with st.container():
            st.markdown(
                "<div style='background:#11112b;border:1px solid #2a2a58;"
                "border-radius:12px;padding:16px;margin-bottom:14px;'>",
                unsafe_allow_html=True,
            )

            # Header: teams + tijd
            hcol, tcol = st.columns([4, 1])
            with hcol:
                st.markdown(f"#### 🏒 {home}  vs  {away}")
            with tcol:
                if time_str:
                    st.markdown(f"<div style='text-align:right;color:#8080b0;padding-top:8px'>"
                                f"⏰ {time_str}</div>", unsafe_allow_html=True)

            # Team stats vergelijking
            if home_f or away_f:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if home_f:
                        st.metric(f"🏠 {home_f.get('abbrev', home[:3])}", "")
                        st.caption(
                            f"Punten: {home_f.get('points',0)} ({home_f.get('points_pct',0):.1%})  \n"
                            f"L10: {home_f.get('last10','—')}  \n"
                            f"Reeks: {home_f.get('streak','—')}  \n"
                            f"Thuis: {home_f.get('home_record','—')}"
                        )
                with c2:
                    if home_f and away_f:
                        lH = probs.get("lH", 0)
                        lA = probs.get("lA", 0)
                        st.markdown(
                            f"<div style='text-align:center;padding-top:12px;color:#8080b0;'>"
                            f"<div>xG: {lH:.2f} – {lA:.2f}</div>"
                            f"<div style='font-size:0.85rem;margin-top:4px'>"
                            f"GF/GA: {home_f.get('gf_avg',0):.2f}/{home_f.get('ga_avg',0):.2f}"
                            f" · {away_f.get('gf_avg',0):.2f}/{away_f.get('ga_avg',0):.2f}</div>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                with c3:
                    if away_f:
                        st.metric(f"✈️ {away_f.get('abbrev', away[:3])}", "")
                        st.caption(
                            f"Punten: {away_f.get('points',0)} ({away_f.get('points_pct',0):.1%})  \n"
                            f"L10: {away_f.get('last10','—')}  \n"
                            f"Reeks: {away_f.get('streak','—')}  \n"
                            f"Uit: {away_f.get('road_record','—')}"
                        )

            st.markdown("---")

            # Drie bet-opties als kolommen
            opt_cols = st.columns(3)
            for col, opt in zip(opt_cols, ma["options"]):
                with col:
                    prob_pct = f"{opt['prob']*100:.1f}%"
                    odds_str = f"{opt['odds']:.2f}" if opt["odds"] else "—"
                    ev_val   = opt["ev"]
                    ev_str   = f"+{ev_val:.3f}" if ev_val and ev_val >= 0 else (
                                f"{ev_val:.3f}" if ev_val is not None else "—")
                    rat      = opt["rating"]
                    is_best  = (best and opt["label"] == best["label"]
                                and ev_val is not None and ev_val >= 0.0)

                    border_col = "#4ade80" if "Waarde" in rat else (
                                 "#facc15" if "Neutraal" in rat else "#f87171")
                    bg_col     = "#081a10" if "Waarde" in rat else (
                                 "#1a1500" if "Neutraal" in rat else "#1a0808")

                    best_badge = " ⭐ Beste" if is_best else ""
                    st.markdown(
                        f"<div style='background:{bg_col};border:1px solid {border_col};"
                        f"border-radius:8px;padding:10px;text-align:center;'>"
                        f"<div style='font-size:0.8rem;color:#8080b0;margin-bottom:4px'>"
                        f"{opt['label']}{best_badge}</div>"
                        f"<div style='font-size:1.1rem;font-weight:700;color:#fff'>"
                        f"Odds: {odds_str}</div>"
                        f"<div style='color:#8080b0;font-size:0.85rem'>Model: {prob_pct}</div>"
                        f"<div style='font-size:1.0rem;font-weight:700;"
                        f"color:{border_col}'>EV {ev_str}</div>"
                        f"<div style='font-size:0.8rem;color:{border_col}'>{rat}</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

            # Odds bron + opslaan knop
            st.markdown(
                f"<div style='color:#6060a0;font-size:0.75rem;margin-top:8px;'>"
                f"Odds bron: {odds_src}  ·  "
                f"Model: Poisson (xG {probs.get('lH',0):.2f}–{probs.get('lA',0):.2f})</div>",
                unsafe_allow_html=True,
            )

            # ⭐ Opslaan in favorieten (beste optie)
            if best and best.get("odds") and best.get("ev") is not None:
                fav_key  = f"nhl_match_{home[:3]}_{away[:3]}_{time_str}"
                fav_bet  = {
                    "player":   f"{home} vs {away}",
                    "sport":    "NHL",
                    "team":     home[:3].upper(),
                    "bet_type": best["label"].replace("🏠 ", "").replace("✈️ ", "").replace("🔄 ", ""),
                    "odds":     best["odds"],
                    "ev":       best["ev"],
                    "rating":   best["rating"],
                    "composite": best["prob"],
                    "linemate_hr": best["prob"],
                    "season_hr":   best["prob"],
                    "sample":  "NHL API",
                    "source":  "NHL Standings",
                }
                _fav_ids = {f["id"] for f in load_favorieten()}
                _fid     = _make_fav_id(fav_bet["player"], fav_bet["bet_type"])
                _is_fav  = _fid in _fav_ids
                _btn_lbl = "⭐ Opgeslagen" if _is_fav else "☆ Opslaan in Favorieten"
                if not _is_fav:
                    if st.button(_btn_lbl, key=f"fav_match_{fav_key}",
                                 use_container_width=True):
                        add_favoriet(fav_bet)
                        st.rerun()
                else:
                    st.markdown(
                        f"<div style='color:#4ade80;text-align:center;padding:4px'>"
                        f"⭐ Opgeslagen in Favorieten</div>",
                        unsafe_allow_html=True,
                    )

            st.markdown("</div>", unsafe_allow_html=True)


# ─── Soccer (voetbal) match analyse ─────────────────────────────────────────

_SOCCER_LEAGUE_AVG   = 1.35   # gemiddelde goals per team per wedstrijd (EPL ~1.35)
_SOCCER_HOME_FACTOR  = 1.15   # thuisvoordeel ~15%


def _soccer_match_probs(home_form: dict, away_form: dict) -> dict:
    """Poisson-model voor voetbal 1X2 (thuiswinst / gelijkspel / uitwinst)."""
    lH = home_form.get("gf_avg", _SOCCER_LEAGUE_AVG) * \
         (away_form.get("ga_avg", _SOCCER_LEAGUE_AVG) / _SOCCER_LEAGUE_AVG) * \
         _SOCCER_HOME_FACTOR
    lA = away_form.get("gf_avg", _SOCCER_LEAGUE_AVG) * \
         (home_form.get("ga_avg", _SOCCER_LEAGUE_AVG) / _SOCCER_LEAGUE_AVG)
    lH = max(0.4, min(lH, 5.0))
    lA = max(0.4, min(lA, 5.0))
    p_home = p_draw = p_away = 0.0
    for h in range(8):
        for a in range(8):
            p = _poisson_p(h, lH) * _poisson_p(a, lA)
            if h > a:   p_home += p
            elif h == a: p_draw += p
            else:        p_away += p
    tot = p_home + p_draw + p_away or 1.0
    return {"p_home": round(p_home/tot,4), "p_draw": round(p_draw/tot,4),
            "p_away": round(p_away/tot,4), "lH": round(lH,2), "lA": round(lA,2)}


def _soccer_form_from_api(team_name: str, competition: str) -> dict:
    try:
        comp = competition.upper() if competition else "EPL"
        raw  = soccer.get_team_stats_for_match(team_name, comp)
        if not raw:
            return {}
        return {"full_name": raw.get("name", team_name),
                "abbrev":    raw.get("name", team_name)[:3].upper(),
                "gf_avg":    raw.get("avg_goals_for", _SOCCER_LEAGUE_AVG),
                "ga_avg":    raw.get("avg_goals_against", _SOCCER_LEAGUE_AVG),
                "form":      raw.get("form", ""), "last10": raw.get("form", ""),
                "streak": "", "home_record": "—", "road_record": "—"}
    except Exception:
        return {}


def analyze_soccer_matches(matches: list) -> list:
    soccer_sports = {"EPL", "PREMIERLEAGUE", "LALIGA", "BUNDESLIGA",
                     "SERIEA", "LIGUE1", "VOETBAL", "SOCCER", "UCL"}
    results = []
    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper().replace(" ", "")
        is_soccer = any(s.replace(" ","") in sport for s in soccer_sports) or \
                    not any(x in sport for x in ("NHL","HOCKEY","NBA","BASKETBALL","MLB","BASEBALL"))
        if not is_soccer:
            continue
        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        if not home_name or not away_name:
            continue
        competition = m.get("competition") or m.get("sport") or "EPL"
        home_form = _soccer_form_from_api(home_name, competition)
        away_form = _soccer_form_from_api(away_name, competition)
        fallback  = {"gf_avg": _SOCCER_LEAGUE_AVG, "ga_avg": _SOCCER_LEAGUE_AVG}
        probs = _soccer_match_probs(home_form or fallback, away_form or fallback)
        scr_odds = m.get("screenshot_odds") or {}
        odds_bron = "screenshot"
        b365 = {}
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                sp_map = {"EPL":"EPL","PREMIERLEAGUE":"EPL","LALIGA":"LALIGA",
                          "BUNDESLIGA":"BUNDESLIGA","SERIEA":"SERIEA",
                          "LIGUE1":"LIGUE1","UCL":"UCL"}
                sp = sp_map.get(competition.upper().replace(" ",""), "EPL")
                b365 = odds_api.get_match_odds_h2h(sp, home_name, away_name)
                if b365.get("source") == "bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass
        home_odds = b365.get("home_odds") if odds_bron=="Bet365" else scr_odds.get("home")
        draw_odds = b365.get("draw_odds") if odds_bron=="Bet365" else scr_odds.get("draw")
        away_odds = b365.get("away_odds") if odds_bron=="Bet365" else scr_odds.get("away")

        def _opt(pk, ov, lbl):
            p = probs.get(pk, 0.0)
            ev = _match_ev(p, ov) if ov else None
            return {"label": lbl, "prob": p, "odds": ov,
                    "ev": ev, "rating": _match_rating(ev) if ev is not None else "—"}

        options = [_opt("p_home", home_odds, f"🏠 {home_name} wint"),
                   _opt("p_draw", draw_odds, "🤝 Gelijkspel"),
                   _opt("p_away", away_odds, f"✈️ {away_name} wint")]
        best = max((o for o in options if o["ev"] is not None),
                   key=lambda o: o["ev"], default=None)
        results.append({"home_team": home_name, "away_team": away_name,
                        "time": m.get("time"), "competition": competition,
                        "home_form": home_form, "away_form": away_form,
                        "probs": probs, "odds_bron": odds_bron,
                        "options": options, "best": best})
    return results


def _render_match_option_box(col, opt, best):
    with col:
        ev_val  = opt["ev"]
        ev_str  = f"+{ev_val:.3f}" if ev_val and ev_val >= 0 else (f"{ev_val:.3f}" if ev_val is not None else "—")
        rat     = opt["rating"]
        is_best = best and opt["label"] == best["label"] and ev_val is not None and ev_val >= 0
        bc = "#4ade80" if "Waarde" in rat else ("#facc15" if "Neutraal" in rat else "#f87171")
        bg = "#081a10" if "Waarde" in rat else ("#1a1500" if "Neutraal" in rat else "#1a0808")
        st.markdown(
            f"<div style='background:{bg};border:1px solid {bc};"
            f"border-radius:8px;padding:10px;text-align:center;'>"
            f"<div style='font-size:0.75rem;color:#8080b0;margin-bottom:4px'>"
            f"{opt['label']}{'  ⭐' if is_best else ''}</div>"
            f"<div style='font-size:1.1rem;font-weight:700;color:#fff'>"
            f"Odds: {'{:.2f}'.format(opt['odds']) if opt['odds'] else '—'}</div>"
            f"<div style='color:#8080b0;font-size:0.85rem'>Model: {opt['prob']*100:.1f}%</div>"
            f"<div style='font-size:1.0rem;font-weight:700;color:{bc}'>EV {ev_str}</div>"
            f"<div style='font-size:0.8rem;color:{bc}'>{rat}</div></div>",
            unsafe_allow_html=True,
        )


def _render_fav_button(ma, sport_label, bet_source):
    best = ma.get("best")
    if not (best and best.get("odds") and best.get("ev") is not None):
        return
    key   = f"{sport_label[:3]}_{ma['home_team'][:4]}_{ma['away_team'][:4]}_{ma.get('time','')}"
    fav_bet = {
        "player":   f"{ma['home_team']} vs {ma['away_team']}",
        "sport":    sport_label, "team": ma["home_team"][:3].upper(),
        "bet_type": best["label"].replace("🏠 ","").replace("✈️ ","").replace("🤝 ","").replace("🔄 ",""),
        "odds": best["odds"], "ev": best["ev"], "rating": best["rating"],
        "composite": best["prob"], "linemate_hr": best["prob"], "season_hr": best["prob"],
        "sample": bet_source, "source": bet_source,
    }
    _fav_ids = {f["id"] for f in load_favorieten()}
    _fid     = _make_fav_id(fav_bet["player"], fav_bet["bet_type"])
    if _fid not in _fav_ids:
        if st.button("☆ Opslaan in Favorieten", key=f"fav_{key}", use_container_width=True):
            add_favoriet(fav_bet)
            st.rerun()
    else:
        st.markdown("<div style='color:#4ade80;text-align:center;padding:4px'>"
                    "⭐ Opgeslagen in Favorieten</div>", unsafe_allow_html=True)


def render_soccer_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### ⚽ Voetbal Wedstrijd-analyse")
    _top3 = sorted([ma for ma in match_analyses if ma.get("best") and ma["best"].get("ev") is not None],
                   key=lambda ma: ma["best"]["ev"], reverse=True)[:3]
    if _top3:
        st.markdown("#### 🏆 Top 3 aanbevolen voetbalwedstrijden")
        for _i, _ma in enumerate(_top3, 1):
            _b  = _ma["best"]
            _ev = f"+{_b['ev']:.3f}" if _b["ev"] >= 0 else f"{_b['ev']:.3f}"
            _hf = _ma.get("home_form") or {}
            _af = _ma.get("away_form") or {}
            st.markdown(f"**{_i}. {_ma['home_team']} vs {_ma['away_team']}**  \n"
                        f"Beste inzet: {_b['label']} · EV `{_ev}` · {_b['rating']}  \n"
                        f"xG: {_ma['probs'].get('lH',0):.2f} – {_ma['probs'].get('lA',0):.2f}  \n"
                        f"Form thuis: `{_hf.get('form','—')}` | Form uit: `{_af.get('form','—')}`")
        st.markdown("---")
    for ma in match_analyses:
        home  = ma["home_team"]; away = ma["away_team"]
        probs = ma.get("probs", {}); best = ma.get("best")
        home_f = ma.get("home_form") or {}; away_f = ma.get("away_form") or {}
        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>", unsafe_allow_html=True)
            hcol, tcol = st.columns([4, 1])
            with hcol:
                comp_lbl = ma.get("competition","")
                st.markdown(f"#### ⚽ {home}  vs  {away}"
                            + (f"  <small style='color:#6060a0'> {comp_lbl}</small>" if comp_lbl else ""),
                            unsafe_allow_html=True)
            with tcol:
                if ma.get("time"):
                    st.markdown(f"<div style='text-align:right;color:#8080b0;padding-top:8px'>⏰ {ma['time']}</div>",
                                unsafe_allow_html=True)
            if home_f or away_f:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if home_f:
                        st.metric(f"🏠 {home_f.get('abbrev', home[:3])}", "")
                        st.caption(f"GF avg: {home_f.get('gf_avg',0):.2f}\nGA avg: {home_f.get('ga_avg',0):.2f}\nForm: {home_f.get('form','—')}")
                with c2:
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#8080b0;'>"
                                f"<div>xG: {probs.get('lH',0):.2f} – {probs.get('lA',0):.2f}</div></div>",
                                unsafe_allow_html=True)
                with c3:
                    if away_f:
                        st.metric(f"✈️ {away_f.get('abbrev', away[:3])}", "")
                        st.caption(f"GF avg: {away_f.get('gf_avg',0):.2f}\nGA avg: {away_f.get('ga_avg',0):.2f}\nForm: {away_f.get('form','—')}")
            st.markdown("---")
            opt_cols = st.columns(3)
            for col, opt in zip(opt_cols, ma["options"]):
                _render_match_option_box(col, opt, best)
            st.markdown(f"<div style='color:#6060a0;font-size:0.75rem;margin-top:8px;'>"
                        f"Odds bron: {ma.get('odds_bron','')}  ·  xG {probs.get('lH',0):.2f}–{probs.get('lA',0):.2f}</div>",
                        unsafe_allow_html=True)
            _render_fav_button(ma, "Soccer", "Football-data.org")
            st.markdown("</div>", unsafe_allow_html=True)


# ─── NBA (basketbal) match analyse ──────────────────────────────────────────

_NBA_LEAGUE_PTS_AVG = 112.0
_NBA_HOME_ADV       = 3.0
_NBA_MARGIN_STD     = 13.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + _math.erf(x / _math.sqrt(2.0)))


def _nba_match_probs(home_form: dict, away_form: dict, spread: float = 0.0) -> dict:
    pts_h = home_form.get("pts_avg", _NBA_LEAGUE_PTS_AVG)
    opp_h = home_form.get("opp_pts_avg", _NBA_LEAGUE_PTS_AVG)
    pts_a = away_form.get("pts_avg", _NBA_LEAGUE_PTS_AVG)
    opp_a = away_form.get("opp_pts_avg", _NBA_LEAGUE_PTS_AVG)
    exp_home   = (pts_h + opp_a) / 2.0 + _NBA_HOME_ADV
    exp_away   = (pts_a + opp_h) / 2.0
    exp_margin = exp_home - exp_away
    p_home = _norm_cdf(exp_margin / _NBA_MARGIN_STD)
    p_cover_home = _norm_cdf((exp_margin - spread) / _NBA_MARGIN_STD) if spread != 0.0 else p_home
    return {"p_home": round(p_home, 4), "p_away": round(1-p_home, 4),
            "p_cover_home": round(p_cover_home, 4), "p_cover_away": round(1-p_cover_home, 4),
            "exp_margin": round(exp_margin, 1),
            "exp_home_pts": round(exp_home, 1), "exp_away_pts": round(exp_away, 1)}


def analyze_nba_matches(matches: list) -> list:
    results = []
    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper()
        if "NBA" not in sport and "BASKETBALL" not in sport:
            continue
        home_name = m.get("home_team",""); away_name = m.get("away_team","")
        if not home_name or not away_name:
            continue
        home_form = nba.get_team_form_for_match(home_name)
        away_form = nba.get_team_form_for_match(away_name)
        fallback  = {"pts_avg": _NBA_LEAGUE_PTS_AVG, "opp_pts_avg": _NBA_LEAGUE_PTS_AVG}
        scr_odds = m.get("screenshot_odds") or {}
        odds_bron = "screenshot"
        b365_h2h = {}; b365_sp = {}
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                b365_sp  = odds_api.get_match_odds_spreads("NBA", home_name, away_name)
                b365_h2h = odds_api.get_match_odds_h2h("NBA", home_name, away_name)
                if b365_sp.get("source")=="bet365" or b365_h2h.get("source")=="bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass
        home_ml = b365_h2h.get("home_odds") or scr_odds.get("home")
        away_ml = b365_h2h.get("away_odds") or scr_odds.get("away")
        hs = b365_sp.get("home_spread"); as_ = b365_sp.get("away_spread")
        h_sp_odds = b365_sp.get("home_spread_odds"); a_sp_odds = b365_sp.get("away_spread_odds")
        spread_val = float(hs) if hs is not None else 0.0
        probs = _nba_match_probs(home_form or fallback, away_form or fallback, spread=spread_val)

        def _opt(pk, ov, lbl):
            p = probs.get(pk, 0.0)
            ev = _match_ev(p, ov) if ov else None
            return {"label": lbl, "prob": p, "odds": ov,
                    "ev": ev, "rating": _match_rating(ev) if ev is not None else "—"}

        options = [_opt("p_home", home_ml, f"🏠 {home_name} wint"),
                   _opt("p_away", away_ml, f"✈️ {away_name} wint")]
        if hs is not None and h_sp_odds:
            options.append(_opt("p_cover_home", h_sp_odds, f"🏠 {home_name} {hs:+.1f}"))
            options.append(_opt("p_cover_away", a_sp_odds, f"✈️ {away_name} {as_:+.1f}" if as_ else f"✈️ {away_name} spread"))
        best = max((o for o in options if o["ev"] is not None), key=lambda o: o["ev"], default=None)
        results.append({"home_team": home_name, "away_team": away_name, "time": m.get("time"),
                        "home_form": home_form, "away_form": away_form, "probs": probs,
                        "odds_bron": odds_bron, "options": options, "best": best, "spread": hs})
    return results


def render_nba_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### 🏀 NBA Wedstrijd-analyse")
    _top3 = sorted([ma for ma in match_analyses if ma.get("best") and ma["best"].get("ev") is not None],
                   key=lambda ma: ma["best"]["ev"], reverse=True)[:3]
    if _top3:
        st.markdown("#### 🏆 Top 3 aanbevolen NBA wedstrijden")
        for _i, _ma in enumerate(_top3, 1):
            _b = _ma["best"]; _ev = f"+{_b['ev']:.3f}" if _b["ev"] >= 0 else f"{_b['ev']:.3f}"
            _hf = _ma.get("home_form") or {}; _af = _ma.get("away_form") or {}
            _p  = _ma.get("probs", {})
            st.markdown(f"**{_i}. {_ma['home_team']} vs {_ma['away_team']}**  \n"
                        f"Beste inzet: {_b['label']} · EV `{_ev}` · {_b['rating']}  \n"
                        f"Verwachte marge: {_p.get('exp_margin',0):+.1f} punten  \n"
                        f"L10 thuis: `{_hf.get('last10','—')}` | L10 uit: `{_af.get('last10','—')}`")
        st.markdown("---")
    for ma in match_analyses:
        home = ma["home_team"]; away = ma["away_team"]
        probs = ma.get("probs",{}); best = ma.get("best")
        home_f = ma.get("home_form") or {}; away_f = ma.get("away_form") or {}
        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>", unsafe_allow_html=True)
            hcol, tcol = st.columns([4, 1])
            with hcol:
                st.markdown(f"#### 🏀 {home}  vs  {away}")
            with tcol:
                if ma.get("time"):
                    st.markdown(f"<div style='text-align:right;color:#8080b0;padding-top:8px'>⏰ {ma['time']}</div>",
                                unsafe_allow_html=True)
            if home_f or away_f:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if home_f:
                        st.metric(f"🏠 {home_f.get('abbrev', home[:3])}", "")
                        st.caption(f"Record: {home_f.get('wins',0)}-{home_f.get('losses',0)}\nL10: {home_f.get('last10','—')}\nReeks: {home_f.get('streak','—')}\nThuis: {home_f.get('home_record','—')}")
                with c2:
                    margin = probs.get("exp_margin",0)
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#8080b0;'>"
                                f"<div>Verwachte marge:</div>"
                                f"<div style='font-size:1.2rem;font-weight:700;color:#fff'>{margin:+.1f} pts</div>"
                                f"<div style='font-size:0.85rem;margin-top:4px'>"
                                f"{probs.get('exp_home_pts',0):.0f} – {probs.get('exp_away_pts',0):.0f}</div>"
                                f"</div>", unsafe_allow_html=True)
                with c3:
                    if away_f:
                        st.metric(f"✈️ {away_f.get('abbrev', away[:3])}", "")
                        st.caption(f"Record: {away_f.get('wins',0)}-{away_f.get('losses',0)}\nL10: {away_f.get('last10','—')}\nReeks: {away_f.get('streak','—')}\nUit: {away_f.get('road_record','—')}")
            st.markdown("---")
            opt_cols = st.columns(min(len(ma["options"]), 4))
            for col, opt in zip(opt_cols, ma["options"]):
                _render_match_option_box(col, opt, best)
            st.markdown(f"<div style='color:#6060a0;font-size:0.75rem;margin-top:8px;'>"
                        f"Odds bron: {ma.get('odds_bron','')}  ·  Marge model: {probs.get('exp_margin',0):+.1f} punten</div>",
                        unsafe_allow_html=True)
            _render_fav_button(ma, "NBA", "NBA API")
            st.markdown("</div>", unsafe_allow_html=True)


# ─── MLB (honkbal) match analyse ────────────────────────────────────────────

_MLB_LEAGUE_RUNS_AVG = 4.35
_MLB_HOME_FACTOR     = 1.05
_MLB_RUN_LINE        = 1.5


def _mlb_match_probs(home_form: dict, away_form: dict) -> dict:
    lH = home_form.get("runs_avg", _MLB_LEAGUE_RUNS_AVG) * \
         (away_form.get("opp_runs_avg", _MLB_LEAGUE_RUNS_AVG) / _MLB_LEAGUE_RUNS_AVG) * \
         _MLB_HOME_FACTOR
    lA = away_form.get("runs_avg", _MLB_LEAGUE_RUNS_AVG) * \
         (home_form.get("opp_runs_avg", _MLB_LEAGUE_RUNS_AVG) / _MLB_LEAGUE_RUNS_AVG)
    lH = max(1.5, min(lH, 9.0)); lA = max(1.5, min(lA, 9.0))
    p_home = p_away = p_home_rl = p_away_rl = 0.0
    for h in range(20):
        for a in range(20):
            p = _poisson_p(h, lH) * _poisson_p(a, lA)
            if h > a:
                p_home += p
                if h - a >= 2: p_home_rl += p
            elif h < a:
                p_away += p; p_away_rl += p
    tot    = p_home + p_away or 1.0
    rl_tot = p_home_rl + p_away_rl or 1.0
    return {"p_home": round(p_home/tot, 4), "p_away": round(p_away/tot, 4),
            "p_home_rl": round(p_home_rl/rl_tot, 4), "p_away_rl": round(p_away_rl/rl_tot, 4),
            "lH": round(lH, 2), "lA": round(lA, 2)}


def analyze_mlb_matches(matches: list) -> list:
    results = []
    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper()
        if not any(x in sport for x in ("MLB","BASEBALL","HONKBAL")):
            continue
        home_name = m.get("home_team",""); away_name = m.get("away_team","")
        if not home_name or not away_name:
            continue
        home_form = mlb.get_team_form_for_match(home_name)
        away_form = mlb.get_team_form_for_match(away_name)
        fallback  = {"runs_avg": _MLB_LEAGUE_RUNS_AVG, "opp_runs_avg": _MLB_LEAGUE_RUNS_AVG}
        probs = _mlb_match_probs(home_form or fallback, away_form or fallback)
        scr_odds = m.get("screenshot_odds") or {}
        odds_bron = "screenshot"
        b365_h2h = {}; b365_sp = {}
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                b365_h2h = odds_api.get_match_odds_h2h("MLB", home_name, away_name)
                b365_sp  = odds_api.get_match_odds_spreads("MLB", home_name, away_name)
                if b365_h2h.get("source")=="bet365" or b365_sp.get("source")=="bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass
        home_ml = b365_h2h.get("home_odds") or scr_odds.get("home")
        away_ml = b365_h2h.get("away_odds") or scr_odds.get("away")
        hs = b365_sp.get("home_spread"); rl_val = abs(float(hs)) if hs is not None else _MLB_RUN_LINE
        h_rl_odds = b365_sp.get("home_spread_odds"); a_rl_odds = b365_sp.get("away_spread_odds")

        # Bereken kansen voor meerdere run lines (-1.5 en -2.5)
        def _p_home_rl_n(n):
            """P(home wint met n+ runs verschil) via Poisson model."""
            lH = probs.get("lH", _MLB_LEAGUE_RUNS_AVG)
            lA = probs.get("lA", _MLB_LEAGUE_RUNS_AVG)
            p = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                    for h in range(20) for a in range(20) if h - a >= int(n))
            rl_tot = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                         for h in range(20) for a in range(20) if h != a)
            return round(p / rl_tot, 4) if rl_tot > 0 else 0.0

        def _p_away_rl_n(n):
            lH = probs.get("lH", _MLB_LEAGUE_RUNS_AVG)
            lA = probs.get("lA", _MLB_LEAGUE_RUNS_AVG)
            p = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                    for h in range(20) for a in range(20) if a - h >= 0)
            rl_tot = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                         for h in range(20) for a in range(20) if h != a)
            return round(p / rl_tot, 4) if rl_tot > 0 else 0.0

        def _opt_raw(p_val, ov, lbl):
            ev = _match_ev(p_val, ov) if ov else None
            return {"label": lbl, "prob": p_val, "odds": ov,
                    "ev": ev, "rating": _match_rating(ev) if ev is not None else "—"}

        p_h_rl15 = probs.get("p_home_rl", 0.0)
        p_a_rl15 = probs.get("p_away_rl", 0.0)
        p_h_rl25 = _p_home_rl_n(3)   # home wint met 3+ = -2.5 gedekt
        p_a_rl25 = _p_away_rl_n(0)   # away wint of gelijk = +2.5 gedekt

        options = [
            _opt_raw(probs.get("p_home", 0.0), home_ml,   f"🏠 {home_name} wint"),
            _opt_raw(probs.get("p_away", 0.0), away_ml,   f"✈️ {away_name} wint"),
            _opt_raw(p_h_rl15, h_rl_odds, f"🏠 {home_name} -{rl_val:.1f} RL"),
            _opt_raw(p_a_rl15, a_rl_odds, f"✈️ {away_name} +{rl_val:.1f} RL"),
            _opt_raw(p_h_rl25, None,       f"🏠 {home_name} -2.5 RL"),
            _opt_raw(p_a_rl25, None,       f"✈️ {away_name} +2.5 RL"),
        ]
        best = max((o for o in options if o["ev"] is not None), key=lambda o: o["ev"], default=None)
        results.append({"home_team": home_name, "away_team": away_name, "time": m.get("time"),
                        "home_form": home_form, "away_form": away_form, "probs": probs,
                        "odds_bron": odds_bron, "options": options, "best": best, "run_line": rl_val})
    return results


def render_mlb_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### ⚾ MLB Wedstrijd-analyse")
    _top3 = sorted([ma for ma in match_analyses if ma.get("best") and ma["best"].get("ev") is not None],
                   key=lambda ma: ma["best"]["ev"], reverse=True)[:3]
    if _top3:
        st.markdown("#### 🏆 Top 3 aanbevolen MLB wedstrijden")
        for _i, _ma in enumerate(_top3, 1):
            _b = _ma["best"]; _ev = f"+{_b['ev']:.3f}" if _b["ev"] >= 0 else f"{_b['ev']:.3f}"
            _p = _ma.get("probs",{})
            st.markdown(f"**{_i}. {_ma['home_team']} vs {_ma['away_team']}**  \n"
                        f"Beste inzet: {_b['label']} · EV `{_ev}` · {_b['rating']}  \n"
                        f"xRuns: {_p.get('lH',0):.2f} – {_p.get('lA',0):.2f}")
        st.markdown("---")
    for ma in match_analyses:
        home = ma["home_team"]; away = ma["away_team"]
        probs = ma.get("probs",{}); best = ma.get("best")
        home_f = ma.get("home_form") or {}; away_f = ma.get("away_form") or {}
        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>", unsafe_allow_html=True)
            hcol, tcol = st.columns([4, 1])
            with hcol:
                st.markdown(f"#### ⚾ {home}  vs  {away}")
            with tcol:
                if ma.get("time"):
                    st.markdown(f"<div style='text-align:right;color:#8080b0;padding-top:8px'>⏰ {ma['time']}</div>",
                                unsafe_allow_html=True)
            if home_f or away_f:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if home_f:
                        st.metric(f"🏠 {home_f.get('abbrev', home[:3])}", "")
                        st.caption(f"Record: {home_f.get('wins',0)}-{home_f.get('losses',0)}\nRuns avg: {home_f.get('runs_avg',0):.2f}\nOpp runs: {home_f.get('opp_runs_avg',0):.2f}\nThuis: {home_f.get('home_record','—')}")
                with c2:
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#8080b0;'>"
                                f"<div>xRuns: {probs.get('lH',0):.2f} – {probs.get('lA',0):.2f}</div>"
                                f"<div style='font-size:0.85rem;margin-top:4px'>Run line: ±{ma.get('run_line',1.5):.1f}</div>"
                                f"</div>", unsafe_allow_html=True)
                with c3:
                    if away_f:
                        st.metric(f"✈️ {away_f.get('abbrev', away[:3])}", "")
                        st.caption(f"Record: {away_f.get('wins',0)}-{away_f.get('losses',0)}\nRuns avg: {away_f.get('runs_avg',0):.2f}\nOpp runs: {away_f.get('opp_runs_avg',0):.2f}\nUit: {away_f.get('road_record','—')}")
            st.markdown("---")
            opt_cols = st.columns(min(len(ma["options"]), 4))
            for col, opt in zip(opt_cols, ma["options"]):
                _render_match_option_box(col, opt, best)
            st.markdown(f"<div style='color:#6060a0;font-size:0.75rem;margin-top:8px;'>"
                        f"Odds bron: {ma.get('odds_bron','')}  ·  xRuns {probs.get('lH',0):.2f}–{probs.get('lA',0):.2f}</div>",
                        unsafe_allow_html=True)
            _render_fav_button(ma, "MLB", "MLB API")
            st.markdown("</div>", unsafe_allow_html=True)



# ─── Props filtering en ranking ────────────────────────────────────────────────

def _filter_and_rank_props(enriched: list) -> list:
    """
    Filter en rank props voor de top N weergave.
    - unavailable → volledig uitsluiten
    - different_line → 15% EV penalty, tonen met waarschuwing
    - available → normaal ranken
    - negatieve EV → altijd uitsluiten
    """
    import logging as _logging
    result = []
    for bet in enriched:
        b365_status = (bet.get("bet365") or {}).get("status", "")
        ev = float(bet.get("ev") or -999)
        
        if b365_status == "unavailable":
            _logging.debug(f"[TOP5 SKIP] {bet.get('player')} | EV={ev:.3f} | status=unavailable")
            continue
        
        if b365_status == "different_line":
            penalized_ev = ev * 0.85
            bet = dict(bet)
            bet["ev"] = penalized_ev
            bet["_ev_penalty_note"] = "⚠️ Andere lijn op Bet365 (−15% EV penalty)"
            _logging.debug(f"[TOP5 PENALTY] {bet.get('player')} | EV={ev:.3f}→{penalized_ev:.3f} | status=different_line")
            ev = penalized_ev
        
        if ev <= 0:
            _logging.debug(f"[TOP5 SKIP] {bet.get('player')} | EV={ev:.3f} | negatief")
            continue
        
        _logging.debug(f"[TOP5 OK] {bet.get('player')} | EV={ev:.3f} | status={b365_status or 'n/a'}")
        result.append(bet)
    
    result.sort(key=lambda b: float(b.get("ev") or 0), reverse=True)
    return result


def generate_parlay_suggestions(bets: list, max_parlays: int = 3) -> list:
    """Genereer top parlay combinaties uit beschikbare, positieve-EV props."""
    import itertools
    eligible = [
        b for b in bets
        if ((b.get("bet365") or {}).get("status", "") in ("available", "different_line", "")
            and float(b.get("ev") or -1) > 0
            and b.get("injury_status", "fit") != "injured")
    ]
    if len(eligible) < 2:
        return []
    candidates = []
    for n in (2, 3):
        for combo in itertools.combinations(eligible[:15], n):
            teams = [b.get("team", "") for b in combo]
            if len(set(t for t in teams if t)) < len([t for t in teams if t]):
                continue
            comb_odds = 1.0
            hit_ch    = 1.0
            for b in combo:
                comb_odds *= float(b.get("odds") or 1.5)
                hit_ch    *= float(b.get("composite") or b.get("linemate_hr") or 0.5)
            p_ev = hit_ch * (comb_odds - 1) - (1 - hit_ch)
            candidates.append({
                "props": list(combo),
                "gecombineerde_odds": round(comb_odds, 3),
                "hit_kans":          round(hit_ch, 4),
                "parlay_ev":         round(p_ev, 4),
            })
    candidates.sort(key=lambda x: x["parlay_ev"], reverse=True)
    return candidates[:max_parlays]


def render_top3(top3: list):
    st.markdown("### 🏆 Top prop aanbevelingen")
    for i, b in enumerate(top3, 1):
        ev_str = f"+{b['ev']:.3f}" if b['ev'] >= 0 else f"{b['ev']:.3f}"
        st.markdown(
            f"**{i}. {b['player']}** · {b['bet_type']} @ {b['odds']}  "
            f"&nbsp;&nbsp; EV `{ev_str}`"
        )


def render_bet_card(bet: dict, rank: int, total: int, is_fav: bool = False):
    sport_icon    = SPORT_ICONS.get(bet["sport"].upper(), "⚽")
    ev_str        = f"+{bet['ev']:.3f}" if bet["ev"] >= 0 else f"{bet['ev']:.3f}"
    composite_pct = int(bet["composite"] * 100)
    rat_color     = _rating_color(bet["rating"])

    with st.container():
        st.markdown(
            f"<div style='background:#11112b;border:1px solid #2a2a58;"
            f"border-radius:12px;padding:16px;margin-bottom:12px;'>",
            unsafe_allow_html=True,
        )
        col_l, col_r = st.columns([3, 1])
        with col_l:
            st.markdown(f"**{sport_icon} #{rank} van {total}**")
        with col_r:
            st.markdown(
                f"<span style='color:{rat_color};font-weight:700;'>{bet['rating']}</span>",
                unsafe_allow_html=True,
            )
        st.markdown(f"#### {bet['player']}")
        _inj = bet.get("injury_status", "unknown")
        if _inj == "injured":
            st.error("❌ Geblesseerd — speler waarschijnlijk niet beschikbaar")
        elif _inj == "questionable":
            st.warning("⚠️ Twijfelachtig voor deze wedstrijd")
        if bet.get("_ev_penalty_note"):
            st.warning(bet["_ev_penalty_note"])
        b365_label = bet.get("bet365", {}).get("label", "")
        caption_line = f"{bet['bet_type']} · {bet['sport']}"
        if b365_label:
            caption_line += f"  ·  {b365_label}"
        st.caption(caption_line)

        ev_color = "#4ade80" if bet["ev"] >= 0.05 else "#facc15"
        st.markdown(
            f"<span style='color:{ev_color};font-size:1.4rem;font-weight:800;'>EV {ev_str}</span>",
            unsafe_allow_html=True,
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Linemate HR", f"{bet['linemate_hr']*100:.1f}%")
        c2.metric("Seizoens HR", f"{bet['season_hr']*100:.1f}%")
        c3.metric("Odds", f"{bet['odds']}")
        c4.metric("Sample", bet["sample"])

        st.progress(bet["composite"], text=f"Composite: {composite_pct}%")

        if bet.get("no_linemate_hr"):
            st.warning(
                "⚠️ **Onvoldoende data** — Linemate hit rate niet gevonden in screenshot. "
                "EV is uitsluitend gebaseerd op historische statistieken."
            )

        info_parts = []
        if bet.get("opponent"):
            info_parts.append(f"vs {bet['opponent']}")
        if bet.get("gaa"):
            info_parts.append(f"GAA {bet['gaa']}")
        if bet.get("source"):
            info_parts.append(f"Bron: {bet['source']}")
        if info_parts:
            st.caption(" · ".join(info_parts))

        # ─── 📝 Odds aanpassen ────────────────────────────────────────────────
        _adj_ss_key = "adj_" + _make_fav_id(bet["player"], bet["bet_type"])
        _stored_odds = st.session_state.get(_adj_ss_key, None)
        _display_odds = _stored_odds if _stored_odds is not None else float(bet["odds"])

        with st.expander("📝 Odds aangepast op Bet365?"):
            _inp_key = f"odds_inp_{rank}_{total}"
            _new_odds_inp = st.number_input(
                "Nieuwe odds",
                min_value=1.01,
                max_value=50.0,
                value=_display_odds,
                step=0.01,
                format="%.2f",
                key=_inp_key,
            )
            if st.button("Herbereken EV", key=f"recalc_{rank}_{total}"):
                st.session_state[_adj_ss_key] = float(
                    st.session_state.get(_inp_key, _new_odds_inp)
                )
                st.rerun()

            # Toon EV vergelijking zodra odds afwijken
            _eff_odds = st.session_state.get(_adj_ss_key, None)
            if _eff_odds is not None and abs(_eff_odds - float(bet["odds"])) > 0.001:
                _composite = bet.get("composite", 0.5)
                _orig_ev   = bet["ev"]
                _new_ev    = _composite * (_eff_odds - 1) - (1 - _composite)
                _diff      = _new_ev - _orig_ev
                _o_str     = f"+{_orig_ev:.3f}" if _orig_ev >= 0 else f"{_orig_ev:.3f}"
                _n_str     = f"+{_new_ev:.3f}"  if _new_ev  >= 0 else f"{_new_ev:.3f}"
                _d_str     = f"{_diff:+.3f}"
                _reden     = "hogere" if _diff >= 0 else "lagere"
                st.caption(f"EV: **{_o_str}** → **{_n_str}** ({_d_str} door {_reden} odds)")
                if _new_ev < 0:
                    st.error("❌ Weddenschap niet meer interessant bij deze odds")
                else:
                    st.success("✅ Nog steeds interessant")

        # ─── ⭐ Favoriet knop (met aangepaste odds indien ingesteld) ──────────
        _fav_label = "⭐ Verwijder favoriet" if is_fav else "⭐ Sla op als favoriet"
        if st.button(_fav_label, key=f"fav_{rank}_{total}", use_container_width=False):
            if is_fav:
                remove_favoriet(_make_fav_id(bet["player"], bet["bet_type"]))
            else:
                # Gebruik aangepaste odds als die ingesteld zijn
                _fav_adj = st.session_state.get(_adj_ss_key, None)
                if _fav_adj is not None and abs(_fav_adj - float(bet["odds"])) > 0.001:
                    _composite = bet.get("composite", 0.5)
                    _adj_ev    = _composite * (_fav_adj - 1) - (1 - _composite)
                    add_favoriet({**bet, "odds": _fav_adj, "ev": _adj_ev})
                else:
                    add_favoriet(bet)
            st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)


# ─── Hoofdscherm ──────────────────────────────────────────────────────────────

st.markdown("""
<div style="
  background: linear-gradient(135deg, #12103a 0%, #1e1860 100%);
  border: 1px solid #3a2a70;
  border-radius: 14px;
  padding: 1.2rem 2rem;
  margin-bottom: 1rem;
  display: flex;
  align-items: center;
  gap: 1rem;
  box-shadow: 0 4px 24px rgba(124,58,237,0.18);
">
  <span style="font-size:2rem;">🎯</span>
  <div>
    <div style="font-size:1.5rem; font-weight:800; color:#c4b5fd; letter-spacing:-0.3px; line-height:1.2;">Bet Analyzer</div>
    <div style="color:#6060a0; font-size:0.82rem; margin-top:2px;">Linemate + Flashscore &nbsp;·&nbsp; NHL · NBA · MLB · Voetbal</div>
  </div>
</div>
""", unsafe_allow_html=True)

# API key
try:
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

if not api_key:
    st.error("❌ Geen `ANTHROPIC_API_KEY` gevonden in st.secrets.")
    st.stop()

# ─── Supabase initialiseren ───────────────────────────────────────────────────
try:
    _sb_url = st.secrets.get("SUPABASE_URL", "")
    _sb_key = st.secrets.get("SUPABASE_KEY", "")
except Exception:
    _sb_url = os.environ.get("SUPABASE_URL", "")
    _sb_key = os.environ.get("SUPABASE_KEY", "")

_db_cloud = db.init(_sb_url, _sb_key)
if not _db_cloud:
    st.warning(
        "⚠️ **Lokale opslag** — Favorieten, resultaten en geschiedenis kunnen "
        "verdwijnen na een Streamlit herstart. "
        "Voeg `SUPABASE_URL` en `SUPABASE_KEY` toe aan je secrets voor persistente opslag."
    )

# Odds API (optioneel — voor Bet365 verificatie)
try:
    _odds_key = st.secrets.get("ODDS_API_KEY", "")
except Exception:
    _odds_key = os.environ.get("ODDS_API_KEY", "")
odds_api.set_api_key(_odds_key)

if not ANTHROPIC_AVAILABLE:
    st.error("❌ `anthropic` pakket niet geïnstalleerd.")
    st.stop()

# Moneypuck GDrive credentials
try:
    from sports.moneypuck_local import set_gdrive_credentials, RAW_DIR, FILTERED_DIR
    from pathlib import Path as _Path

    _gdrive_ok = False
    try:
        _gdrive_dict = dict(st.secrets.get("gcp_service_account", {}))
        if _gdrive_dict.get("type") == "service_account":
            set_gdrive_credentials(_gdrive_dict)
            _gdrive_ok = True
    except Exception:
        pass

    _local_ok    = RAW_DIR.exists() or FILTERED_DIR.exists()
    _file_ids_ok = _Path(__file__).parent.joinpath("gdrive_file_ids.json").exists()

    if not _local_ok:
        if _gdrive_ok and _file_ids_ok:
            st.success("☁️ **Cloud modus** — MoneyPuck data via Google Drive.")
        elif not _gdrive_ok:
            st.info("ℹ️ **Cloud versie** — Historische MoneyPuck data niet beschikbaar.")
except Exception:
    pass

# ─── Session state initialiseren ──────────────────────────────────────────────

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

if "last_analysis" not in st.session_state:
    st.session_state.last_analysis = None

# ─── Tabs ─────────────────────────────────────────────────────────────────────

tab_analyse, tab_favorieten, tab_bankroll, tab_parlay, tab_history = st.tabs(
    ["🔍 Analyse", "⭐ Favorieten", "📊 Bankroll", "🎯 Parlay Builder", "📋 Geschiedenis"]
)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — ANALYSE
# ══════════════════════════════════════════════════════════════════════════════

with tab_analyse:

    # Melding na vorige analyse
    if st.session_state.get("just_analyzed"):
        st.success("✅ Analyse klaar — bestanden gewist. Upload nieuwe screenshots voor een nieuwe analyse.")
        st.session_state.just_analyzed = False

    # Uploader — key incrementeert na elke analyse om hem te resetten
    uploaded_files = st.file_uploader(
        "Upload Linemate en/of Flashscore screenshots",
        type=["png", "jpg", "jpeg", "webp", "heic", "heif"],
        accept_multiple_files=True,
        help="Je kunt meerdere screenshots tegelijk uploaden (ook iPhone HEIC foto's)",
        key=f"uploader_{st.session_state.uploader_key}",
    )

    if uploaded_files:
        cols = st.columns(min(len(uploaded_files), 4))
        for i, f in enumerate(uploaded_files):
            cols[i % 4].image(f, use_container_width=True)

    # ── Odds API gebruik indicator ──
    if _odds_key:
        _usage    = odds_api.get_usage()
        _calls    = _usage.get("calls", 0)
        _limiet   = _usage.get("limiet", 500)
        _maand    = _usage.get("maand", "")
        # Bereken eerste dag volgende maand
        _today    = datetime.date.today()
        _nxt      = (_today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        _nxt_str  = _nxt.strftime("%-d %B %Y")
        if _calls >= _limiet:
            st.warning(
                f"ℹ️ Bet365 verificatie tijdelijk uitgeschakeld (maandlimiet bereikt). "
                f"Reset op {_nxt_str}."
            )
        elif _calls > 400:
            st.warning(
                f"⚠️ Bijna op Odds API limiet — bet365 verificatie wordt binnenkort "
                f"uitgeschakeld ({_calls}/{_limiet} calls deze maand)"
            )
        else:
            st.caption(f"🎯 Odds API: {_calls}/{_limiet} calls gebruikt deze maand")

    analyze_btn = st.button(
        "🔍 Analyseer",
        use_container_width=True,
        disabled=not uploaded_files,
        type="primary",
    )

    if analyze_btn and uploaded_files:
        tmp_paths = []
        _analysis_aborted = False
        try:
            for f in uploaded_files:
                suffix = Path(f.name).suffix.lower() or ".png"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp.write(f.read())
                tmp.flush()
                tmp.close()   # sluit voor lezen door Claude
                # Converteer HEIC/HEIF automatisch naar JPEG (iPhone foto's)
                converted = _convert_heic_to_jpeg(tmp.name)
                tmp_paths.append(converted)

            client = anthropic.Anthropic(api_key=api_key)

            with st.status("⏳ Analyseren...", expanded=True) as status:
                st.write("📸 Screenshots herkennen...")
                bets, matches = extract_bets(client, tmp_paths)

                if not bets and not matches:
                    _analysis_aborted = True
                    st.error("Geen bets of wedstrijden gevonden in de afbeeldingen.")

                    # ── Debug: toon wat Claude werkelijk antwoordde ──
                    _dbg       = st.session_state.get("_dbg_raw", "")
                    _dbg_model = st.session_state.get("_dbg_model", _EXTRACT_MODEL)
                    with st.expander("🔧 Debug — Claude's ruwe response", expanded=True):
                        st.caption(f"Model: `{_dbg_model}` · {len(_dbg)} tekens teruggegeven")
                        st.code(_dbg[:3000] if _dbg else "(leeg — API-call mislukt?)", language="text")

                    # ── Auto-test: beschrijf de eerste afbeelding ──
                    st.write("🔍 **Auto-test:** Claude beschrijft de afbeelding...")
                    try:
                        _test_block = _image_content_block(tmp_paths[0])
                        _test_resp  = client.messages.create(
                            model=_EXTRACT_MODEL,
                            max_tokens=512,
                            messages=[{"role": "user", "content": [
                                _test_block,
                                {"type": "text",
                                 "text": "Beschrijf wat je ziet in deze afbeelding."},
                            ]}],
                        )
                        st.info(f"**Claude ziet:** {_test_resp.content[0].text}")
                    except Exception as _te:
                        st.error(f"Beschrijvingstest ook mislukt: {_te}")

                if _analysis_aborted:
                    status.update(label="⚠️ Analyse mislukt — zie debug info hierboven", state="error")
                else:
                    scenario = detect_scenario(bets, matches)
                    st.write(f"✅ Gevonden: {len(bets)} props, {len(matches)} wedstrijden")
                    st.write(SCENARIO_LABELS[scenario])

                    lm_w, s_w = SCENARIO_WEIGHTS[scenario]

                    # Scenario 1: genereer auto-props uit het wedstrijdschema
                    if scenario == 1:
                        st.write("📅 Wedstrijdschema ophalen en props genereren...")
                        auto_bets = generate_auto_props(
                            matches,
                            progress_cb=lambda msg: st.write(f"  · {msg}"),
                        )
                        if auto_bets:
                            bets = auto_bets
                            st.write(f"✅ {len(auto_bets)} automatische props gegenereerd")
                        else:
                            st.warning("⚠️ Geen automatische props beschikbaar (schema of API niet bereikbaar)")

                    if bets:
                        st.write("🔎 Spelersdata ophalen en EV berekenen...")
                        cache: dict = {}
                        enriched = []
                        prog = st.progress(0)
                        for i, bet in enumerate(bets):
                            enriched.append(enrich_bet(bet, cache,
                                                        linemate_weight=lm_w,
                                                        season_weight=s_w))
                            prog.progress((i + 1) / len(bets))
                        enriched.sort(key=lambda x: x["ev"], reverse=True)
                        st.write(f"✅ {len(enriched)} props gescoord")

                        # ── Bet365 verificatie (optioneel) ──
                        if odds_api._API_KEY and not odds_api.is_limit_reached():
                            _to_check = [b for b in enriched if b["ev"] > 0]
                            if _to_check:
                                st.write(
                                    f"💰 Bet365 verificatie voor {len(_to_check)}/{len(enriched)} "
                                    f"props (EV > 0)..."
                                )
                                odds_api.prefetch_event_props_for_bets(_to_check)
                                b365_prog = st.progress(0)
                                for _i, _bet in enumerate(_to_check):
                                    b365 = odds_api.check_bet365_availability(
                                        player_name=_bet["player"],
                                        bet_type=_bet["bet_type"],
                                        sport=_bet["sport"],
                                        team=_bet.get("team", ""),
                                    )
                                    _bet["bet365"] = b365
                                    if b365["status"] == "available" and b365.get("bet365_odds"):
                                        _bet["odds"]   = b365["bet365_odds"]
                                        _bet["ev"]     = ev(_bet["composite"], b365["bet365_odds"])
                                        _bet["rating"] = rating(_bet["ev"], _bet["composite"])
                                    b365_prog.progress((_i + 1) / len(_to_check))
                                _usage_now = odds_api.get_usage()
                                st.write(
                                    f"✅ Bet365 verificatie klaar "
                                    f"({_usage_now['calls']}/{_usage_now['limiet']} calls deze maand)"
                                )

                            def _ev_rank(b):
                                s = b.get("bet365", {}).get("status", "unknown")
                                if s == "unavailable":
                                    return -999.0
                                if s == "different_line":
                                    return b["ev"] * 0.85
                                return b["ev"]
                            enriched.sort(key=_ev_rank, reverse=True)

                        elif odds_api._API_KEY and odds_api.is_limit_reached():
                            st.write("ℹ️ Bet365 verificatie overgeslagen (maandlimiet bereikt)")
                    else:
                        enriched = []

                    flashscore_text       = ""
                    nhl_match_analyses    = []
                    soccer_match_analyses = []
                    nba_match_analyses    = []
                    mlb_match_analyses    = []
                    if matches:
                        def _is_nba_match(m):
                            s = (m.get("sport") or m.get("competition") or "").upper()
                            return "NBA" in s or "BASKETBALL" in s
                        def _is_mlb_match(m):
                            s = (m.get("sport") or m.get("competition") or "").upper()
                            return any(x in s for x in ("MLB","BASEBALL","HONKBAL"))

                        nhl_matches    = [m for m in matches if _is_nhl_match(m)]
                        nba_matches    = [m for m in matches if _is_nba_match(m)]
                        mlb_matches    = [m for m in matches if _is_mlb_match(m)]
                        soccer_matches = [m for m in matches
                                          if m not in nhl_matches
                                          and m not in nba_matches
                                          and m not in mlb_matches]

                        if nhl_matches:
                            st.write(f"🏒 {len(nhl_matches)} NHL-wedstrijd(en) analyseren...")
                            nhl_match_analyses = analyze_nhl_matches(nhl_matches)
                            st.write("✅ NHL wedstrijd-analyse klaar")

                        if nba_matches:
                            st.write(f"🏀 {len(nba_matches)} NBA-wedstrijd(en) analyseren...")
                            nba_match_analyses = analyze_nba_matches(nba_matches)
                            st.write("✅ NBA wedstrijd-analyse klaar")

                        if mlb_matches:
                            st.write(f"⚾ {len(mlb_matches)} MLB-wedstrijd(en) analyseren...")
                            mlb_match_analyses = analyze_mlb_matches(mlb_matches)
                            st.write("✅ MLB wedstrijd-analyse klaar")

                        if soccer_matches:
                            st.write(f"⚽ {len(soccer_matches)} voetbalwedstrijd(en) analyseren...")
                            soccer_match_analyses = analyze_soccer_matches(soccer_matches)
                            st.write("📺 Teamvorm ophalen via Football-Data API...")
                            soccer_matches = _enrich_soccer_matches_form(soccer_matches)
                            st.write("📺 Flashscore analyseren via Claude...")
                            flashscore_text = analyze_flashscore(client, soccer_matches, enriched)
                            st.write("✅ Voetbal analyse klaar")

                    status.update(label="✅ Analyse compleet!", state="complete")

            if not _analysis_aborted:
                # Blessure status toevoegen (alleen bij positieve EV props, API-quota sparen)
                if _INJURIES_AVAILABLE and enriched:
                    try:
                        _injuries_mod.enrich_with_injury_status(enriched)
                    except Exception:
                        pass

                # Filter en rank met verbeterde logica (penalty voor different_line, uitsluiten unavailable)
                enriched_ranked = _filter_and_rank_props(enriched)

                # Parlay suggesties genereren op basis van gerankte props
                _auto_parlays = generate_parlay_suggestions(enriched_ranked)
                st.session_state["auto_parlay_suggestions"] = _auto_parlays

                # Top 3 berekenen uit gerankte props
                def _is_b365_ok(b):
                    return b.get("bet365", {}).get("status", "unknown") != "unavailable"

                top3 = [b for b in enriched_ranked if b["rating"].startswith("✅")][:3]
                if not top3:
                    top3 = enriched_ranked[:3]
                if not top3:
                    # fallback op volledige enriched lijst — ALLEEN positieve EV
                    top3 = [
                        b for b in enriched
                        if _is_b365_ok(b) and float(b.get("ev") or -1) > 0
                    ][:3]
                top3_out = [{"player": b["player"], "bet_type": b["bet_type"],
                             "odds": b["odds"], "ev": b["ev"]} for b in top3]

                # Opslaan in geschiedenis (met alle positieve-EV props + parlay suggesties)
                if enriched:
                    save_to_history(
                        enriched,
                        alle_props=enriched_ranked,
                        parlay_suggesties=_auto_parlays,
                    )

                # Resultaten opslaan in session state
                st.session_state.last_analysis = {
                    "enriched":              enriched,
                    "enriched_ranked":       enriched_ranked,
                    "top3":                  top3_out,
                    "flashscore":            flashscore_text,
                    "nhl_match_analyses":    nhl_match_analyses,
                    "soccer_match_analyses": soccer_match_analyses,
                    "nba_match_analyses":    nba_match_analyses,
                    "mlb_match_analyses":    mlb_match_analyses,
                    "scenario":              scenario,
                    "auto_parlays":          _auto_parlays,
                }

                # Uploader resetten + rerun
                st.session_state.uploader_key += 1
                st.session_state.just_analyzed = True
                st.rerun()

        except Exception as e:
            st.error(f"❌ Fout: {e}")
            raise
        finally:
            for p in tmp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    # ── Vorige analyseresultaten tonen ──
    if st.session_state.last_analysis:
        res       = st.session_state.last_analysis
        enriched  = res["enriched"]
        top3_out  = res["top3"]
        flashscore_text       = res["flashscore"]
        nhl_match_analyses    = res.get("nhl_match_analyses", [])
        soccer_match_analyses = res.get("soccer_match_analyses", [])
        nba_match_analyses    = res.get("nba_match_analyses", [])
        mlb_match_analyses    = res.get("mlb_match_analyses", [])
        scenario  = res.get("scenario", 3)

        # Scenario label
        st.info(SCENARIO_LABELS.get(scenario, ""))

        # Tip bij Scenario 3: alleen Linemate, geen Flashscore
        if scenario == 3:
            st.info("⚠️ Tip: upload ook een Flashscore screenshot voor wedstrijdcontext en automatische prop-suggesties.")

        # Wedstrijd-analyses (NHL, NBA, MLB, voetbal)
        if nhl_match_analyses:
            render_nhl_match_cards(nhl_match_analyses)
        if nba_match_analyses:
            render_nba_match_cards(nba_match_analyses)
        if mlb_match_analyses:
            render_mlb_match_cards(mlb_match_analyses)
        if soccer_match_analyses:
            render_soccer_match_cards(soccer_match_analyses)

        if flashscore_text:
            render_flashscore(flashscore_text)

        if enriched:
            # Debug: hit_rates per prop (collapsed)
            _hit_rates_dbg = st.session_state.get("_dbg_hit_rates", [])
            if _hit_rates_dbg:
                with st.expander("🔧 Debug — Hit rates per prop (Claude Haiku extractie)", expanded=False):
                    _missing = [r for r in _hit_rates_dbg if r["hit_rate"] is None]
                    _found   = [r for r in _hit_rates_dbg if r["hit_rate"] is not None]
                    st.caption(
                        f"{len(_found)}/{len(_hit_rates_dbg)} props met hit_rate · "
                        f"{len(_missing)} ontbrekend"
                    )
                    for r in _hit_rates_dbg:
                        hr = r["hit_rate"]
                        hr_str = f"{hr*100:.1f}%" if hr is not None else "⚠️ Niet gevonden"
                        st.write(
                            f"- **{r['player']}** · {r['bet_type']} "
                            f"@ {r['odds']} → HR: `{hr_str}`"
                        )

            st.markdown("---")
            render_top3(top3_out)

            # ── Automatische parlay suggesties ────────────────────────────────
            _aps = res.get("auto_parlays") or st.session_state.get("auto_parlay_suggestions", [])
            if _aps:
                st.markdown("---")
                st.markdown("### 🎯 Automatische Parlay Suggesties")
                st.caption("Top combinaties op basis van beschikbare props")
                for _api, _apc in enumerate(_aps, 1):
                    _aps_ev   = _apc.get("parlay_ev", 0)
                    _aps_ev_s = f"+{_aps_ev:.3f}" if _aps_ev >= 0 else f"{_aps_ev:.3f}"
                    _legs_str = " + ".join(
                        f"{b.get('player','')} ({b.get('bet_type','')})"
                        for b in _apc.get("props", [])
                    )
                    _asc1, _asc2, _asc3, _asc4, _asc5 = st.columns([4, 1, 1, 1, 1])
                    _asc1.write(f"**{_api}.** {_legs_str}")
                    _asc2.write(f"Odds: {_apc.get('gecombineerde_odds', 0):.2f}")
                    _asc3.write(f"Hit: {_apc.get('hit_kans', 0)*100:.1f}%")
                    _asc4.write(f"EV: {_aps_ev_s}")
                    if _asc5.button("⭐ Sla op", key=f"autopar_{_api}"):
                        import uuid as _apuuid, datetime as _apdt
                        db.save_parlay({
                            "id":                 str(_apuuid.uuid4())[:8],
                            "datum":              _apdt.datetime.now().isoformat(),
                            "props_json":         _apc.get("props", []),
                            "gecombineerde_odds": _apc.get("gecombineerde_odds", 1.0),
                            "hit_kans":           _apc.get("hit_kans", 0.0),
                            "ev_score":           _aps_ev,
                            "inzet":              10.0,
                            "uitkomst":           "open",
                            "winst_verlies":      0.0,
                            "legs_json":          {
                                b.get("player","")+"_"+b.get("bet_type",""):"open"
                                for b in _apc.get("props",[])
                            },
                        })
                        st.success(f"✅ Parlay {_api} opgeslagen!")
                        st.rerun()

            st.markdown("---")
            st.markdown("### 📊 Alle props")
            _fav_ids_set = {f["id"] for f in load_favorieten()}
            for i, bet in enumerate(enriched, 1):
                _is_fav = _make_fav_id(bet["player"], bet["bet_type"]) in _fav_ids_set
                render_bet_card(bet, i, len(enriched), is_fav=_is_fav)

        st.caption("⚠️ Statistische analyse ter ondersteuning. Wedden brengt financiële risico's. Speel verantwoord.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — FAVORIETEN
# ══════════════════════════════════════════════════════════════════════════════

with tab_favorieten:
    st.markdown("### ⭐ Favorieten")

    _favs    = load_favorieten()
    _res_map = {r["id"]: r for r in load_resultaten()}

    if not _favs:
        st.info("Nog geen favorieten. Klik op ⭐ in een prop-kaart om te bewaren.")
    else:
        # Samenvatting bovenaan als er resultaten zijn
        _done_favs = [r for r in load_resultaten() if r.get("uitkomst") in ("gewonnen", "verloren")]
        if _done_favs:
            _fn_won  = sum(1 for r in _done_favs if r.get("uitkomst") == "gewonnen")
            _fn_lost = len(_done_favs) - _fn_won
            _ft_inzet = sum(r.get("inzet", 0) for r in _done_favs)
            _ft_wl    = sum(r.get("winst_verlies", 0) for r in _done_favs)
            _froi     = (_ft_wl / _ft_inzet * 100) if _ft_inzet > 0 else 0.0
            _fc1, _fc2, _fc3, _fc4 = st.columns(4)
            _fc1.metric("✅ Gewonnen", _fn_won)
            _fc2.metric("❌ Verloren", _fn_lost)
            _fc3.metric("💰 P&L", f"€{_ft_wl:+.2f}")
            _fc4.metric("📈 ROI", f"{_froi:+.1f}%")
            st.markdown("---")

        for _idx, _fav in enumerate(_favs):
            _fid      = _fav.get("id", "")
            _res      = _res_map.get(_fid, {})
            _uitkomst = _res.get("uitkomst", "")
            _icon = "✅" if _uitkomst == "gewonnen" else ("❌" if _uitkomst == "verloren" else "⏳")
            _ev_disp  = f"{float(_fav.get('ev_score', 0)):+.3f}"

            with st.expander(
                f"{_icon} {_fav.get('speler','')} · {_fav.get('bet','')} "
                f"@ {_fav.get('odds','')}  |  EV {_ev_disp}  |  {_fav.get('datum','')}",
                expanded=(_uitkomst == ""),
            ):
                _ci, _cd = st.columns([4, 1])
                with _ci:
                    _cap = f"Sport: {_fav.get('sport','')} · Bet365: {_fav.get('bet365_status','')}"
                    if _res:
                        _cap += f"  ·  Inzet: €{_res.get('inzet',0):.2f}  ·  P&L: €{_res.get('winst_verlies',0):+.2f}"
                    st.caption(_cap)
                with _cd:
                    if st.button("🗑️", key=f"delfav_{_fid}_{_idx}", help="Verwijder favoriet"):
                        remove_favoriet(_fid)
                        remove_resultaat(_fid)
                        st.rerun()

                _inzet_default = float(_res.get("inzet", 10.0))
                _inzet = st.number_input(
                    "💰 Inzet (€)", min_value=0.10, value=_inzet_default,
                    step=1.0, format="%.2f", key=f"inzet_{_fid}_{_idx}",
                )
                _cw, _cl, _cp = st.columns(3)
                with _cw:
                    if st.button("✅ Gewonnen", key=f"won_{_fid}_{_idx}", use_container_width=True):
                        upsert_resultaat(_fid, _fav, "gewonnen", _inzet)
                        st.rerun()
                with _cl:
                    if st.button("❌ Verloren", key=f"lost_{_fid}_{_idx}", use_container_width=True):
                        upsert_resultaat(_fid, _fav, "verloren", _inzet)
                        st.rerun()
                with _cp:
                    if st.button("⏳ Reset", key=f"reset_{_fid}_{_idx}", use_container_width=True):
                        remove_resultaat(_fid)
                        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — BANKROLL
# ══════════════════════════════════════════════════════════════════════════════

with tab_bankroll:
    st.markdown("### 📊 Bankroll Tracker")

    # ── Filters ──────────────────────────────────────────────────────────────
    st.markdown("#### 🔎 Filters")
    _bkf1, _bkf2, _bkf3, _bkf4 = st.columns(4)
    _bk_sport  = _bkf1.selectbox("Sport",   ["Alles","NHL","NBA","MLB","Voetbal"], key="bk_sport")
    _bk_btype  = _bkf2.selectbox("Bet type",
        ["Alles","Goals","Assists","Shots on Goal","Blocked Shots",
         "Hits","Points","Home Runs","Strikeouts","Over/Under"], key="bk_btype")
    _bk_period = _bkf3.selectbox("Periode", ["Alles","Laatste 7 dagen","Laatste 30 dagen"], key="bk_period")
    _bk_kind   = _bkf4.selectbox("Type",    ["Alles","Singles","Parlays"], key="bk_kind")
    st.markdown("---")

    _alle_res = load_resultaten()

    import datetime as _dt_bk2
    _today_bk2 = _dt_bk2.date.today()

    def _bk_filter(r: dict) -> bool:
        if _bk_sport != "Alles" and _bk_sport.lower() not in (r.get("sport","") or "").lower():
            return False
        if _bk_btype != "Alles" and _bk_btype.lower() not in (r.get("bet_type","") or "").lower():
            return False
        if _bk_period != "Alles":
            try:
                rd = _dt_bk2.date.fromisoformat((r.get("datum") or "")[:10])
                days = 7 if "7" in _bk_period else 30
                if (_today_bk2 - rd).days > days:
                    return False
            except Exception:
                pass
        if _bk_kind == "Singles" and r.get("is_parlay"):
            return False
        if _bk_kind == "Parlays" and not r.get("is_parlay"):
            return False
        return True

    _alle_res = [r for r in _alle_res if _bk_filter(r)]
    _gedaan   = [r for r in _alle_res if r.get("uitkomst") in ("gewonnen", "verloren")]

    if not _gedaan:
        st.info(
            "Nog geen afgeronde weddenschappen. "
            "Markeer props als ✅/❌ in het **⭐ Favorieten** tabblad."
        )
    else:
        import pandas as pd

        # ── Overzicht ──────────────────────────────────────────────────────────
        st.markdown("#### 🎯 Overzicht")
        _bn_won   = sum(1 for r in _gedaan if r.get("uitkomst") == "gewonnen")
        _bn_lost  = len(_gedaan) - _bn_won
        _bt_inzet = sum(r.get("inzet", 0) for r in _gedaan)
        _bt_wl    = sum(r.get("winst_verlies", 0) for r in _gedaan)
        _broi     = (_bt_wl / _bt_inzet * 100) if _bt_inzet > 0 else 0.0
        _bwin_pct = (_bn_won / len(_gedaan) * 100) if _gedaan else 0.0

        _bc1, _bc2, _bc3, _bc4 = st.columns(4)
        _bc1.metric("💰 Totaal P&L",  f"€{_bt_wl:+.2f}")
        _bc2.metric("📈 ROI",          f"{_broi:+.1f}%")
        _bc3.metric("🎯 Win %",        f"{_bwin_pct:.1f}%")
        _bc4.metric("📊 W / L",        f"{_bn_won} / {_bn_lost}")

        # ── Cumulatieve P&L grafiek ────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 📈 P&L over tijd")
        _sorted_res = sorted(_gedaan, key=lambda r: r.get("datum", ""))
        if len(_sorted_res) >= 2:
            _cum_wl = 0.0
            _chart_rows = []
            for _r in _sorted_res:
                _cum_wl += _r.get("winst_verlies", 0)
                _chart_rows.append({
                    "Datum":   _r.get("datum", ""),
                    "P&L (€)": round(_cum_wl, 2),
                })
            _df_chart = pd.DataFrame(_chart_rows).set_index("Datum")
            st.line_chart(_df_chart)
        else:
            st.caption("Minimaal 2 afgeronde weddenschappen nodig voor een grafiek.")

        # ── Per sport ──────────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 🏟️ Per sport")
        _bsports = sorted({r.get("sport", "?") for r in _gedaan})
        for _bsport in _bsports:
            _sr   = [r for r in _gedaan if r.get("sport", "") == _bsport]
            _sw   = sum(1 for r in _sr if r.get("uitkomst") == "gewonnen")
            _si   = sum(r.get("inzet", 0) for r in _sr)
            _swl  = sum(r.get("winst_verlies", 0) for r in _sr)
            _sroi = (_swl / _si * 100) if _si > 0 else 0.0
            _sico = SPORT_ICONS.get(_bsport.upper(), "⚽")

            with st.expander(
                f"{_sico} {_bsport}  —  P&L: €{_swl:+.2f}  |  ROI: {_sroi:+.1f}%",
                expanded=True,
            ):
                _sc1, _sc2, _sc3 = st.columns(3)
                _sc1.metric("W / L",       f"{_sw} / {len(_sr) - _sw}")
                _sc2.metric("Totale inzet", f"€{_si:.2f}")
                _sc3.metric("P&L",          f"€{_swl:+.2f}")

                # Beste bet type per sport
                _btype_wl = {}
                for _r in _sr:
                    _bt = _r.get("bet", "?")
                    _btype_wl.setdefault(_bt, 0.0)
                    _btype_wl[_bt] += _r.get("winst_verlies", 0)
                if _btype_wl:
                    _best_bt  = max(_btype_wl, key=lambda k: _btype_wl[k])
                    _best_val = _btype_wl[_best_bt]
                    if _best_val > 0:
                        st.caption(f"✨ Meest winstgevend: **{_best_bt}** (€{_best_val:+.2f})")

        # ── EV Analyse ─────────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 🔬 EV Analyse")
        st.caption(
            "Vergelijkt voorspelde hit rate (via EV + odds) met werkelijk win % per sport. "
            "Minimaal 3 weddenschappen per sport nodig."
        )
        _ev_rows = []
        for _bsport in _bsports:
            _sr = [r for r in _gedaan if r.get("sport", "") == _bsport]
            if len(_sr) < 3:
                continue
            # Voorspelde hit rate: hr = (ev + 1) / odds
            _pred_hrs = []
            for _r in _sr:
                _ev_val  = float(_r.get("ev_score", 0))
                _odds_r  = float(_r.get("odds", 2.0))
                if _odds_r > 1.0:
                    _pred_hrs.append((_ev_val + 1) / _odds_r)
            if not _pred_hrs:
                continue
            _pred_hr   = sum(_pred_hrs) / len(_pred_hrs)
            _actual_hr = sum(1 for r in _sr if r.get("uitkomst") == "gewonnen") / len(_sr)
            _diff      = _actual_hr - _pred_hr
            _ev_rows.append({
                "Sport":        _bsport,
                "Voorspeld HR": f"{_pred_hr*100:.1f}%",
                "Werkelijk HR": f"{_actual_hr*100:.1f}%",
                "Verschil":     f"{_diff*100:+.1f}%",
                "N":            len(_sr),
            })
            # Waarschuwing bij >20% onder verwacht
            if _pred_hr > 0 and _actual_hr < _pred_hr * 0.80:
                st.warning(
                    f"⚠️ {_bsport} props presteren {abs(_diff)*100:.0f}% onder verwachte hit rate "
                    f"— overweeg filters aan te passen"
                )
        if _ev_rows:
            st.dataframe(
                pd.DataFrame(_ev_rows),
                hide_index=True,
                use_container_width=True,
            )
        elif _gedaan:
            st.caption("Minimaal 3 afgeronde weddenschappen per sport nodig voor EV analyse.")

        # ── Statistieken per bet type ─────────────────────────────────────────
        if _gedaan:
            st.markdown("---")
            st.markdown("#### 📊 Per bet type")
            _bt_agg: dict = {}
            for _r in _gedaan:
                _bt = (_r.get("bet") or _r.get("bet_type") or "Onbekend").split(" ")[0]
                if _bt not in _bt_agg:
                    _bt_agg[_bt] = {"n": 0, "won": 0, "ev": 0.0, "wv": 0.0}
                _bt_agg[_bt]["n"]  += 1
                if (_r.get("uitkomst") or "") == "gewonnen":
                    _bt_agg[_bt]["won"] += 1
                _bt_agg[_bt]["ev"] += float(_r.get("ev_score") or 0)
                _bt_agg[_bt]["wv"] += float(_r.get("winst_verlies") or 0)
            _bt_rows2 = []
            for _bt, _s in sorted(_bt_agg.items(), key=lambda x: x[1]["n"], reverse=True):
                _wp  = _s["won"] / _s["n"] * 100 if _s["n"] else 0
                _roi = _s["wv"] / (_s["n"] * 10) * 100 if _s["n"] else 0
                _aev = _s["ev"] / _s["n"] if _s["n"] else 0
                _bt_rows2.append({
                    "Bet Type": _bt,
                    "N":        _s["n"],
                    "Win %":    f"{_wp:.0f}%",
                    "ROI":      f"{_roi:.0f}%",
                    "Gem. EV":  f"{_aev:.3f}",
                })
            if _bt_rows2:
                try:
                    import pandas as _pd_bt
                    st.dataframe(_pd_bt.DataFrame(_bt_rows2), use_container_width=True, hide_index=True)
                except ImportError:
                    for _row in _bt_rows2:
                        st.write(_row)

        # ── Parlays in bankroll ───────────────────────────────────────────────
        _all_parlays_bk = db.load_parlays()
        if _all_parlays_bk:
            st.markdown("---")
            st.markdown("#### 🎯 Parlay ROI")
            _p_n    = len(_all_parlays_bk)
            _p_won  = sum(1 for p in _all_parlays_bk if (p.get("uitkomst") or "") == "gewonnen")
            _p_inzet = sum(float(p.get("inzet") or 10) for p in _all_parlays_bk)
            _p_wv   = sum(float(p.get("winst_verlies") or 0) for p in _all_parlays_bk)
            _p_roi  = _p_wv / _p_inzet * 100 if _p_inzet else 0
            _pc1, _pc2, _pc3, _pc4 = st.columns(4)
            _pc1.metric("Parlays gespeeld", _p_n)
            _pc2.metric("Gewonnen",         f"{_p_won}/{_p_n}")
            _pc3.metric("Totaal W/V",       f"€{_p_wv:.2f}")
            _pc4.metric("Parlay ROI",       f"{_p_roi:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — GESCHIEDENIS
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# TAB — PARLAY BUILDER
# ══════════════════════════════════════════════════════════════════════════════

with tab_parlay:
    st.markdown("### 🎯 Parlay Builder")
    st.caption("Combineer props tot een parlay en bereken de gecombineerde EV")

    if "parlay_legs" not in st.session_state:
        st.session_state.parlay_legs = []

    _la = st.session_state.get("last_analysis") or {}
    all_props_parlay = _la.get("enriched_ranked") or _la.get("enriched") or []
    if all_props_parlay:
        zoek = st.text_input("🔍 Zoek speler of sport", key="parlay_search",
                             placeholder="Bijv. McDavid, NHL, Goals...")
        zoek_l = zoek.lower() if zoek else ""
        filtered_p = [b for b in all_props_parlay
                      if not zoek_l
                      or zoek_l in (b.get("player") or "").lower()
                      or zoek_l in (b.get("sport") or "").lower()
                      or zoek_l in (b.get("bet_type") or "").lower()]
        if filtered_p:
            st.markdown("**Beschikbare props uit laatste analyse:**")
            for b in filtered_p[:25]:
                pc1, pc2, pc3 = st.columns([3, 1, 1])
                pc1.write(f"{b.get('player','?')} — {b.get('bet_type','?')}")
                pc2.write(f"Odds: {b.get('odds','—')}")
                _already = any(
                    l.get("player") == b.get("player") and l.get("bet_type") == b.get("bet_type")
                    for l in st.session_state.parlay_legs
                )
                if not _already:
                    if pc3.button("+ Voeg toe", key=f"addleg_{b.get('player','')}_{b.get('bet_type','')}"):
                        st.session_state.parlay_legs.append({
                            "player":   b.get("player", ""),
                            "sport":    b.get("sport", ""),
                            "bet_type": b.get("bet_type", ""),
                            "odds":     float(b.get("odds") or 1.5),
                            "hit_rate": float(b.get("composite") or b.get("linemate_hr") or 0.5),
                        })
                        st.rerun()
                else:
                    pc3.caption("✅ Toegevoegd")
    else:
        st.info("Voer eerst een analyse uit om props te kunnen toevoegen aan de parlay.")

    st.markdown("---")

    if st.session_state.parlay_legs:
        st.markdown("#### 🧩 Jouw Parlay")
        legs_to_remove = []
        for _li, _leg in enumerate(st.session_state.parlay_legs):
            lc1, lc2, lc3, lc4 = st.columns([3, 1, 1, 0.5])
            lc1.write(f"**{_leg.get('player','')}** — {_leg.get('bet_type','')}")
            _new_odds = lc2.number_input(
                "Odds", min_value=1.01, max_value=50.0,
                value=float(_leg.get("odds", 1.5)), step=0.05, format="%.2f",
                key=f"pleg_odds_{_li}",
            )
            st.session_state.parlay_legs[_li]["odds"] = _new_odds
            lc3.caption(f"HR: {_leg.get('hit_rate', 0)*100:.0f}%")
            if lc4.button("🗑️", key=f"rmleg_{_li}"):
                legs_to_remove.append(_li)
        for _idx in sorted(legs_to_remove, reverse=True):
            st.session_state.parlay_legs.pop(_idx)
        if legs_to_remove:
            st.rerun()

        _legs = st.session_state.parlay_legs
        _comb_odds = 1.0
        _hit_ch    = 1.0
        for _leg in _legs:
            _comb_odds *= float(_leg.get("odds", 1.5))
            _hit_ch    *= float(_leg.get("hit_rate", 0.5))
        _p_ev = _hit_ch * (_comb_odds - 1) - (1 - _hit_ch)

        _inzet = st.number_input("💰 Inzet (€)", min_value=1.0, max_value=10000.0,
                                 value=10.0, step=1.0, key="parlay_inzet")
        _winst = _inzet * _comb_odds - _inzet

        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _mc1.metric("Gecombineerde Odds", f"{_comb_odds:.2f}")
        _mc2.metric("Hit Kans", f"{_hit_ch*100:.1f}%")
        _ev_s2 = f"+{_p_ev:.3f}" if _p_ev >= 0 else f"{_p_ev:.3f}"
        _mc3.metric("Parlay EV", _ev_s2)
        _mc4.metric(f"Winst bij €{_inzet:.0f}", f"€{_winst:.2f}")

        if _p_ev < 0:
            st.warning(f"⚠️ Negatieve EV ({_ev_s2}) — verliesgevend op lange termijn.")
        else:
            st.success(f"✅ Positieve EV ({_ev_s2})")

        _pb_col1, _pb_col2 = st.columns(2)
        if _pb_col1.button("⭐ Sla parlay op", use_container_width=True, type="primary"):
            import uuid as _puuid, datetime as _pdt
            _parlay_obj = {
                "id":                 str(_puuid.uuid4())[:8],
                "datum":              _pdt.datetime.now().isoformat(),
                "props_json":         list(_legs),
                "gecombineerde_odds": round(_comb_odds, 4),
                "hit_kans":           round(_hit_ch, 6),
                "ev_score":           round(_p_ev, 6),
                "inzet":              float(_inzet),
                "uitkomst":           "open",
                "winst_verlies":      0.0,
                "legs_json":          {
                    l.get("player","") + "_" + l.get("bet_type",""): "open"
                    for l in _legs
                },
            }
            db.save_parlay(_parlay_obj)
            st.session_state.parlay_legs = []
            st.success("✅ Parlay opgeslagen!")
            st.rerun()
        if _pb_col2.button("🗑️ Wis parlay", use_container_width=True):
            st.session_state.parlay_legs = []
            st.rerun()
    else:
        st.info("Voeg props toe om een parlay te bouwen.")

    _saved_parlays = db.load_parlays()
    if _saved_parlays:
        st.markdown("---")
        st.markdown("#### 📋 Opgeslagen Parlays")
        for _prl in _saved_parlays:
            _prl_legs = _prl.get("props_json") or []
            _prl_lj   = _prl.get("legs_json") or {}
            if isinstance(_prl_lj, str):
                try:
                    _prl_lj = __import__("json").loads(_prl_lj)
                except Exception:
                    _prl_lj = {}
            _prl_ev_s = f"+{_prl.get('ev_score',0):.3f}" if (_prl.get('ev_score') or 0) >= 0 else f"{_prl.get('ev_score',0):.3f}"
            with st.expander(
                f"🎯 {len(_prl_legs)} legs · Odds {_prl.get('gecombineerde_odds',0):.2f}"
                f" · EV {_prl_ev_s} · {(_prl.get('uitkomst') or 'open').upper()}"
            ):
                _upd_legs = dict(_prl_lj)
                _changed  = False
                for _pleg in _prl_legs:
                    _lk  = str(_pleg.get("player","")) + "_" + str(_pleg.get("bet_type",""))
                    _lst = _upd_legs.get(_lk, "open")
                    _plc1, _plc2 = st.columns([3, 2])
                    _plc1.write(f"**{_pleg.get('player','')}** — {_pleg.get('bet_type','')} @ {_pleg.get('odds','—')}")
                    _nst = _plc2.selectbox(
                        "Status",
                        options=["open", "geraakt", "gemist"],
                        index=["open","geraakt","gemist"].index(_lst) if _lst in ["open","geraakt","gemist"] else 0,
                        key=f"legst_{_prl.get('id','')}_{_lk}",
                        label_visibility="collapsed",
                    )
                    if _nst != _lst:
                        _upd_legs[_lk] = _nst
                        _changed = True
                if _changed:
                    db.update_parlay(_prl.get("id",""), {"legs_json": _upd_legs})
                    st.rerun()

                _oc1, _oc2, _oc3 = st.columns(3)
                if (_prl.get("uitkomst") or "open") == "open":
                    if _oc1.button("✅ Gewonnen", key=f"pwon_{_prl.get('id','')}"):
                        _pw = _prl.get("inzet",10) * _prl.get("gecombineerde_odds",1) - _prl.get("inzet",10)
                        db.update_parlay(_prl.get("id",""), {"uitkomst":"gewonnen","winst_verlies":round(_pw,2)})
                        st.rerun()
                    if _oc2.button("❌ Verloren", key=f"plost_{_prl.get('id','')}"):
                        db.update_parlay(_prl.get("id",""), {"uitkomst":"verloren","winst_verlies":-_prl.get("inzet",10)})
                        st.rerun()
                else:
                    _wv = _prl.get("winst_verlies", 0) or 0
                    _kl = "#4ade80" if _wv >= 0 else "#f87171"
                    st.markdown(f"<span style='color:{_kl};font-weight:700'>Uitkomst: {(_prl.get('uitkomst') or '').upper()} · W/V: €{_wv:.2f}</span>",
                                unsafe_allow_html=True)
                if _oc3.button("🗑️ Verwijder", key=f"pdel_{_prl.get('id','')}"):
                    db.delete_parlay(_prl.get("id",""))
                    st.rerun()


with tab_history:
    st.markdown("### 📋 Analysegeschiedenis (laatste 7 dagen)")

    # ── Filters ──────────────────────────────────────────────────────────────
    _hf1, _hf2 = st.columns(2)
    _hist_sport = _hf1.selectbox("Filter sport",
        ["Alles","NHL","NBA","MLB","Voetbal"], key="hist_sport_flt")
    _hist_btype = _hf2.selectbox("Filter bet type",
        ["Alles","Goals","Assists","Shots","Points","Hits","Home Runs","Strikeouts"],
        key="hist_btype_flt")
    st.markdown("---")

    _all_hist = load_history()

    if not _all_hist:
        st.info("Nog geen analyses opgeslagen. Voer een analyse uit om de geschiedenis te vullen.")
    else:
        for entry in _all_hist:
            datum = entry.get("datum", "")
            tijd  = entry.get("tijd", "")
            top5  = entry.get("top5", [])

            # Gebruik alle_props_json als die beschikbaar is, anders top5
            _alle_p = entry.get("alle_props_json") or []
            if isinstance(_alle_p, str):
                try:
                    import json as _jh
                    _alle_p = _jh.loads(_alle_p)
                except Exception:
                    _alle_p = []

            # Gebruik top5 als alle_props_json leeg is (oudere analyses)
            if not _alle_p:
                _alle_p = [
                    {
                        "player":   b.get("speler", b.get("player", "")),
                        "sport":    b.get("sport", ""),
                        "bet_type": b.get("bet", b.get("bet_type", "")),
                        "odds":     b.get("odds", ""),
                        "ev":       float((b.get("ev_score") or "0").replace("+","")) if isinstance(b.get("ev_score"), str) else float(b.get("ev") or 0),
                        "composite": 0,
                        "rating":   b.get("rating", ""),
                    }
                    for b in top5
                ]

            # Filters toepassen
            _filt_p = _alle_p
            if _hist_sport != "Alles":
                _filt_p = [p for p in _filt_p if _hist_sport.lower() in (p.get("sport") or "").lower()]
            if _hist_btype != "Alles":
                _filt_p = [p for p in _filt_p if _hist_btype.lower() in (p.get("bet_type") or "").lower()]

            if not _filt_p:
                continue

            with st.expander(f"📅 {datum} om {tijd}  —  {len(_filt_p)} props", expanded=False):
                for _hp in _filt_p:
                    _hpc1, _hpc2, _hpc3, _hpc4 = st.columns([3, 1, 1, 1])
                    _hpc1.write(
                        f"**{_hp.get('player', _hp.get('speler',''))}** — "
                        f"{_hp.get('bet_type', _hp.get('bet',''))}"
                    )
                    _hpc2.write(f"@ {_hp.get('odds','—')}")
                    _hev   = float(_hp.get("ev") or 0)
                    _hev_s = f"+{_hev:.3f}" if _hev >= 0 else f"{_hev:.3f}"
                    _hpc3.write(f"EV: {_hev_s}")

                    _hpk = f"hpar_{datum}_{_hp.get('player',_hp.get('speler',''))}_{_hp.get('bet_type',_hp.get('bet',''))}"
                    if _hpc4.button("🎯 Parlay", key=_hpk[:60]):
                        if "parlay_legs" not in st.session_state:
                            st.session_state.parlay_legs = []
                        st.session_state.parlay_legs.append({
                            "player":   _hp.get("player", _hp.get("speler", "")),
                            "sport":    _hp.get("sport", ""),
                            "bet_type": _hp.get("bet_type", _hp.get("bet", "")),
                            "odds":     float(_hp.get("odds") or 1.5),
                            "hit_rate": float(_hp.get("composite") or 0.5),
                        })
                        st.rerun()
