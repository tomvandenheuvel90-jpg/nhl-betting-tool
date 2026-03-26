"""
The Odds API — Bet365 player props verificatie.

Gratis tier: 500 requests/maand.
Haalt per wedstrijd bet365 player prop odds op en vergelijkt met onze bets.

Status:
  "available"       — Bet365 heeft dezelfde lijn, gebruik die odds voor EV
  "different_line"  — Bet365 heeft ander drempelwaarde (⚠️ penalty)
  "unavailable"     — Bet365 heeft deze prop niet (❌ uitsluiten van top 5)
  "unknown"         — Geen API key of API fout (neutraal)
"""

import json
import unicodedata
import urllib.request
import urllib.parse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set

# ─── Configuratie ─────────────────────────────────────────────────────────────

_API_KEY: str = ""

def set_api_key(key: str) -> None:
    global _API_KEY
    _API_KEY = key or ""


BASE_URL = "https://api.the-odds-api.com/v4"

# Sport keys voor The Odds API
SPORT_KEYS = {
    "NHL":     "icehockey_nhl",
    "NBA":     "basketball_nba",
    "MLB":     "baseball_mlb",
    "SOCCER":  "soccer_epl",          # default; overschrijven indien nodig
    "EPL":     "soccer_epl",
    "LALIGA":  "soccer_spain_la_liga",
    "BUNDESLIGA": "soccer_germany_bundesliga",
    "SERIEA":  "soccer_italy_serie_a",
    "LIGUE1":  "soccer_france_ligue_one",
    "UCL":     "soccer_uefa_champs_league",
}

# Bet type keywords → The Odds API market keys
BET_MARKETS = {
    "shot":            "player_shots_on_target",   # NHL shots on goal
    "sog":             "player_shots_on_target",
    "anytime":         "player_anytime_goalscorer",
    "goal":            "player_anytime_goalscorer",
    "scorer":          "player_anytime_goalscorer",
    "assist":          "player_assists",
    "point":           "player_points",            # NBA / NHL points
    "hit":             "player_hits",              # MLB hits
    "home run":        "player_home_runs",
    "rbi":             "player_rbis",
    "total base":      "player_total_bases",
    "strikeout":       "player_strikeouts",
    "rebound":         "player_rebounds",
    "three":           "player_threes",
    "3pt":             "player_threes",
    "steal":           "player_steals",
    "block":           "player_blocks",
    "pts":             "player_points",
}

# NHL team naam → mogelijke varianten in The Odds API
_NHL_TEAM_NAMES = {
    "ANA": ["Anaheim Ducks", "Ducks"],
    "ARI": ["Arizona Coyotes", "Coyotes"],
    "BOS": ["Boston Bruins", "Bruins"],
    "BUF": ["Buffalo Sabres", "Sabres"],
    "CGY": ["Calgary Flames", "Flames"],
    "CAR": ["Carolina Hurricanes", "Hurricanes"],
    "CHI": ["Chicago Blackhawks", "Blackhawks"],
    "COL": ["Colorado Avalanche", "Avalanche"],
    "CBJ": ["Columbus Blue Jackets", "Blue Jackets"],
    "DAL": ["Dallas Stars", "Stars"],
    "DET": ["Detroit Red Wings", "Red Wings"],
    "EDM": ["Edmonton Oilers", "Oilers"],
    "FLA": ["Florida Panthers", "Panthers"],
    "LAK": ["Los Angeles Kings", "Kings"],
    "MIN": ["Minnesota Wild", "Wild"],
    "MTL": ["Montreal Canadiens", "Canadiens"],
    "NSH": ["Nashville Predators", "Predators"],
    "NJD": ["New Jersey Devils", "Devils"],
    "NYI": ["New York Islanders", "Islanders"],
    "NYR": ["New York Rangers", "Rangers"],
    "OTT": ["Ottawa Senators", "Senators"],
    "PHI": ["Philadelphia Flyers", "Flyers"],
    "PIT": ["Pittsburgh Penguins", "Penguins"],
    "STL": ["St. Louis Blues", "Blues"],
    "SJS": ["San Jose Sharks", "Sharks"],
    "SEA": ["Seattle Kraken", "Kraken"],
    "TBL": ["Tampa Bay Lightning", "Lightning"],
    "TOR": ["Toronto Maple Leafs", "Maple Leafs"],
    "VAN": ["Vancouver Canucks", "Canucks"],
    "VGK": ["Vegas Golden Knights", "Golden Knights"],
    "WSH": ["Washington Capitals", "Capitals"],
    "WPG": ["Winnipeg Jets", "Jets"],
    "UTA": ["Utah Hockey Club", "Utah"],
}


# ─── HTTP helper ──────────────────────────────────────────────────────────────

def _get(url: str) -> dict | list | None:
    """GET-request naar The Odds API. Geeft None bij fout."""
    if not _API_KEY:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 BetAnalyzer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  Odds API fout: {e}")
        return None


def get_requests_remaining() -> int | None:
    """Geeft het aantal resterende requests voor deze maand, of None."""
    if not _API_KEY:
        return None
    url = f"{BASE_URL}/sports?apiKey={_API_KEY}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BetAnalyzer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            remaining = r.headers.get("x-requests-remaining")
            return int(remaining) if remaining else None
    except Exception:
        return None


# ─── Events ophalen ───────────────────────────────────────────────────────────

def get_events(sport_key: str) -> list:
    """Haalt vandaag's events op voor een sport. Gecacht 6 uur."""
    cache_key = f"odds_events_{sport_key}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{BASE_URL}/sports/{sport_key}/events?apiKey={_API_KEY}"
    data = _get(url)
    if not isinstance(data, list):
        return []

    cache_set(cache_key, data, ttl_hours=6)
    return data


# ─── Player props ophalen ─────────────────────────────────────────────────────

def get_event_props(sport_key: str, event_id: str, markets: list) -> dict:
    """
    Haalt player props op voor één event via bet365.
    Gecacht 4 uur (odds veranderen niet snel).

    markets: lijst van market keys bijv. ["player_shots_on_target", "player_anytime_goalscorer"]
    """
    market_str = ",".join(sorted(set(markets)))
    cache_key  = f"odds_props_{event_id}_{market_str}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({
        "apiKey":      _API_KEY,
        "bookmakers":  "bet365",
        "markets":     market_str,
        "oddsFormat":  "decimal",
    })
    url = f"{BASE_URL}/sports/{sport_key}/events/{event_id}/odds?{params}"
    data = _get(url)

    if not isinstance(data, dict):
        return {}

    cache_set(cache_key, data, ttl_hours=4)
    return data


# ─── Naam normalisatie ────────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Lowercase, strip accenten, verwijder leestekens behalve spaties."""
    name = name.strip().lower()
    # Verwijder accenten (é → e, ü → u, etc.)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    # Verwijder punten (initialen: "j. smith" → "j smith")
    name = name.replace(".", "")
    return name.strip()


def _names_match(a: str, b: str) -> bool:
    """
    Fuzzy naam vergelijking:
      - Exacte match na normalisatie
      - Initiaal + achternaam: "B. Rust" matcht "Bryan Rust"
      - Alleen achternaam (uniek in context)
    """
    na = _normalize_name(a)
    nb = _normalize_name(b)

    if na == nb:
        return True

    # Initiaal-match: één kant heeft "b rust", andere "bryan rust"
    for shorter, longer in ((na, nb), (nb, na)):
        parts_s = shorter.split()
        parts_l = longer.split()
        if (
            len(parts_s) >= 2
            and len(parts_l) >= 2
            and len(parts_s[0]) == 1          # initiaal
            and parts_s[-1] == parts_l[-1]    # zelfde achternaam
            and parts_l[0].startswith(parts_s[0])  # voornaam begint met initiaal
        ):
            return True

    return False


# ─── Event matching ───────────────────────────────────────────────────────────

def _find_team_event(events: list, team: str, sport: str) -> dict | None:
    """
    Zoek het event op in de events-lijst dat overeenkomt met het team.
    team kan een afkorting zijn (NHL) of een teamnaam.
    """
    if not team or not events:
        return None

    team_lower = team.strip().lower()

    # Bouw zoeklijst: voor NHL ook volledige teamnamen
    search_names = {team_lower}
    if sport == "NHL":
        team_upper = team.strip().upper()
        for variant in _NHL_TEAM_NAMES.get(team_upper, []):
            search_names.add(variant.lower())

    for event in events:
        home = event.get("home_team", "").lower()
        away = event.get("away_team", "").lower()
        for search in search_names:
            if search in home or search in away:
                return event

    return None


# ─── Markt bepaling ───────────────────────────────────────────────────────────

def _market_for_bet(bet_type: str) -> str | None:
    """Geeft de The Odds API market key voor een bet type, of None."""
    bt = bet_type.lower()
    for keyword, market in BET_MARKETS.items():
        if keyword in bt:
            return market
    return None


# ─── Hoofdfunctie: verificatie ────────────────────────────────────────────────

def check_bet365_availability(
    player_name: str,
    bet_type:    str,
    sport:       str,
    team:        str = "",
) -> dict:
    """
    Controleert of bet365 een player prop heeft voor dit bet.

    Returns dict:
      status:       "available" | "different_line" | "unavailable" | "unknown"
      bet365_odds:  float | None
      bet365_line:  float | None
      our_line:     float | None
      label:        str  (✅ / ⚠️ / ❌ / "")
    """
    empty = {
        "status":      "unknown",
        "bet365_odds": None,
        "bet365_line": None,
        "our_line":    None,
        "label":       "",
    }

    if not _API_KEY:
        return empty

    sport_key = SPORT_KEYS.get(sport.upper())
    if not sport_key:
        return empty

    market = _market_for_bet(bet_type)
    if not market:
        return empty

    # Our line
    import re
    m = re.search(r"over\s+([\d.]+)", bet_type.lower())
    our_line = float(m.group(1)) if m else None

    # Haal events op
    events = get_events(sport_key)
    event  = _find_team_event(events, team, sport)
    if not event:
        # Event niet gevonden → onbekend (niet noodzakelijk afwezig op bet365)
        return {**empty, "status": "unknown", "label": ""}

    # Haal props op
    props_data = get_event_props(sport_key, event["id"], [market])
    if not props_data:
        return {**empty, "status": "unavailable", "label": "❌ Niet op Bet365"}

    # Zoek bet365 bookmaker
    bookmakers = props_data.get("bookmakers", [])
    bet365 = next(
        (b for b in bookmakers if b.get("key") == "bet365"),
        None,
    )
    if not bet365:
        return {**empty, "status": "unavailable", "label": "❌ Niet op Bet365"}

    # Zoek de juiste market
    markets_found = bet365.get("markets", [])
    market_data = next(
        (mk for mk in markets_found if mk.get("key") == market),
        None,
    )
    if not market_data:
        return {**empty, "status": "unavailable", "label": "❌ Niet op Bet365"}

    # Zoek de speler in de outcomes
    outcomes = market_data.get("outcomes", [])
    player_outcome = None
    for outcome in outcomes:
        if _names_match(outcome.get("description", "") or outcome.get("name", ""), player_name):
            # The Odds API: Over outcomes hebben "Over" als naam
            if outcome.get("name", "").lower() == "over" or "over" in bet_type.lower():
                player_outcome = outcome
                break

    if not player_outcome:
        return {**empty, "status": "unavailable", "label": "❌ Niet op Bet365"}

    bet365_odds = float(player_outcome.get("price", 0) or 0)
    bet365_line = float(player_outcome.get("point", 0) or 0)

    if bet365_line == 0 and our_line is not None:
        # Sommige markets hebben geen point (bijv. anytime scorer)
        bet365_line = our_line

    # Vergelijk lijnen
    if our_line is None or abs(bet365_line - our_line) < 0.26:
        # Zelfde (of geen) lijn
        return {
            "status":      "available",
            "bet365_odds": round(bet365_odds, 2),
            "bet365_line": round(bet365_line, 2),
            "our_line":    our_line,
            "label":       f"✅ Bet365 @{bet365_odds:.2f}",
        }
    else:
        return {
            "status":      "different_line",
            "bet365_odds": round(bet365_odds, 2),
            "bet365_line": round(bet365_line, 2),
            "our_line":    our_line,
            "label":       f"⚠️ Bet365 line {bet365_line} (wij: {our_line})",
        }
