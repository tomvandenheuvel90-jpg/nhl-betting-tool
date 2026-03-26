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

SOCCER_COMPS   = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}
HISTORY_FILE   = Path(__file__).parent / "analyse_geschiedenis.json"
HISTORY_DAYS   = 7

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

st.markdown("""
<style>
  .block-container { max-width: 720px; padding-top: 1.5rem; }
  div[data-testid="stFileUploaderDropzone"] { background: #1a1a2e; border: 2px dashed #3a3a6a; }
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


# ─── Geschiedenis helpers ─────────────────────────────────────────────────────

def load_history() -> list:
    """Laad analysegeschiedenis, filter op laatste 7 dagen."""
    if not HISTORY_FILE.exists():
        return []
    try:
        entries = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        cutoff = (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
        return [e for e in entries if e.get("datum", "") >= cutoff]
    except Exception:
        return []


def save_to_history(enriched: list):
    """Sla top 5 bets op in analysegeschiedenis."""
    now  = datetime.datetime.now()
    top5 = enriched[:5]

    entry = {
        "datum": now.strftime("%Y-%m-%d"),
        "tijd":  now.strftime("%H:%M"),
        "top5": [
            {
                "rank":     i + 1,
                "speler":   b["player"],
                "bet":      b["bet_type"],
                "odds":     str(b["odds"]),
                "ev_score": f"{b['ev']:+.3f}",
                "rating":   b["rating"],
            }
            for i, b in enumerate(top5)
        ],
    }

    entries = load_history()
    entries.insert(0, entry)

    # Bewaar alleen laatste 7 dagen
    cutoff = (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    entries = [e for e in entries if e.get("datum", "") >= cutoff]

    try:
        HISTORY_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # Op read-only filesystems: stil falen


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
        temperature=0,
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
                        "hit_rate":      0.0,
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
                    "hit_rate":      0.0,
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
                        "linemate_odds": _REF_ODDS["points"], "hit_rate": 0.0,
                        "sample": "auto", "sample_n": games_n,
                    })
                if avg_reb >= 6:
                    line = math.floor(avg_reb * 0.80) + 0.5
                    props.append({
                        "player": p["name"], "sport": "NBA", "team": str(tid),
                        "bet_type": f"Over {line:.1f} Rebounds",
                        "linemate_odds": _REF_ODDS["rebounds"], "hit_rate": 0.0,
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
                        "linemate_odds": _REF_ODDS["hits"], "hit_rate": 0.0,
                        "sample": "auto", "sample_n": games_n,
                    })
                if avg_tb >= 1.4:
                    props.append({
                        "player": p["name"], "sport": "MLB", "team": str(tid),
                        "bet_type": "Over 1.5 Total Bases",
                        "linemate_odds": _REF_ODDS["total_bases"], "hit_rate": 0.0,
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

    odds  = bet.get("linemate_odds", 1.0)
    score = composite_score(
        linemate_hit_rate=bet.get("hit_rate", 0.5),
        sample_size=sample_n,
        bet_type=bet_type,
        player_stats=player_stats,
        opponent_stats=opponent_stats,
        sport=sport,
        linemate_weight=linemate_weight,
        season_weight=season_weight,
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
    sport_icon    = SPORT_ICONS.get(bet["sport"].upper(), "⚽")
    ev_str        = f"+{bet['ev']:.3f}" if bet["ev"] >= 0 else f"{bet['ev']:.3f}"
    composite_pct = int(bet["composite"] * 100)
    rat_color     = _rating_color(bet["rating"])

    with st.container():
        st.markdown(
            f"<div style='background:#1a1a2e;border:1px solid #2a2a4a;"
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
        st.caption(f"{bet['bet_type']} · {bet['sport']}")

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

# API key
try:
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

if not api_key:
    st.error("❌ Geen `ANTHROPIC_API_KEY` gevonden in st.secrets.")
    st.stop()

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

tab_analyse, tab_history = st.tabs(["🔍 Analyse", "📋 Geschiedenis"])

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
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        help="Je kunt meerdere screenshots tegelijk uploaden",
        key=f"uploader_{st.session_state.uploader_key}",
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
            for f in uploaded_files:
                suffix = Path(f.name).suffix or ".png"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp.write(f.read())
                tmp.flush()
                tmp_paths.append(tmp.name)

            client = anthropic.Anthropic(api_key=api_key)

            with st.status("⏳ Analyseren...", expanded=True) as status:
                st.write("📸 Screenshots herkennen...")
                bets, matches = extract_bets(client, tmp_paths)

                if not bets and not matches:
                    st.error("Geen bets of wedstrijden gevonden in de afbeeldingen.")
                    st.stop()

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
                else:
                    enriched = []

                flashscore_text = ""
                if matches:
                    st.write("📺 Flashscore analyseren via Claude...")
                    flashscore_text = analyze_flashscore(client, matches, enriched)
                    st.write("✅ Flashscore analyse klaar")

                status.update(label="✅ Analyse compleet!", state="complete")

            # Top 3 berekenen
            top3 = [b for b in enriched if b["rating"].startswith("✅")][:3]
            if not top3:
                top3 = enriched[:3]
            top3_out = [{"player": b["player"], "bet_type": b["bet_type"],
                         "odds": b["odds"], "ev": b["ev"]} for b in top3]

            # Opslaan in geschiedenis
            if enriched:
                save_to_history(enriched)

            # Resultaten opslaan in session state
            st.session_state.last_analysis = {
                "enriched":       enriched,
                "top3":           top3_out,
                "flashscore":     flashscore_text,
                "scenario":       scenario,
            }

            # Uploader resetten + rerun
            st.session_state.uploader_key   += 1
            st.session_state.just_analyzed   = True
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
        flashscore_text = res["flashscore"]
        scenario  = res.get("scenario", 3)

        # Scenario label
        st.info(SCENARIO_LABELS.get(scenario, ""))

        # Tip bij Scenario 3: alleen Linemate, geen Flashscore
        if scenario == 3:
            st.info("⚠️ Tip: upload ook een Flashscore screenshot voor wedstrijdcontext en automatische prop-suggesties.")

        if flashscore_text:
            render_flashscore(flashscore_text)

        if enriched:
            st.markdown("---")
            render_top3(top3_out)
            st.markdown("---")
            st.markdown("### 📊 Alle props")
            for i, bet in enumerate(enriched, 1):
                render_bet_card(bet, i, len(enriched))

        st.caption("⚠️ Statistische analyse ter ondersteuning. Wedden brengt financiële risico's. Speel verantwoord.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — GESCHIEDENIS
# ══════════════════════════════════════════════════════════════════════════════

with tab_history:
    st.markdown("### 📋 Analysegeschiedenis (laatste 7 dagen)")

    history = load_history()

    if not history:
        st.info("Nog geen analyses opgeslagen. Voer een analyse uit om de geschiedenis te vullen.")
    else:
        for entry in history:
            datum = entry.get("datum", "")
            tijd  = entry.get("tijd", "")
            top5  = entry.get("top5", [])

            with st.expander(f"📅 {datum} om {tijd}  —  {len(top5)} aanbevelingen", expanded=False):
                if not top5:
                    st.caption("Geen props in deze analyse.")
                    continue

                import pandas as pd
                rows = []
                for b in top5:
                    rows.append({
                        "#":       b.get("rank", ""),
                        "Speler":  b.get("speler", ""),
                        "Bet":     b.get("bet", ""),
                        "Odds":    b.get("odds", ""),
                        "EV":      b.get("ev_score", ""),
                        "Rating":  b.get("rating", ""),
                    })
                st.dataframe(
                    pd.DataFrame(rows),
                    hide_index=True,
                    use_container_width=True,
                )
