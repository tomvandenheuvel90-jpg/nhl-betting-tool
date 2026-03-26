#!/usr/bin/env python3
"""
Bet Analyzer — Streamlit Cloud versie
Ondersteunt: NHL · NBA · MLB · Voetbal (EPL/La Liga/Bundesliga/Serie A/Ligue 1)
"""

import streamlit as st
import os
import json
import base64
import tempfile
import datetime
from pathlib import Path

# ─── Secrets injecteren vóór import van sports modules ────────────────────────
# soccer.py leest FOOTBALL_DATA_API_KEY bij module-import; zet env var vroegtijdig
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

from sports import nhl, nba, mlb, soccer
from scorer import composite_score, ev, rating

# Patch soccer API key (voor het geval de env var na module-import is gewijzigd)
try:
    soccer.API_KEY = st.secrets.get("FOOTBALL_DATA_TOKEN", "")
except Exception:
    pass

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# ─── Constanten ───────────────────────────────────────────────────────────────

SOCCER_COMPS = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}

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

2. "matches": ALLE Flashscore wedstrijden. Elk item:
   - "home_team": naam thuisploeg (string)
   - "away_team": naam uitploeg (string)
   - "home_form": laatste 5 resultaten thuisploeg, bijv. "WWDLW", of null
   - "away_form": idem voor uitploeg, of null
   - "h2h": korte H2H samenvatting indien zichtbaar, bijv. "Arsenal won 3/5", of null
   - "competition": competitienaam (bijv. "Premier League"), of null
   - "date": datum indien zichtbaar (bijv. "2025-03-25"), of null
   - "status": "gepland", "bezig" of "afgelopen"
   - "score": score indien zichtbaar (bijv. "2-1"), of null

Als er geen Linemate screenshots zijn, geef dan een lege array voor "bets".
Als er geen Flashscore screenshots zijn, geef dan een lege array voor "matches".
Geef ALLEEN het JSON object terug, geen andere tekst.
"""

FLASHSCORE_PROMPT = """
Je bent een expert sportsbetting analist. Analyseer de volgende wedstrijden en props in het Nederlands.

## WEDSTRIJDEN (Flashscore)
{matches_json}

## PROPS (Linemate — al gescoord)
{bets_json}

## STAP 2 — FLASHSCORE ANALYSE
Geef een scoretabel voor de wedstrijden:
| Wedstrijd | Thuis vorm | Uit vorm | H2H | Advies |
|---|---|---|---|---|

Daarna: **Top 3 wedstrijden** om op te focussen, met 1-zin uitleg per wedstrijd.

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

# Donker mobiel-vriendelijk thema via CSS
st.markdown("""
<style>
  .block-container { max-width: 720px; padding-top: 1.5rem; }
  div[data-testid="stFileUploaderDropzone"] { background: #1a1a2e; border: 2px dashed #3a3a6a; }
  .bet-card { background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
  .rating-strong { color: #4ade80; font-weight: 700; }
  .rating-matig  { color: #facc15; font-weight: 700; }
  .rating-vermijd { color: #f87171; font-weight: 700; }
  .ev-positive { color: #4ade80; font-size: 1.3rem; font-weight: 800; }
  .ev-low      { color: #facc15; font-size: 1.3rem; font-weight: 800; }
</style>
""", unsafe_allow_html=True)


# ─── Wachtwoord ───────────────────────────────────────────────────────────────

def _check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True

    st.markdown("## 🎯 Bet Analyzer")
    pwd = st.text_input("Wachtwoord", type="password", key="pwd_input")
    if st.button("Inloggen", use_container_width=True):
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


# ─── Daycache (sessie-level, wordt per deploy niet bewaard) ───────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _player_cache_today():
    return {}


# ─── Extractie via Claude Haiku ───────────────────────────────────────────────

def extract_bets(client, image_paths: list):
    content = []
    for path in image_paths:
        ext = Path(path).suffix.lower()
        media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".png": "image/png", ".webp": "image/webp"}
        media_type = media_map.get(ext, "image/jpeg")
        with open(path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_data},
        })
    content.append({"type": "text", "text": EXTRACT_PROMPT})

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().strip("```")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data, []
        return data.get("bets", []), data.get("matches", [])
    except Exception:
        return [], []


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


# ─── Bet verrijken ────────────────────────────────────────────────────────────

def enrich_bet(bet: dict, cache: dict) -> dict:
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

    odds  = bet.get("linemate_odds", 1.0)
    score = composite_score(
        linemate_hit_rate=bet.get("hit_rate", 0.5),
        sample_size=sample_n,
        bet_type=bet_type,
        player_stats=player_stats,
        opponent_stats=opponent_stats,
        sport=sport,
    )
    ev_score = ev(score["composite"], odds)
    rat      = rating(ev_score, score["composite"])

    return {
        "player":      player_name,
        "sport":       bet.get("sport", "?"),
        "bet_type":    bet_type,
        "odds":        odds,
        "sample":      bet.get("sample", "?"),
        "linemate_hr": score["linemate_hr"],
        "season_hr":   score["season_hr"],
        "composite":   score["composite"],
        "ev":          ev_score,
        "rating":      rat,
        "opponent":    opponent_name,
        "gaa":         opponent_stats.get("goals_against_avg"),
        "source":      player_stats.get("source", ""),
    }


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


def render_top3(top3: list):
    st.markdown("### 🏆 Top prop aanbevelingen")
    for i, b in enumerate(top3, 1):
        ev_str = f"+{b['ev']:.3f}" if b['ev'] >= 0 else f"{b['ev']:.3f}"
        st.markdown(
            f"**{i}. {b['player']}** · {b['bet_type']} @ {b['odds']}  "
            f"&nbsp;&nbsp; EV `{ev_str}`"
        )


def render_bet_card(bet: dict, rank: int, total: int):
    sport_icon = SPORT_ICONS.get(bet["sport"].upper(), "⚽")
    ev_str     = f"+{bet['ev']:.3f}" if bet["ev"] >= 0 else f"{bet['ev']:.3f}"
    composite_pct = int(bet["composite"] * 100)
    rat_color  = _rating_color(bet["rating"])

    with st.container():
        st.markdown(
            f"<div style='background:#1a1a2e;border:1px solid #2a2a4a;"
            f"border-radius:12px;padding:16px;margin-bottom:12px;'>",
            unsafe_allow_html=True,
        )

        # Header row
        col_l, col_r = st.columns([3, 1])
        with col_l:
            st.markdown(f"**{sport_icon} #{rank} van {total}**")
        with col_r:
            st.markdown(
                f"<span style='color:{rat_color};font-weight:700;'>{bet['rating']}</span>",
                unsafe_allow_html=True,
            )

        # Speler + bet type
        st.markdown(f"#### {bet['player']}")
        st.caption(f"{bet['bet_type']} · {bet['sport']}")

        # EV groot
        ev_color = "#4ade80" if bet["ev"] >= 0.05 else "#facc15"
        st.markdown(
            f"<span style='color:{ev_color};font-size:1.4rem;font-weight:800;'>EV {ev_str}</span>",
            unsafe_allow_html=True,
        )

        # Stats raster
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Linemate HR", f"{bet['linemate_hr']*100:.1f}%")
        c2.metric("Seizoens HR", f"{bet['season_hr']*100:.1f}%")
        c3.metric("Odds", f"{bet['odds']}")
        c4.metric("Sample", bet["sample"])

        # Composite progress
        st.progress(bet["composite"], text=f"Composite: {composite_pct}%")

        # Extra info
        info_parts = []
        if bet.get("opponent"):
            info_parts.append(f"vs {bet['opponent']}")
        if bet.get("gaa"):
            info_parts.append(f"GAA {bet['gaa']}")
        if bet.get("source"):
            info_parts.append(f"Bron: {bet['source']}")
        if info_parts:
            st.caption(" · ".join(info_parts))

        st.markdown("</div>", unsafe_allow_html=True)


# ─── Hoofdscherm ──────────────────────────────────────────────────────────────

st.markdown("## 🎯 Bet Analyzer")
st.caption("Linemate + Flashscore · NHL · NBA · MLB · Voetbal")

# API key ophalen
try:
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

if not api_key:
    st.error("❌ Geen `ANTHROPIC_API_KEY` gevonden in st.secrets. Voeg deze toe in de Streamlit Cloud instellingen.")
    st.stop()

if not ANTHROPIC_AVAILABLE:
    st.error("❌ `anthropic` pakket niet geïnstalleerd. Voeg `anthropic` toe aan requirements.txt.")
    st.stop()

# Moneypuck lokale data check
try:
    from sports.moneypuck_local import DATA_DIR as MP_DATA_DIR
    if not MP_DATA_DIR.exists():
        st.info(
            "ℹ️ **Cloud versie** — Historische MoneyPuck data niet beschikbaar. "
            "NHL scoring gebruikt alleen live seizoensdata (licht verminderde nauwkeurigheid)."
        )
except Exception:
    pass

# Uploader
uploaded_files = st.file_uploader(
    "Upload Linemate en/of Flashscore screenshots",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    help="Je kunt meerdere screenshots tegelijk uploaden",
)

if uploaded_files:
    cols = st.columns(min(len(uploaded_files), 4))
    for i, f in enumerate(uploaded_files):
        cols[i % 4].image(f, use_container_width=True)

analyze_btn = st.button(
    "🔍 Analyseer",
    use_container_width=True,
    disabled=not uploaded_files,
    type="primary",
)

if analyze_btn and uploaded_files:
    tmp_paths = []
    try:
        # Sla uploads op als tijdelijke bestanden
        for f in uploaded_files:
            suffix = Path(f.name).suffix or ".png"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(f.read())
            tmp.flush()
            tmp_paths.append(tmp.name)

        client = anthropic.Anthropic(api_key=api_key)

        with st.status("⏳ Analyseren...", expanded=True) as status:
            # Stap 1: extraheer bets + matches
            st.write("📸 Screenshots herkennen...")
            bets, matches = extract_bets(client, tmp_paths)

            if not bets and not matches:
                st.error("Geen bets of wedstrijden gevonden in de afbeeldingen.")
                st.stop()

            st.write(f"✅ Gevonden: {len(bets)} props, {len(matches)} wedstrijden")

            # Stap 2: verrijk props
            if bets:
                st.write("🔎 Spelersdata ophalen...")
                cache: dict = {}
                enriched = []
                prog = st.progress(0)
                for i, bet in enumerate(bets):
                    enriched.append(enrich_bet(bet, cache))
                    prog.progress((i + 1) / len(bets))
                enriched.sort(key=lambda x: x["ev"], reverse=True)
                st.write(f"✅ {len(enriched)} props gescoord")
            else:
                enriched = []

            # Stap 3: Flashscore analyse
            flashscore_text = ""
            if matches:
                st.write("📺 Flashscore analyseren via Claude...")
                flashscore_text = analyze_flashscore(client, matches, enriched)
                st.write("✅ Flashscore analyse klaar")

            status.update(label="✅ Analyse compleet!", state="complete")

        # ── Resultaten weergeven ──

        if flashscore_text:
            render_flashscore(flashscore_text)

        if enriched:
            st.markdown("---")

            # Top 3
            top3 = [b for b in enriched if b["rating"].startswith("✅")][:3]
            if not top3:
                top3 = enriched[:3]
            render_top3(top3)

            # Alle kaarten
            st.markdown("---")
            st.markdown("### 📊 Alle props")
            for i, bet in enumerate(enriched, 1):
                render_bet_card(bet, i, len(enriched))

        st.caption("⚠️ Statistische analyse ter ondersteuning. Wedden brengt financiële risico's. Speel verantwoord.")

    except Exception as e:
        st.error(f"❌ Fout: {e}")
        raise
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass
