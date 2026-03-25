"""
Voetbal module — football-data.org API v4 (gratis tier).
Vereist API key: registreer gratis op football-data.org → export FOOTBALL_DATA_API_KEY=...

Ondersteunde competities: EPL, La Liga, Bundesliga, Serie A, Ligue 1
Ondersteunde bet types: anytime goalscorer (vrije tier geeft goal-events per wedstrijd)

Shots-on-target per speler zijn NIET beschikbaar in de gratis tier.
"""

import urllib.request
import urllib.parse
import json
import os
import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set
from rate_limiter import soccer_limiter

BASE    = "https://api.football-data.org/v4"
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")

# Linemate-naam → football-data.org competitie code
COMPETITION_MAP = {
    "epl":             "PL",
    "premier league":  "PL",
    "la liga":         "PD",
    "laliga":          "PD",
    "bundesliga":      "BL1",
    "serie a":         "SA",
    "ligue 1":         "FL1",
    "ligue1":          "FL1",
}


# ─── HTTP helper ──────────────────────────────────────────────────────────────

def _get(path: str) -> dict:
    if not API_KEY:
        return {}
    soccer_limiter.wait()
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": API_KEY,
        "User-Agent": "Mozilla/5.0 BetAnalyzer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  ⚠️  football-data.org HTTP {e.code}: {path}")
        return {}
    except Exception as e:
        print(f"  ⚠️  football-data.org fout: {e}")
        return {}


# ─── Team opzoeken ────────────────────────────────────────────────────────────

def _get_teams(competition_code: str) -> list:
    cache_key = f"soccer_teams_{competition_code}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    data   = _get(f"/competitions/{competition_code}/teams")
    teams  = data.get("teams", [])
    cache_set(cache_key, teams, ttl_hours=24)
    return teams


def find_team_by_name(team_hint: str, competition: str):
    """Zoek team op (deel van) naam in gegeven competitie."""
    comp_code = COMPETITION_MAP.get(competition.lower(), competition.upper())
    teams = _get_teams(comp_code)
    hint  = team_hint.lower()
    for t in teams:
        if (
            hint in t.get("name", "").lower()
            or hint in t.get("shortName", "").lower()
            or hint in t.get("tla", "").lower()
        ):
            return t
    return None


# ─── Speler opzoeken ──────────────────────────────────────────────────────────

def find_player(name: str, team_hint: str = "", competition: str = "PL"):  # -> dict | None
    """
    Zoek voetbalspeler via het squad-endpoint van hun team.
    Geeft player dict (met 'id', 'name', 'team_id', 'competition') of None.
    """
    if not API_KEY:
        print("  ⚠️  FOOTBALL_DATA_API_KEY niet ingesteld → voetbal uitgeschakeld")
        return None

    comp_code = COMPETITION_MAP.get(competition.lower(), competition.upper())
    cache_key = f"soccer_player_{name.lower().replace(' ', '_')}_{comp_code}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    teams = _get_teams(comp_code)
    name_lower = name.strip().lower()
    parts      = name_lower.split()

    for team in teams:
        # Skip teams die niet overeenkomen met de hint (indien opgegeven)
        if team_hint:
            hint = team_hint.lower()
            team_name = (team.get("name", "") + " " + team.get("shortName", "") + " " + team.get("tla", "")).lower()
            if hint not in team_name:
                continue

        squad_data = _get(f"/teams/{team['id']}")
        squad      = squad_data.get("squad", [])

        for player in squad:
            pname = player.get("name", "").lower()
            # Exacte match
            if pname == name_lower:
                result = {**player, "team_id": team["id"], "team_name": team.get("name", ""), "competition": comp_code}
                cache_set(cache_key, result, ttl_hours=24)
                return result
            # Achternaam match
            if parts and parts[-1] == pname.split()[-1]:
                result = {**player, "team_id": team["id"], "team_name": team.get("name", ""), "competition": comp_code}
                cache_set(cache_key, result, ttl_hours=24)
                return result

    return None


# ─── Spelerstats — goal scoring per wedstrijd ─────────────────────────────────

def get_player_stats(player_id: int, team_id: int, competition: str = "PL", n_games: int = 20) -> dict:
    """
    Haal recente wedstrijden op voor het team en check of de speler scoorde.
    Gratis tier: goal-events per wedstrijd zijn beschikbaar via /matches/{id}.

    Geeft:
      - raw_goals: [0 of 1 per wedstrijd]
      - games_sampled
    """
    if not API_KEY:
        return {"games_sampled": 0, "source": "football-data.org (geen API key)"}

    cache_key = f"soccer_stats_{player_id}_{team_id}_{competition}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    # Haal de laatste wedstrijden op voor het team
    season = _current_season()
    data   = _get(
        f"/teams/{team_id}/matches"
        f"?competitions={competition}&season={season}&status=FINISHED"
    )
    matches = data.get("matches", [])

    # Nieuwste eerst
    matches = sorted(matches, key=lambda m: m.get("utcDate", ""), reverse=True)[:n_games]

    goals_per_game = []

    for match in matches:
        match_id = match.get("id")
        if not match_id:
            continue

        # Check of score aanwezig is (soms geen goal-events in basis-response)
        # Haal wedstrijd detail op voor goal-events
        detail = _get(f"/matches/{match_id}")
        goals  = detail.get("goals", [])

        scored = sum(
            1 for g in goals
            if g.get("scorer", {}).get("id") == player_id
        )
        goals_per_game.append(float(scored))

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    n = len(goals_per_game)
    result = {
        "games_sampled": n,
        "source": f"football-data.org — {competition} {season}",

        "raw_goals":   goals_per_game,

        "avg_goals":   avg(goals_per_game),
    }

    cache_set(cache_key, result, ttl_hours=6)
    return result


def get_team_defense(team_id: int) -> dict:
    """Haal basisdefensieve stats op (goals against)."""
    cache_key = f"soccer_team_defense_{team_id}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    data  = _get(f"/teams/{team_id}/matches?status=FINISHED&limit=10")
    matches = data.get("matches", [])

    goals_against = []
    for m in matches[-10:]:
        home_team = m.get("homeTeam", {}).get("id")
        score     = m.get("score", {}).get("fullTime", {})
        if home_team == team_id:
            goals_against.append(float(score.get("away", 0) or 0))
        else:
            goals_against.append(float(score.get("home", 0) or 0))

    avg_ga = round(sum(goals_against) / len(goals_against), 2) if goals_against else 1.3
    result = {"goals_against_avg": avg_ga}
    cache_set(cache_key, result, ttl_hours=6)
    return result


def _current_season() -> int:
    today = datetime.date.today()
    # Voetbalseizoen aug–mei: voor augustus gebruiken we vorig jaar
    return today.year - 1 if today.month < 8 else today.year
