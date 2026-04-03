"""
analysis.py — BetAnalyzer kernlogica (geen Streamlit-afhankelijkheden)

Bevat:
  - Afbeeldingsverwerking (HEIC conversie, splitsen, content blocks)
  - JSON parsing/repair
  - extract_bets()  → (bets, matches, debug_info)
  - enrich_bet()
  - _filter_and_rank_props()
  - generate_parlay_suggestions()
  - Auto-props generatie (NHL, NBA, MLB)
  - Scenario- en sportdetectie
  - Voetbal form-verrijking + Flashscore analyse
"""

import os
import re
import json
import base64
import logging
import traceback
import itertools
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# ─── Sports modules ───────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent))

from sports import nhl, nba, mlb, soccer, odds_api
from scorer import composite_score, ev, rating
from prompts import (
    SOCCER_COMPS, _NHL_TEAM_KEYWORDS, _REF_ODDS,
    SCENARIO_WEIGHTS, EXTRACT_PROMPT, FLASHSCORE_PROMPT,
    COMBO_SECTION, EXTRACT_MODEL,
)

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
except ImportError:
    pass

_MEDIA_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",  ".webp": "image/webp",
}


# ─── NHL sportdetectie helper ─────────────────────────────────────────────────

def is_nhl_match(m: dict) -> bool:
    """Detecteer of een wedstrijd NHL is, ook als competitie-veld ontbreekt."""
    sport_comp = (m.get("sport") or m.get("competition") or "").lower()
    if "nhl" in sport_comp or "hockey" in sport_comp:
        return True
    home = (m.get("home_team") or "").lower()
    away = (m.get("away_team") or "").lower()
    return (
        any(kw in home for kw in _NHL_TEAM_KEYWORDS) or
        any(kw in away for kw in _NHL_TEAM_KEYWORDS)
    )


def is_nba_match(m: dict) -> bool:
    s = (m.get("sport") or m.get("competition") or "").upper()
    return "NBA" in s or "BASKETBALL" in s


def is_mlb_match(m: dict) -> bool:
    s = (m.get("sport") or m.get("competition") or "").upper()
    return any(x in s for x in ("MLB", "BASEBALL", "HONKBAL"))


# ─── Afbeeldingshulpfuncties ──────────────────────────────────────────────────

def convert_heic_to_jpeg(path: str) -> str:
    """Converteert HEIC/HEIF naar JPEG. Geeft nieuw pad terug (of origineel bij fout)."""
    suffix = Path(path).suffix.lower()
    if suffix not in (".heic", ".heif") or not _PIL_AVAILABLE:
        return path
    try:
        img = _PILImage.open(path)
        jpeg_path = path.rsplit(".", 1)[0] + "_converted.jpg"
        img.convert("RGB").save(jpeg_path, "JPEG", quality=90)
        return jpeg_path
    except Exception:
        return path


def _split_tall_image(path: str) -> list:
    """Splits afbeeldingen > 1500px hoog in twee overlappende helften."""
    if not _PIL_AVAILABLE:
        return [path]
    try:
        img = _PILImage.open(path)
        w, h = img.size
        if h <= 1500:
            return [path]
        overlap = int(h * 0.10)
        mid = h // 2
        top = img.crop((0, 0, w, mid + overlap))
        bot = img.crop((0, mid - overlap, w, h))
        top_path = path + "_top.jpg"
        bot_path = path + "_bot.jpg"
        top.convert("RGB").save(top_path, "JPEG", quality=90)
        bot.convert("RGB").save(bot_path, "JPEG", quality=90)
        return [top_path, bot_path]
    except Exception:
        return [path]


def _split_image_halves(path: str) -> list:
    """Splits altijd in twee helften (fallback bij afgekapte JSON)."""
    if not _PIL_AVAILABLE:
        return [path]
    try:
        img = _PILImage.open(path)
        w, h = img.size
        if h < 200:
            return [path]
        overlap = int(h * 0.05)
        mid = h // 2
        top = img.crop((0, 0, w, mid + overlap))
        bot = img.crop((0, mid - overlap, w, h))
        top_path = path + "_half_top.jpg"
        bot_path = path + "_half_bot.jpg"
        top.convert("RGB").save(top_path, "JPEG", quality=90)
        bot.convert("RGB").save(bot_path, "JPEG", quality=90)
        return [top_path, bot_path]
    except Exception:
        return [path]


def _image_content_block(path: str) -> dict:
    """Bouw een Anthropic image-content-blok van een lokaal bestandspad."""
    media_type = _MEDIA_MAP.get(Path(path).suffix.lower(), "image/jpeg")
    with open(path, "rb") as fh:
        img_b64 = base64.b64encode(fh.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}}


# ─── JSON parsing ─────────────────────────────────────────────────────────────

def _parse_json_from_text(text: str):
    """Parse JSON uit een Claude-response die mogelijk ```json ... ``` bevat."""
    if not text or not text.strip():
        return None
    clean = text.strip()
    clean = re.sub(r'^```json\s*', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'^```\s*',     '', clean, flags=re.MULTILINE)
    clean = re.sub(r'```\s*$',     '', clean, flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    if start != -1:
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":   depth += 1
            elif ch == "}": depth -= 1
            if depth == 0:  end = i; break
        if end != -1:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    start = text.find("[")
    if start != -1:
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "[":   depth += 1
            elif ch == "]": depth -= 1
            if depth == 0:  end = i; break
        if end != -1:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    return None


def _repair_truncated_json(text: str) -> str:
    """Probeer afgekapt JSON te repareren."""
    clean = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    clean = re.sub(r'^```\s*',     '', clean, flags=re.MULTILINE)
    clean = re.sub(r'```\s*$',     '', clean, flags=re.MULTILINE).strip()
    last_comma = clean.rfind('},')
    if last_comma > 0:
        return clean[:last_comma + 1] + '\n  ]\n}'
    last_brace = clean.rfind('}')
    if last_brace > 0:
        return clean[:last_brace + 1] + '\n  ]\n}'
    return clean


# ─── Deduplicatie ─────────────────────────────────────────────────────────────

def _deduplicate_bets(bets: list) -> list:
    seen = set()
    result = []
    for b in bets:
        key = (str(b.get("player", "")).lower(), str(b.get("bet_type", "")).lower())
        if key not in seen:
            seen.add(key)
            result.append(b)
    return result


def _deduplicate_matches(matches: list) -> list:
    seen = set()
    result = []
    for m in matches:
        key = (str(m.get("home_team", "")).lower(), str(m.get("away_team", "")).lower())
        if key not in seen:
            seen.add(key)
            result.append(m)
    return result


# ─── Extractie via Claude ─────────────────────────────────────────────────────

def extract_bets(client, image_paths: list) -> tuple:
    """
    Stuurt afbeeldingen naar Claude en extraheert bets + matches als JSON.

    Returns: (bets, matches, debug_info)
      debug_info is een dict met keys: _dbg_raw, _dbg_model, _dbg_parse_error,
      _dbg_traceback, _dbg_steps, _dbg_hit_rates
    """
    debug_info: dict = {}
    steps: list = []
    raw = ""

    try:
        steps.append(f"Stap 1: {len(image_paths)} afbeelding(en) verwerken — per screenshot")

        # Bug 2 fix: verwerk elke screenshot afzonderlijk zodat de output per API-call
        # binnen de 8 000-token limiet van claude-haiku-4-5 past. Eerder werden alle
        # afbeeldingen in één call gecombineerd waardoor de JSON werd afgekapt bij ~25
        # props en de rest van de screenshots volledig werd overgeslagen.
        _all_bets: list = []
        _all_matches: list = []

        for _i, _path in enumerate(image_paths):
            steps.append(f"  → Screenshot {_i + 1}/{len(image_paths)}: {Path(_path).name}")

            # Splits hoge afbeeldingen in twee overlappende helften (ongewijzigd)
            _expanded = _split_tall_image(_path)
            steps.append(f"    → {len(_expanded)} blok(ken) na splitsing")

            _content = [_image_content_block(_ep) for _ep in _expanded]
            _content.append({"type": "text", "text": EXTRACT_PROMPT})

            steps.append(f"    → Claude {EXTRACT_MODEL} aanroepen (max_tokens=8000)")
            _resp = client.messages.create(
                model=EXTRACT_MODEL,
                max_tokens=8000,
                temperature=0,
                messages=[{"role": "user", "content": _content}],
            )
            raw = _resp.content[0].text.strip()   # bewaar laatste raw voor debug
            steps.append(f"    → Response: {len(raw)} tekens")
            _log.info(f"[extract_bets] Screenshot {_i + 1}: {len(raw)} tekens")

            _data = _parse_json_from_text(raw)
            if _data is None:
                steps.append("    → Directe parse mislukt, probeer repair_truncated_json")
                _data = _parse_json_from_text(_repair_truncated_json(raw))
                if _data is not None:
                    steps.append("    → repair_truncated_json geslaagd")

            if _data is None:
                # Fallback: splits in helften (voor extreem lange screenshots)
                steps.append("    → Repair mislukt, splits in helften")
                for _half in _split_image_halves(_path):
                    steps.append(f"      → Analyseer helft: {Path(_half).name}")
                    _hc = [_image_content_block(_half), {"type": "text", "text": EXTRACT_PROMPT}]
                    _hr = client.messages.create(
                        model=EXTRACT_MODEL, max_tokens=8000, temperature=0,
                        messages=[{"role": "user", "content": _hc}],
                    )
                    _hraw  = _hr.content[0].text.strip()
                    _hdata = _parse_json_from_text(_hraw) or _parse_json_from_text(_repair_truncated_json(_hraw))
                    if _hdata:
                        if isinstance(_hdata, list):
                            _all_bets.extend(_hdata)
                        else:
                            _all_bets.extend(_hdata.get("bets", []) or [])
                            _all_matches.extend(_hdata.get("matches", []) or [])
            else:
                if isinstance(_data, list):
                    _all_bets.extend(_data)
                else:
                    _all_bets.extend(_data.get("bets", []) or [])
                    _all_matches.extend(_data.get("matches", []) or [])

        bets    = _deduplicate_bets(_all_bets)
        matches = _deduplicate_matches(_all_matches)
        steps.append(f"  → Totaal: {len(bets)} bets, {len(matches)} matches")
        _log.info(f"[extract_bets] {len(bets)} bets, {len(matches)} matches")

        debug_info["_dbg_raw"]   = raw
        debug_info["_dbg_model"] = EXTRACT_MODEL

        if not bets and not matches:
            err = (f"JSON parsing mislukt voor alle screenshots.\n"
                   f"Laatste response ({len(raw)} tekens):\n{raw[:500]}")
            debug_info["_dbg_parse_error"] = err
            steps.append("  ✗ Alle fallbacks mislukt")
            _log.error(f"[extract_bets] {err}")

    except Exception as exc:
        tb = traceback.format_exc()
        debug_info["_dbg_traceback"] = tb
        steps.append(f"  ✗ EXCEPTION: {type(exc).__name__}: {exc}")
        _log.error(f"[extract_bets] Exception:\n{tb}")
        bets    = []
        matches = []

    debug_info["_dbg_steps"] = steps
    debug_info["_dbg_hit_rates"] = [
        {
            "player":   b.get("player", "?"),
            "bet_type": b.get("bet_type", "?"),
            "hit_rate": b.get("hit_rate"),
            "odds":     b.get("linemate_odds"),
            "sample":   b.get("sample"),
        }
        for b in bets
    ]
    return bets, matches, debug_info


# ─── Voetbal form-verrijking ──────────────────────────────────────────────────

def enrich_soccer_matches_form(matches: list) -> list:
    """Haalt thuis/uit form-data op via Football-Data API (optioneel)."""
    if not getattr(soccer, "API_KEY", ""):
        return matches
    enriched = []
    for m in matches:
        m = dict(m)
        if m.get("home_form") and m.get("away_form"):
            enriched.append(m)
            continue
        comp = (m.get("competition") or "").strip() or "EPL"
        for side in ("home", "away"):
            name = m.get(f"{side}_team", "")
            if not m.get(f"{side}_form") and name:
                try:
                    stats = soccer.get_team_stats_for_match(name, comp)
                    if stats.get("form"):
                        m[f"{side}_form"] = stats["form"]
                    if stats.get("avg_goals_for"):
                        m[f"{side}_gf_avg"] = stats["avg_goals_for"]
                    if stats.get("avg_goals_against"):
                        m[f"{side}_ga_avg"] = stats["avg_goals_against"]
                except Exception:
                    pass
        enriched.append(m)
    return enriched


# ─── Flashscore analyse via Claude ───────────────────────────────────────────

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
        model=EXTRACT_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ─── Scenario- en sportdetectie ───────────────────────────────────────────────

def detect_scenario(bets: list, matches: list) -> int:
    """1 = alleen Flashscore | 2 = Flashscore + Linemate | 3 = alleen Linemate"""
    if matches and not bets:
        return 1
    if matches and bets:
        return 2
    return 3


def detect_sports_from_matches(matches: list) -> set:
    """
    Detecteer welke sporten aanwezig zijn op basis van 'competition' OF 'sport' veld.
    Bug 2 fix: checkt nu ook het 'sport' veld (was eerder alleen 'competition').
    """
    sports = set()
    for m in matches:
        # FIX Bug 2: gebruik sport-veld als competition leeg is
        comp = (m.get("competition") or m.get("sport") or "").lower()
        if not comp:
            continue
        if any(k in comp for k in ("nhl", "hockey")):
            sports.add("NHL")
        elif any(k in comp for k in ("nba", "basketball")):
            sports.add("NBA")
        elif any(k in comp for k in ("mlb", "baseball")):
            sports.add("MLB")
        elif any(k in comp for k in ("premier", "la liga", "bundesliga", "serie a", "ligue", "epl")):
            sports.add("SOCCER")
    if not sports:
        sports = {"NHL", "NBA", "MLB"}
    return sports


# ─── Auto-props generatie ─────────────────────────────────────────────────────

def _nhl_auto_props(progress_cb=None, injuries_enabled: bool = True) -> list:
    if not injuries_enabled:
        return []
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
            for line in (4.5, 3.5, 2.5, 1.5):
                if avg_shots > line:
                    props.append({
                        "player": p["name"], "sport": "NHL", "team": team_abbrev,
                        "bet_type": f"Over {line} Shots on Goal",
                        "linemate_odds": _REF_ODDS["shots"],
                        "hit_rate": None, "sample": "auto", "sample_n": games_n,
                    })
                    break
            if avg_goals >= 0.18:
                props.append({
                    "player": p["name"], "sport": "NHL", "team": team_abbrev,
                    "bet_type": "Anytime Goal Scorer",
                    "linemate_odds": _REF_ODDS["anytime"],
                    "hit_rate": None, "sample": "auto", "sample_n": games_n,
                })
    return props


def _nba_auto_props(progress_cb=None) -> list:
    import math
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
                    line = math.floor(avg_pts * 0.82) + 0.5
                    props.append({
                        "player": p["name"], "sport": "NBA", "team": str(tid),
                        "bet_type": f"Over {line:.1f} Points",
                        "linemate_odds": _REF_ODDS["points"],
                        "hit_rate": None, "sample": "auto", "sample_n": games_n,
                    })
                if avg_reb >= 6:
                    line = math.floor(avg_reb * 0.80) + 0.5
                    props.append({
                        "player": p["name"], "sport": "NBA", "team": str(tid),
                        "bet_type": f"Over {line:.1f} Rebounds",
                        "linemate_odds": _REF_ODDS["rebounds"],
                        "hit_rate": None, "sample": "auto", "sample_n": games_n,
                    })
    return props


def _mlb_auto_props(progress_cb=None) -> list:
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
                        "linemate_odds": _REF_ODDS["hits"],
                        "hit_rate": None, "sample": "auto", "sample_n": games_n,
                    })
                if avg_tb >= 1.4:
                    props.append({
                        "player": p["name"], "sport": "MLB", "team": str(tid),
                        "bet_type": "Over 1.5 Total Bases",
                        "linemate_odds": _REF_ODDS["total_bases"],
                        "hit_rate": None, "sample": "auto", "sample_n": games_n,
                    })
    return props


def generate_auto_props(matches: list, progress_cb=None,
                        injuries_enabled: bool = True) -> list:
    """Genereer automatische props (Scenario 1: geen Linemate).

    injuries_enabled=False slaat de NHL-spelersroster scan over (32 API-calls).
    """
    sports = detect_sports_from_matches(matches)
    all_props: list = []
    if "NHL" in sports:
        if progress_cb:
            progress_cb("🏒 NHL schema + spelersstats ophalen...")
        all_props.extend(_nhl_auto_props(progress_cb, injuries_enabled=injuries_enabled))
    if "NBA" in sports:
        if progress_cb:
            progress_cb("🏀 NBA schema + spelersstats ophalen...")
        all_props.extend(_nba_auto_props(progress_cb))
    if "MLB" in sports:
        if progress_cb:
            progress_cb("⚾ MLB schema + spelersstats ophalen...")
        all_props.extend(_mlb_auto_props(progress_cb))
    if "SOCCER" in sports and progress_cb:
        progress_cb("⚽ Voetbal: automatische props niet beschikbaar")
    return all_props


# ─── Team-bet detectie ────────────────────────────────────────────────────────

# Trefwoorden die wijzen op een team-niveau bet (moneyline, spread, totals, enz.)
# Bij deze bet types is spelerdata NIET nodig; enrich_bet() slaat de lookup over.
_TEAM_BET_KEYWORDS: frozenset = frozenset({
    "win",           # "Panthers Win", "To Win"
    "moneyline",     # "Moneyline"
    " ml",           # " ML" (met spatie om "html" te vermijden)
    "to win",
    "game winner",
    "puck line",     # NHL
    "run line",      # MLB
    "spread",        # NBA / MLB
    "handicap",
    "regulation win",
    "overtime win",
    "period",        # "1st Period", "3rd Period Winner"
    "first half",    # Soccer / NBA
    "second half",
    "half time",
    "total goals",   # Soccer
    "game total",
    "over/under",
    "double chance", # Soccer
    "draw",          # Soccer
    "both teams",    # Soccer BTTS
    "clean sheet",   # Soccer
})


def _is_team_bet(player_name: str, bet_type: str,
                 extra_team_keywords: set = None) -> bool:
    """
    Geeft True als dit een team-niveau bet is (geen spelerdata nodig).

    Criteria (één ervan is genoeg):
    - player_name is leeg
    - bet_type bevat een trefwoord uit _TEAM_BET_KEYWORDS
    - player_name komt overeen met een bekend team-trefwoord (optioneel meegegeven)
    """
    if not player_name:
        return True
    bt = bet_type.lower()
    if any(kw in bt for kw in _TEAM_BET_KEYWORDS):
        return True
    if extra_team_keywords and any(kw in player_name.lower() for kw in extra_team_keywords):
        return True
    return False


# ─── Bet verrijken ────────────────────────────────────────────────────────────

def enrich_bet(bet: dict, cache: dict,
               linemate_weight: float = 0.35,
               season_weight:   float = 0.35) -> dict:
    """
    Verrijkt één bet met spelersstatistieken en berekent EV/composite.

    Bug 3 fix: sample_n default is nu 0 (onbekend) in plaats van 5.
    """
    sport       = (bet.get("sport") or "").upper().strip()
    player_name = bet.get("player", "")
    team_hint   = bet.get("team") or ""
    bet_type    = bet.get("bet_type", "")

    # FIX Bug 3: default 0 (onbekend) niet 5 (artificiele data)
    sample_n = bet.get("sample_n") or 0

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
                # Alleen spelerdata ophalen voor echte player props (shots, goals,
                # assists, hits, enz.). Team-niveau bets (Moneyline, Puck Line,
                # Regulation Win, enz.) lopen via analyze_nhl_matches() en hebben
                # géén spelersdata nodig. _all_rosters() laadt 32 teamroosters
                # via losse API-calls en mag hiervoor NIET getriggerd worden.
                if not _is_team_bet(player_name, bet_type, _NHL_TEAM_KEYWORDS):
                    player_id, team = nhl.find_player(player_name)
                    if player_id:
                        player_stats  = nhl.get_player_stats(player_id)
                        opponent_name = nhl.get_opponent(team) if team else None
                        if opponent_name:
                            opponent_stats = nhl.get_team_defense(opponent_name)
            elif sport == "NBA":
                # Alleen spelerdata ophalen voor player props (Points, Rebounds,
                # Assists, enz.). Team moneyline / spread / totals lopen via
                # analyze_nba_matches() met alleen team-form data.
                if not _is_team_bet(player_name, bet_type):
                    player = nba.find_player(player_name)
                    if player:
                        player_stats = nba.get_player_stats(player["id"])
            elif sport == "MLB":
                # Alleen spelerdata ophalen voor player props (Hits, Home Runs,
                # Strikeouts, enz.). Moneyline / Run Line / Game Total lopen via
                # analyze_mlb_matches() met team-form + pitcher data.
                if not _is_team_bet(player_name, bet_type):
                    player = mlb.find_player(player_name)
                    if player:
                        pos_code = player.get("primaryPosition", {}).get("code", "")
                        pos_type = "pitching" if pos_code == "1" else "hitting"
                        player_stats = mlb.get_player_stats(player.get("id"), position_type=pos_type)
            elif sport in SOCCER_COMPS:
                # Alleen spelerdata ophalen voor player props (Goals, Assists,
                # Shots on Target, enz.). Team-bets (1X2, BTTS, Over/Under)
                # lopen via analyze_soccer_matches() met team-form data.
                if not _is_team_bet(player_name, bet_type):
                    comp = sport if sport != "VOETBAL" else "EPL"
                    player = soccer.find_player(player_name, team_hint=team_hint, competition=comp)
                    if player:
                        player_stats   = soccer.get_player_stats(player.get("id"), player.get("team_id"), comp)
                        opponent_stats = soccer.get_team_defense(player.get("team_id")) if player.get("team_id") else {}
        except Exception as exc:
            _log.warning(f"[enrich_bet] Datafout voor {player_name}: {exc}")

        cache[cache_key] = {
            "player_stats":   player_stats,
            "opponent":       opponent_name,
            "opponent_stats": opponent_stats,
        }

    _raw_hr   = bet.get("hit_rate")
    _hr_ok    = _raw_hr is not None
    _lm_hr    = float(_raw_hr) if _hr_ok else 0.0
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
        "sample_n":       sample_n,
        "games_sampled":  score.get("games_sampled", 0),
        "linemate_hr":    score["linemate_hr"],
        "season_hr":      score["season_hr"],
        "composite":      score["composite"],
        "ev":             ev_score,
        "rating":         rat,
        "opponent":       opponent_name or bet.get("opponent"),
        "gaa":            opponent_stats.get("goals_against_avg"),
        "source":         player_stats.get("source", ""),
        "bet365":         {},
        "no_linemate_hr": not _hr_ok,
        # Extra context uit de Linemate Trends-weergave (meerdere statistiekregels per prop)
        "trend_stats":    bet.get("trend_stats", []),
    }


# ─── Filtering en ranking ──────────────────────────────────────────────────────

def filter_and_rank_props(enriched: list) -> list:
    """
    Filter en rank props:
      - unavailable / negatieve EV / sample_n < 3  → uitsluiten
      - different_line → −15% EV penalty
      - sample_n < 5   → −40% EV penalty
      - sortering via gewogen_ev = ev × min(sample_n, 20) / 20

    Bug 1 fix: rating wordt herberekend nadat penalties zijn toegepast.
    """
    result = []
    for bet in enriched:
        bet         = dict(bet)
        b365_status = (bet.get("bet365") or {}).get("status", "")
        ev_val      = float(bet.get("ev") or -999)
        sample_n    = int(bet.get("sample_n") or 0)

        # Harde uitsluitingen
        if b365_status == "unavailable":
            continue
        if ev_val <= 0:
            continue
        if sample_n > 0 and sample_n < 3:
            continue

        # Penalties
        if b365_status == "different_line":
            ev_val         = ev_val * 0.85
            bet["ev"]      = ev_val
            bet["_ev_penalty_note"] = "⚠️ Andere lijn op Bet365 (−15% EV penalty)"

        if sample_n > 0 and sample_n < 5:
            ev_val         = ev_val * 0.60
            bet["ev"]      = ev_val
            bet["_sample_warning"] = f"⚠️ Klein sample ({sample_n} wedstrijden)"

        # FIX Bug 1: herbereken rating na penalties op ev_val
        bet["rating"] = rating(ev_val, bet.get("composite", 0.0))

        # Gewogen EV voor sortering
        eff_n = sample_n if sample_n > 0 else 20
        bet["_weighted_ev"] = ev_val * min(eff_n, 20) / 20

        result.append(bet)

    result.sort(key=lambda b: float(b.get("_weighted_ev") or 0), reverse=True)
    return result


# ─── Parlay suggesties ────────────────────────────────────────────────────────

def generate_parlay_suggestions(bets: list, max_parlays: int = 3) -> list:
    """Genereer top parlay combinaties uit beschikbare, positieve-EV props."""
    eligible = [
        b for b in bets
        if ((b.get("bet365") or {}).get("status", "") in ("available", "different_line", "")
            and float(b.get("ev") or -1) > 0)
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
                "props":               list(combo),
                "gecombineerde_odds":  round(comb_odds, 3),
                "hit_kans":            round(hit_ch, 4),
                "parlay_ev":           round(p_ev, 4),
            })
    candidates.sort(key=lambda x: x["parlay_ev"], reverse=True)
    return candidates[:max_parlays]
