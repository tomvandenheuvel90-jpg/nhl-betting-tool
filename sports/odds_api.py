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
import datetime
import unicodedata
import urllib.request
import urllib.parse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set

# Teller-bestand in de repo-root (BetAnalyzer/)
USAGE_FILE = Path(__file__).parent.parent / "odds_api_usage.json"

# ─── Configuratie ─────────────────────────────────────────────────────────────

_API_KEY: str = ""

def set_api_key(key: str) -> None:
    global _API_KEY
    _API_KEY = key or ""


BASE_URL = "https://api.the-odds-api.com/v4"


# ─── Request teller ───────────────────────────────────────────────────────────

def _get_usage() -> dict:
    """Lees of initialiseer de maandelijkse teller. Reset automatisch bij nieuwe maand."""
    maand = datetime.date.today().strftime("%Y-%m")
    default = {
        "maand":          maand,
        "calls":          0,
        "limiet":         500,
        "laatste_reset":  f"{maand}-01",
    }
    if not USAGE_FILE.exists():
        return default
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if data.get("maand") != maand:
            # Nieuwe maand → reset teller
            data = default
        return data
    except Exception:
        return default


def _save_usage(data: dict) -> None:
    try:
        USAGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # read-only filesystem (bijv. sommige cloud omgevingen)


def _increment_counter() -> None:
    """Verhoog de teller na elke echte API call (geen cache hits)."""
    data = _get_usage()
    data["calls"] = data.get("calls", 0) + 1
    _save_usage(data)


def get_usage() -> dict:
    """Publieke functie: geeft huidig gebruik terug."""
    return _get_usage()


def is_limit_reached() -> bool:
    """Geeft True als de maandlimiet bereikt is."""
    data = _get_usage()
    return data.get("calls", 0) >= data.get("limiet", 500)

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

def _get(url: str):
    """GET-request naar The Odds API. Geeft None bij fout. Verhoogt teller bij succes."""
    if not _API_KEY:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 BetAnalyzer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        _increment_counter()   # alleen bij succesvol HTTP-verzoek
        return data
    except Exception as e:
        print(f"  ⚠️  Odds API fout: {e}")
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
    Cache key is per event_id (ongeacht market combinatie) → TTL 2 uur.

    Gebruik prefetch_event_props_for_bets() om meerdere markets per event
    in één API call op te halen vóór de per-bet loop.

    markets: lijst van market keys bijv. ["player_shots_on_target", "player_anytime_goalscorer"]
    """
    # Cache per event (niet per market combo) — prefetch vult alle benodigde markets tegelijk
    cache_key = f"odds_props_{event_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    market_str = ",".join(sorted(set(markets)))
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

    cache_set(cache_key, data, ttl_hours=2)
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

def _find_team_event(events: list, team: str, sport: str):
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

def _market_for_bet(bet_type: str):
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


# ─── 1X2 / three-way wedstrijd odds ─────────────────────────────────────────

def get_match_odds_h2h(sport: str, home_team: str, away_team: str) -> dict:
    """
    Haalt 1X2 (drie-weg: thuiswinst / gelijkspel-OT / uitwinst) odds op
    van Bet365 voor een NHL-wedstrijd.

    sport:      "NHL"
    home_team:  volledige naam of afkorting ("Florida Panthers" of "FLA")
    away_team:  idem

    Returns dict:
      home_odds:  float | None   — odds thuiswinst
      draw_odds:  float | None   — odds gelijkspel / OT/SO
      away_odds:  float | None   — odds uitwinst
      source:     "bet365" | "not_found" | "no_api_key"
    """
    empty = {"home_odds": None, "draw_odds": None, "away_odds": None, "source": "not_found"}

    if not _API_KEY:
        return {**empty, "source": "no_api_key"}

    sport_key = SPORT_KEYS.get(sport.upper())
    if not sport_key:
        return empty

    # Cache per wedstrijdpaar (6 uur)
    ck = f"h2h_{sport_key}_{_normalize_name(home_team)}_{_normalize_name(away_team)}"
    cached = cache_get(ck)
    if cached is not None:
        return cached

    # Zoek event in vandaag's schema
    events = get_events(sport_key)
    event  = _find_match_event(events, home_team, away_team, sport)
    if not event:
        return empty

    # Haal h2h odds op (aparte call om prop-cache niet te vervuilen)
    h2h_ck = f"h2h_event_{event['id']}"
    data = cache_get(h2h_ck)
    if data is None:
        params = urllib.parse.urlencode({
            "apiKey":     _API_KEY,
            "bookmakers": "bet365",
            "markets":    "h2h",
            "oddsFormat": "decimal",
        })
        url  = f"{BASE_URL}/sports/{sport_key}/events/{event['id']}/odds?{params}"
        data = _get(url)
        if data:
            cache_set(h2h_ck, data, ttl_hours=4)

    if not data:
        return empty

    bookmakers = data.get("bookmakers", [])
    bet365     = next((b for b in bookmakers if b.get("key") == "bet365"), None)
    if not bet365:
        return empty

    h2h_mkt = next(
        (m for m in bet365.get("markets", []) if m.get("key") == "h2h"),
        None,
    )
    if not h2h_mkt:
        return empty

    # Identificeer home / draw / away uit outcomes
    # De Odds API geeft voor hockey drie outcomes: Home, Draw, Away
    ev_home = _normalize_name(event.get("home_team", ""))
    ev_away = _normalize_name(event.get("away_team", ""))

    # Bouw zoeksets voor beide teams (afkortingen + varianten)
    ht_vars = _team_name_variants(home_team)
    at_vars = _team_name_variants(away_team)

    home_odds = draw_odds = away_odds = None

    for outcome in h2h_mkt.get("outcomes", []):
        nm  = _normalize_name(outcome.get("name", ""))
        prc = float(outcome.get("price", 0) or 0)
        if nm == "draw":
            draw_odds = prc
        elif any(v in nm or nm in v for v in ht_vars) or nm in ev_home or ev_home in nm:
            home_odds = prc
        elif any(v in nm or nm in v for v in at_vars) or nm in ev_away or ev_away in nm:
            away_odds = prc

    result = {
        "home_odds": home_odds,
        "draw_odds": draw_odds,
        "away_odds": away_odds,
        "source":    "bet365" if any([home_odds, draw_odds, away_odds]) else "not_found",
    }
    cache_set(ck, result, ttl_hours=4)
    return result


def _team_name_variants(name: str) -> set:
    """Geeft een set van genormaliseerde zoekstrings voor een teamnaam of afkorting."""
    variants = {_normalize_name(name)}
    name_up  = name.strip().upper()
    for names in _NHL_TEAM_NAMES.get(name_up, []):
        variants.add(_normalize_name(names))
    # Omgekeerd: als de input een volledige naam is, zoek dan de afkorting
    name_low = _normalize_name(name)
    for abbrev, names in _NHL_TEAM_NAMES.items():
        if any(_normalize_name(n) == name_low or name_low in _normalize_name(n) for n in names):
            variants.update(_normalize_name(n) for n in names)
            variants.add(abbrev.lower())
    return variants


def _find_match_event(events: list, home_team: str, away_team: str, sport: str):
    """Zoek event op in events lijst waarbij BEIDE teams overeenkomen."""
    if not events:
        return None
    ht_vars = _team_name_variants(home_team)
    at_vars = _team_name_variants(away_team)
    for event in events:
        ev_home = _normalize_name(event.get("home_team", ""))
        ev_away = _normalize_name(event.get("away_team", ""))
        ht_ok = any(v in ev_home or ev_home in v for v in ht_vars)
        at_ok = any(v in ev_away or ev_away in v for v in at_vars)
        if ht_ok and at_ok:
            return event
    return None


# ─── Batch prefetch (Optimalisatie 3) ────────────────────────────────────────

def prefetch_event_props_for_bets(bets: list) -> None:
    """
    Pre-fetcht bet365 props voor alle unieke events in één ronde.
    Groepeer alle benodigde markets per event en haal ze in één API call op.
    Resultaten worden gecacht zodat check_bet365_availability() geen nieuwe
    calls meer hoeft te maken.

    bets: lijst van enriched bets (moeten 'sport', 'team', 'bet_type' hebben)
    """
    if not _API_KEY:
        return

    # Groepeer per (sport_key, event_id) → set van markets
    event_markets: dict = {}
    event_id_map:  dict = {}  # (sport_key, event_id) → sport_key (voor API call)

    for bet in bets:
        sport     = (bet.get("sport") or "").upper()
        sport_key = SPORT_KEYS.get(sport)
        if not sport_key:
            continue
        market = _market_for_bet(bet.get("bet_type", ""))
        if not market:
            continue
        team = bet.get("team", "")

        # get_events is gecacht — geen extra API call
        events = get_events(sport_key)
        event  = _find_team_event(events, team, sport)
        if not event:
            continue

        key = (sport_key, event["id"])
        event_markets.setdefault(key, set()).add(market)
        event_id_map[key] = sport_key

    # Eén API call per uniek event (met alle benodigde markets tegelijk)
    for (sport_key, event_id), markets in event_markets.items():
        cache_key = f"odds_props_{event_id}"
        if cache_get(cache_key) is None:
            get_event_props(sport_key, event_id, list(markets))
