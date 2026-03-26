"""
NHL module — NHL API voor spelersopzoek/schema/teamstats + MoneyPuck CSV voor per-game stats.
Geen API key nodig. MoneyPuck CSV wordt gedownload en gecacht (6 uur).

Ondersteunde bet types (via raw game values):
  shots on goal, blocked shots, goals (anytime scorer), assists, points, hits
"""

import csv
import io
import urllib.request
import json
import datetime
from functools import lru_cache
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set
from rate_limiter import nhl_limiter, moneypuck_limiter
from moneypuck_local import career_averages, playoff_averages

NHL_BASE            = "https://api-web.nhle.com/v1"
_TEAM_PLAYERS_CACHE: dict = {}   # team_abbrev → [{name, id, team, position}]
MP_GAMELOG_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/2024/regular/skaters.csv"
MP_SEASON_URL  = "https://moneypuck.com/moneypuck/playerData/seasonSummary/2024/regular/skaters.csv"
SEASON         = "20242025"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _nhl_get(url: str) -> dict:
    nhl_limiter.wait()
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  NHL API fout: {e}")
        return {}


def _mp_download_csv(url: str, cache_key: str, ttl_hours: int = 6) -> list:
    """Download MoneyPuck CSV, return als lijst van dicts. Gecacht."""
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    print(f"  📥  MoneyPuck downloaden ({cache_key})…")
    moneypuck_limiter.wait()
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = r.read().decode("utf-8-sig")  # BOM-safe
    except Exception as e:
        print(f"  ⚠️  MoneyPuck download mislukt: {e}")
        return []

    rows = list(csv.DictReader(io.StringIO(content)))
    cache_set(cache_key, rows, ttl_hours)
    print(f"  ✅  {len(rows):,} rijen gecacht ({cache_key})")
    return rows


# ─── Speler opzoeken via NHL API ──────────────────────────────────────────────

@lru_cache(maxsize=1)
def _all_rosters() -> dict:
    """Bouw name→(player_id, team) index van alle NHL-teams.
    Vult tegelijk _TEAM_PLAYERS_CACHE (skaters per team, geen goalies)."""
    global _TEAM_PLAYERS_CACHE
    standings = _nhl_get(f"{NHL_BASE}/standings/now").get("standings", [])
    teams = [
        t.get("teamAbbrev", {}).get("default")
        for t in standings
        if t.get("teamAbbrev")
    ]
    index: dict = {}
    _TEAM_PLAYERS_CACHE = {}
    for team in teams:
        if not team:
            continue
        roster = _nhl_get(f"{NHL_BASE}/roster/{team}/{SEASON}")
        _TEAM_PLAYERS_CACHE.setdefault(team, [])
        for pos in ("forwards", "defensemen", "goalies"):
            for p in roster.get(pos, []):
                fn  = p.get("firstName", {}).get("default", "")
                ln  = p.get("lastName", {}).get("default", "")
                pid = p.get("id")
                if not pid:
                    continue
                for key in (
                    f"{fn} {ln}".lower(),
                    f"{fn[0]}. {ln}".lower() if fn else ln.lower(),
                    ln.lower(),
                ):
                    index.setdefault(key, (pid, team))
                # Skaters only (geen goalies) in de teamcache
                if pos != "goalies" and fn and ln:
                    _TEAM_PLAYERS_CACHE[team].append({
                        "name":     f"{fn} {ln}",
                        "id":       pid,
                        "team":     team,
                        "position": "forward" if pos == "forwards" else "defenseman",
                    })
    return index


def find_player(name: str):
    """Geeft (player_id, team_abbrev) of (None, None)."""
    roster = _all_rosters()
    key = name.strip().lower()
    if key in roster:
        return roster[key]
    # Fuzzy: achternaam
    parts = key.split()
    ln = parts[-1] if parts else key
    for k, v in roster.items():
        if k.split()[-1] == ln:
            return v
    return None, None


# ─── Schema & teamstats ───────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _today_schedule() -> dict:
    today = datetime.date.today().isoformat()
    data  = _nhl_get(f"{NHL_BASE}/schedule/{today}")
    matchups = {}
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            away = g.get("awayTeam", {}).get("abbrev")
            home = g.get("homeTeam", {}).get("abbrev")
            if away and home:
                matchups[away] = home
                matchups[home] = away
    return matchups


def get_opponent(team_abbrev: str):
    return _today_schedule().get(team_abbrev)


def get_team_defense(team_abbrev: str) -> dict:
    standings = _nhl_get(f"{NHL_BASE}/standings/now").get("standings", [])
    for t in standings:
        if t.get("teamAbbrev", {}).get("default") == team_abbrev:
            gp = t.get("gamesPlayed") or 1
            return {
                "goals_against_avg": round(t.get("goalAgainst", 0) / gp, 2),
                "wins":   t.get("wins", 0),
                "losses": t.get("losses", 0),
                "points": t.get("points", 0),
            }
    return {}


# ─── MoneyPuck stats ──────────────────────────────────────────────────────────

def _f(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def get_player_stats(player_id: int, n_games: int = 20) -> dict:
    """
    Haalt per-game stats op uit MoneyPuck game-by-game CSV.
    Primair: MoneyPuck. Fallback: NHL API game-log (geen hits/blocks).

    Geeft dict met:
      - raw_shots, raw_blocks, raw_goals, raw_assists, raw_points, raw_hits
      - avg_* gemiddelden
      - games_sampled, source
      - advanced MoneyPuck seizoensstats (corsi, fenwick, xGoals, ...)
    """
    cache_key = f"nhl_stats_{player_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    rows = _mp_download_csv(MP_GAMELOG_URL, "mp_gamelog_2024", ttl_hours=6)
    pid_str = str(player_id)

    player_games = [
        g for g in rows
        if g.get("playerId") == pid_str or g.get("player_id") == pid_str
    ]

    if not player_games:
        print(f"  ⚠️  {player_id} niet in MoneyPuck → NHL API fallback")
        result = _stats_from_nhl_api(player_id, n_games)
    else:
        # Nieuwste games eerst (gameId is oplopend numeriek)
        player_games.sort(key=lambda g: g.get("gameId", "0"), reverse=True)
        recent = player_games[:n_games]
        result = _build_stats_from_mp(recent, player_id)

    # Historische context uit lokale Moneypuck bestanden (geen netwerk)
    result.update(career_averages(player_id))
    result.update(playoff_averages(player_id))

    cache_set(cache_key, result, ttl_hours=6)
    return result


def _build_stats_from_mp(recent: list, player_id: int) -> dict:
    """Verwerk MoneyPuck game-rijen naar stats dict."""

    def col(g, name):
        return _f(g.get(name, 0))

    shots     = [col(g, "I_F_shotsOnGoal")          for g in recent]
    attempts  = [col(g, "I_F_shotAttempts")          for g in recent]
    blocks    = [col(g, "I_F_blockedShotAttempts")   for g in recent]
    goals     = [col(g, "I_F_goals")                 for g in recent]
    p_ast     = [col(g, "I_F_primaryAssists")        for g in recent]
    s_ast     = [col(g, "I_F_secondaryAssists")      for g in recent]
    assists   = [p + s for p, s in zip(p_ast, s_ast)]
    points    = [col(g, "I_F_points")                for g in recent]
    hits      = [col(g, "I_F_hits")                  for g in recent]
    xg        = [col(g, "I_F_xGoals")                for g in recent]
    hd_xg     = [col(g, "I_F_highDangerxGoals")      for g in recent]
    ld_shots  = [col(g, "I_F_lowDangerShots")        for g in recent]
    md_shots  = [col(g, "I_F_mediumDangerShots")     for g in recent]
    hd_shots  = [col(g, "I_F_highDangerShots")       for g in recent]
    missed    = [col(g, "I_F_missedShots")            for g in recent]
    takeaways = [col(g, "I_F_takeaways")             for g in recent]
    giveaways = [col(g, "I_F_giveaways")             for g in recent]

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    n = len(recent)
    result = {
        "games_sampled": n,
        "source": "MoneyPuck",

        # Raw per-game waarden (voor dynamische hit rate berekening in scorer)
        "raw_shots":    shots,
        "raw_blocks":   blocks,
        "raw_goals":    goals,
        "raw_assists":  assists,
        "raw_points":   points,
        "raw_hits":     hits,

        # Gemiddelden
        "avg_shots":         avg(shots),
        "avg_shot_attempts": avg(attempts),
        "avg_blocks":        avg(blocks),
        "avg_goals":         avg(goals),
        "avg_assists":       avg(assists),
        "avg_points":        avg(points),
        "avg_hits":          avg(hits),
        "avg_xgoals":        avg(xg),
        "avg_hd_xgoals":     avg(hd_xg),
        "avg_missed_shots":  avg(missed),
        "avg_takeaways":     avg(takeaways),
        "avg_giveaways":     avg(giveaways),
        "avg_ld_shots":      avg(ld_shots),
        "avg_md_shots":      avg(md_shots),
        "avg_hd_shots":      avg(hd_shots),
    }

    # Seizoenscontext uit MoneyPuck season summary
    season_rows = _mp_download_csv(MP_SEASON_URL, "mp_season_2024", ttl_hours=6)
    pid_str = str(player_id)
    for row in season_rows:
        if row.get("playerId") == pid_str or row.get("player_id") == pid_str:
            gp = _f(row.get("games_played", 1)) or 1
            result.update({
                "season_goals":   _f(row.get("I_F_goals")),
                "season_assists": _f(row.get("I_F_primaryAssists", 0)) + _f(row.get("I_F_secondaryAssists", 0)),
                "season_shots":   _f(row.get("I_F_shotsOnGoal")),
                "season_xgoals":  _f(row.get("I_F_xGoals")),
                "season_hd_xg":   _f(row.get("I_F_highDangerxGoals")),
                "corsi_pct":      _f(row.get("onIce_corsiPercentage")),
                "fenwick_pct":    _f(row.get("onIce_fenwickPercentage")),
                "xg_pct":         _f(row.get("onIce_xGoalsPercentage")),
                "position":       row.get("position", ""),
                "icetime_avg":    _f(row.get("icetime", 0)) / gp,
            })
            break

    return result


def _stats_from_nhl_api(player_id: int, n_games: int = 20) -> dict:
    """Fallback: gebruik NHL API game-log. Geeft shots/goals/assists/points (geen hits/blocks)."""
    game_log = _nhl_get(
        f"{NHL_BASE}/player/{player_id}/game-log/{SEASON}/2"
    ).get("gameLog", [])

    shots   = []
    goals   = []
    assists = []
    points  = []

    for g in game_log[:n_games]:
        shots.append(_f(g.get("shots", 0) or g.get("sog", 0)))
        g_   = _f(g.get("goals", 0))
        a    = _f(g.get("assists", 0))
        goals.append(g_)
        assists.append(a)
        points.append(g_ + a)

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    result = {
        "games_sampled": len(shots),
        "source": "NHL API (fallback — geen hits/blocks)",
        "raw_shots":   shots,
        "raw_blocks":  [],
        "raw_goals":   goals,
        "raw_assists": assists,
        "raw_points":  points,
        "raw_hits":    [],
        "avg_shots":   avg(shots),
        "avg_goals":   avg(goals),
        "avg_assists": avg(assists),
        "avg_points":  avg(points),
    }
    result.update(career_averages(player_id))
    result.update(playoff_averages(player_id))
    return result


# ─── Auto-props helpers ───────────────────────────────────────────────────────

def get_today_teams() -> list:
    """Alle teamafkortingen die vandaag een NHL-wedstrijd spelen."""
    try:
        return list(set(_today_schedule().keys()))
    except Exception:
        return []


def get_team_players(team_abbrev: str, n: int = 8) -> list:
    """
    Geeft lijst van {name, id, team, position} voor skaters (geen goalies) in een team.
    Gebruikt de _TEAM_PLAYERS_CACHE die gevuld wordt door _all_rosters().
    """
    _all_rosters()   # zorgt dat cache gevuld is
    return list(_TEAM_PLAYERS_CACHE.get(team_abbrev, []))[:n]
