"""
MLB module — MLB Stats API (statsapi.mlb.com) voor per-game stats.
Volledig gratis, geen API key nodig.

Ondersteunde bet types:
  hits, total bases, runs, RBI, home runs (batters)
  strikeouts, innings pitched (pitchers)
"""

import urllib.request
import json
import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set
from rate_limiter import mlb_limiter

BASE    = "https://statsapi.mlb.com/api/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 BetAnalyzer/1.0"}


# ─── HTTP helper ──────────────────────────────────────────────────────────────

def _get(url: str) -> dict:
    mlb_limiter.wait()
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  MLB API fout: {e}")
        return {}


# ─── Speler opzoeken ──────────────────────────────────────────────────────────

def find_player(name: str):
    """Zoek MLB speler op naam. Geeft player dict of None."""
    import urllib.parse
    cache_key = f"mlb_player_{name.lower().replace(' ', '_')}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    data    = _get(f"{BASE}/people/search?names={urllib.parse.quote(name)}&sportId=1")
    people  = data.get("people", [])

    if not people:
        return None

    # Probeer exacte match
    name_lower = name.strip().lower()
    for p in people:
        fn = p.get("firstName", "")
        ln = p.get("lastName", "")
        if f"{fn} {ln}".lower() == name_lower:
            cache_set(cache_key, p, ttl_hours=24)
            return p

    # Eerste resultaat als fallback
    result = people[0]
    cache_set(cache_key, result, ttl_hours=24)
    return result


# ─── Spelerstats via game log ─────────────────────────────────────────────────

def _current_season() -> int:
    today = datetime.date.today()
    # MLB seizoen loopt april–oktober; vóór april gebruiken we vorig jaar
    return today.year if today.month >= 3 else today.year - 1


def get_player_stats(player_id: int, position_type: str = "hitting", n_games: int = 20) -> dict:
    """
    Per-game stats voor de huidige MLB seizoen.
    position_type: "hitting" of "pitching"

    Geeft raw waarden + gemiddelden voor dynamische hit rate berekening.
    """
    season    = _current_season()
    cache_key = f"mlb_stats_{player_id}_{position_type}_{season}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    group = "pitching" if position_type == "pitching" else "hitting"
    data  = _get(
        f"{BASE}/people/{player_id}/stats"
        f"?stats=gameLog&season={season}&group={group}&sportId=1"
    )
    splits = data.get("stats", [{}])[0].get("splits", [])

    if not splits:
        return {"games_sampled": 0, "source": f"MLB API ({group})"}

    # Nieuwste games eerst
    splits = list(reversed(splits))[:n_games]

    def s(split, key, default=0):
        return split.get("stat", {}).get(key, default)

    def _f(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    n = len(splits)

    if group == "hitting":
        hits   = [_f(s(g, "hits"))      for g in splits]
        runs   = [_f(s(g, "runs"))      for g in splits]
        rbi    = [_f(s(g, "rbi"))       for g in splits]
        hrs    = [_f(s(g, "homeRuns"))  for g in splits]
        dbl    = [_f(s(g, "doubles"))   for g in splits]
        trp    = [_f(s(g, "triples"))   for g in splits]
        bb     = [_f(s(g, "baseOnBalls")) for g in splits]
        so     = [_f(s(g, "strikeOuts")) for g in splits]
        sb     = [_f(s(g, "stolenBases")) for g in splits]

        # Total bases = H + 2B + 2×3B + 3×HR
        total_bases = [
            h + d + 2 * t + 3 * r
            for h, d, t, r in zip(hits, dbl, trp, hrs)
        ]

        result = {
            "games_sampled": n,
            "source": f"MLB API (batting) — {season}",
            "position_type": "hitting",

            # Raw per-game waarden
            "raw_mlb_hits":    hits,
            "raw_runs":        runs,
            "raw_rbi":         rbi,
            "raw_home_runs":   hrs,
            "raw_total_bases": total_bases,
            "raw_strikeouts":  so,
            "raw_walks":       bb,
            "raw_stolen_bases": sb,

            # Gemiddelden
            "avg_hits":        avg(hits),
            "avg_runs":        avg(runs),
            "avg_rbi":         avg(rbi),
            "avg_home_runs":   avg(hrs),
            "avg_total_bases": avg(total_bases),
        }

    else:  # pitching
        k    = [_f(s(g, "strikeOuts"))      for g in splits]
        er   = [_f(s(g, "earnedRuns"))       for g in splits]
        h    = [_f(s(g, "hits"))             for g in splits]
        bb   = [_f(s(g, "baseOnBalls"))      for g in splits]
        ip_s = [str(s(g, "inningsPitched", "0.0")) for g in splits]
        ip   = [_f(v) for v in ip_s]

        result = {
            "games_sampled": n,
            "source": f"MLB API (pitching) — {season}",
            "position_type": "pitching",

            "raw_strikeouts":    k,
            "raw_earned_runs":   er,
            "raw_hits_allowed":  h,
            "raw_walks":         bb,

            "avg_strikeouts":    avg(k),
            "avg_earned_runs":   avg(er),
            "avg_hits_allowed":  avg(h),
            "avg_innings":       avg(ip),
        }

    cache_set(cache_key, result, ttl_hours=6)
    return result


def get_team_defense(team_id: int) -> dict:
    """Beperkte teamstats — ERA is beschikbaar via standings."""
    return {}


# ─── Auto-props helpers ───────────────────────────────────────────────────────

def get_today_games() -> list:
    """Geeft vandaag's MLB wedstrijden: [{home_team_id, away_team_id, home_team_name, away_team_name}]."""
    today = datetime.date.today().isoformat()
    cache_key = f"mlb_today_{today}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    data  = _get(f"{BASE}/schedule?date={today}&sportId=1&gameType=R")
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            home = g.get("teams", {}).get("home", {}).get("team", {})
            away = g.get("teams", {}).get("away", {}).get("team", {})
            if home.get("id") and away.get("id"):
                games.append({
                    "home_team_id":   home["id"],
                    "away_team_id":   away["id"],
                    "home_team_name": home.get("name", ""),
                    "away_team_name": away.get("name", ""),
                })
    cache_set(cache_key, games, ttl_hours=2)
    return games


def get_team_players(team_id: int, n: int = 5, position_type: str = "hitting") -> list:
    """Haal actieve batters of pitchers op voor een MLB team."""
    cache_key = f"mlb_roster_{team_id}_{position_type}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached[:n]
    season = _current_season()
    data   = _get(f"{BASE}/teams/{team_id}/roster?rosterType=active&season={season}")
    players = []
    for p in data.get("roster", []):
        person   = p.get("person", {})
        pos_code = p.get("position", {}).get("code", "")
        pos_type = "pitching" if pos_code == "1" else "hitting"
        if pos_type != position_type:
            continue
        pid = person.get("id")
        if pid:
            players.append({
                "name":     person.get("fullName", ""),
                "id":       pid,
                "position": pos_code,
            })
    cache_set(cache_key, players, ttl_hours=6)
    return players[:n]
