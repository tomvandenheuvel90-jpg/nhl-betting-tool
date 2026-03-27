"""
injuries.py — Controleer beschikbaarheidsstatus van spelers.
Ondersteunt NHL, NBA en MLB.
Checkt alleen voor props met EV > 0.3 om API-quota te sparen.
"""
import logging
import time
import requests
from typing import Optional

_logger = logging.getLogger(__name__)
_CACHE: dict = {}
_CACHE_TTL = 3600  # 1 uur cache

_NHL_TEAM_CODES = {
    "Boston Bruins": "BOS", "Buffalo Sabres": "BUF", "Detroit Red Wings": "DET",
    "Florida Panthers": "FLA", "Montreal Canadiens": "MTL", "Ottawa Senators": "OTT",
    "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR",
    "Carolina Hurricanes": "CAR", "Columbus Blue Jackets": "CBJ",
    "New Jersey Devils": "NJD", "New York Islanders": "NYI",
    "New York Rangers": "NYR", "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT", "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Dallas Stars": "DAL",
    "Minnesota Wild": "MIN", "Nashville Predators": "NSH",
    "St. Louis Blues": "STL", "Anaheim Ducks": "ANA",
    "Calgary Flames": "CGY", "Edmonton Oilers": "EDM",
    "Los Angeles Kings": "LAK", "San Jose Sharks": "SJS",
    "Seattle Kraken": "SEA", "Vancouver Canucks": "VAN",
    "Vegas Golden Knights": "VGK", "Utah Hockey Club": "UTA",
}


def _cached_get(url: str, headers: Optional[dict] = None, timeout: int = 8) -> Optional[dict]:
    now = time.time()
    if url in _CACHE and now - _CACHE[url]["ts"] < _CACHE_TTL:
        return _CACHE[url]["data"]
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        _CACHE[url] = {"ts": now, "data": data}
        return data
    except Exception as e:
        _logger.debug(f"injuries._cached_get fout {url}: {e}")
        return None


def _nhl_check(player_name: str, team_hint: str = "") -> str:
    """Geeft 'fit', 'questionable', 'injured' of 'unknown'."""
    team_code = None
    hint_upper = team_hint.upper()
    for full, code in _NHL_TEAM_CODES.items():
        if hint_upper in (full.upper(), code.upper(), full.upper().split()[-1]):
            team_code = code
            break
    if not team_code:
        return "unknown"
    data = _cached_get(f"https://api-web.nhle.com/v1/roster/{team_code}/current")
    if not data:
        return "unknown"
    name_lower = player_name.lower().strip()
    for group in ("forwards", "defensemen", "goalies"):
        for p in data.get(group, []):
            fn = p.get("firstName", {}).get("default", "")
            ln = p.get("lastName", {}).get("default", "")
            full = f"{fn} {ln}".lower()
            if name_lower == full or all(part in full for part in name_lower.split() if len(part) > 2):
                return "fit"
    return "unknown"


def _nba_check(player_name: str) -> str:
    """Controleer NBA injury via ESPN."""
    data = _cached_get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries")
    if not data:
        return "unknown"
    name_lower = player_name.lower()
    try:
        for team_obj in data.get("injuries", []):
            for inj in team_obj.get("injuries", []):
                p_name = inj.get("athlete", {}).get("displayName", "").lower()
                if name_lower in p_name or all(p in p_name for p in name_lower.split() if len(p) > 2):
                    status = inj.get("status", "").lower()
                    if any(w in status for w in ("out", "injured reserve")):
                        return "injured"
                    if any(w in status for w in ("questionable", "doubtful", "day-to-day")):
                        return "questionable"
    except Exception as e:
        _logger.debug(f"_nba_check fout: {e}")
    return "fit"


def _mlb_check(player_name: str) -> str:
    """Controleer MLB injury via statsapi."""
    data = _cached_get("https://statsapi.mlb.com/api/v1/injuries")
    if not data:
        return "unknown"
    name_lower = player_name.lower()
    try:
        for item in data.get("roster", []):
            p_name = item.get("person", {}).get("fullName", "").lower()
            if name_lower in p_name or all(p in p_name for p in name_lower.split() if len(p) > 2):
                note = item.get("note", "").lower()
                if any(w in note for w in ("10-day", "60-day", "il ", "injured list")):
                    return "injured"
                if "day-to-day" in note:
                    return "questionable"
    except Exception as e:
        _logger.debug(f"_mlb_check fout: {e}")
    return "fit"


def check_player_status(player_name: str, sport: str,
                        team_hint: str = "", ev: float = 0.0) -> str:
    """
    Publieke interface. Geeft 'fit', 'questionable', 'injured' of 'unknown'.
    Slaat check over als ev <= 0.3 (quota sparen).
    """
    if ev <= 0.3:
        return "unknown"
    sport_u = sport.upper()
    try:
        if sport_u == "NHL":
            return _nhl_check(player_name, team_hint)
        if sport_u == "NBA":
            return _nba_check(player_name)
        if sport_u == "MLB":
            return _mlb_check(player_name)
    except Exception as e:
        _logger.warning(f"check_player_status fout ({sport}, {player_name}): {e}")
    return "unknown"


def enrich_with_injury_status(bets: list) -> list:
    """Voeg injury_status toe aan elke bet. Retourneert dezelfde lijst."""
    for bet in bets:
        ev    = float(bet.get("ev") or 0)
        sport = bet.get("sport", "")
        name  = bet.get("player", "")
        team  = bet.get("team", "")
        bet["injury_status"] = check_player_status(name, sport, team, ev)
    return bets
