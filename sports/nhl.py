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


def _mp_year() -> int:
    """
    Moneypuck gebruikt het EINDJAAR van het seizoen als mapnaam.
    2025-26 seizoen (okt 2025 – jun 2026) → jaar 2026.
    Formule: als maand >= 7 dan huidigjaar+1, anders huidigjaar.
    """
    today = datetime.date.today()
    return today.year + 1 if today.month >= 7 else today.year


def _nhl_season() -> str:
    """NHL API seizoensformat: '20252026' voor het 2025-26 seizoen."""
    y = _mp_year()
    return f"{y - 1}{y}"


MP_GAMELOG_URL = (
    "https://moneypuck.com/moneypuck/playerData/careers/gameByGame"
    f"/{_mp_year()}/regular/skaters.csv"
)
MP_SEASON_URL = (
    "https://moneypuck.com/moneypuck/playerData/seasonSummary"
    f"/{_mp_year()}/regular/skaters.csv"
)
SEASON = _nhl_season()

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

    mp_year = _mp_year()
    rows = _mp_download_csv(MP_GAMELOG_URL, f"mp_gamelog_{mp_year}", ttl_hours=6)

    # Als huidig seizoen nog niet beschikbaar is op MoneyPuck, gebruik vorig jaar
    if not rows:
        prev_year = mp_year - 1
        fallback_url = (
            "https://moneypuck.com/moneypuck/playerData/careers/gameByGame"
            f"/{prev_year}/regular/skaters.csv"
        )
        print(f"  ↩️  MoneyPuck {mp_year} niet beschikbaar → probeer {prev_year}")
        rows = _mp_download_csv(fallback_url, f"mp_gamelog_{prev_year}", ttl_hours=6)

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

    # Gebruik historische xGoals als fallback als realtime data ontbreekt
    if not result.get("avg_xgoals") and result.get("hist_xgoals_avg", 0) > 0:
        result["avg_xgoals"] = result["hist_xgoals_avg"]

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
    mp_year = _mp_year()
    season_rows = _mp_download_csv(MP_SEASON_URL, f"mp_season_{mp_year}", ttl_hours=6)
    if not season_rows:
        prev_year = mp_year - 1
        fallback_url = (
            "https://moneypuck.com/moneypuck/playerData/seasonSummary"
            f"/{prev_year}/regular/skaters.csv"
        )
        season_rows = _mp_download_csv(fallback_url, f"mp_season_{prev_year}", ttl_hours=6)
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
        "source": "NHL API (fallback — geen hits/blocks/xGoals)",
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
        # xGoals niet beschikbaar via NHL API — gebruik historische waarden hieronder
        "avg_xgoals":     0.0,
        "avg_hd_xgoals":  0.0,
    }
    result.update(career_averages(player_id))
    result.update(playoff_averages(player_id))
    # Gebruik historische xGoals als fallback (NHL API geeft geen xGoals)
    if result.get("hist_xgoals_avg", 0) > 0:
        result["avg_xgoals"] = result["hist_xgoals_avg"]
    return result


# ─── Team form (voor wedstrijd-analyse) ──────────────────────────────────────

def get_team_form(team_name: str) -> dict:
    """
    Haalt uitgebreide teamstats op voor een NHL-team via de standings API.

    team_name kan zijn:
      - Afkorting: "FLA", "MIN", "TBL"
      - Volledige naam: "Florida Panthers", "Minnesota Wild"
      - Deelnaam: "Panthers", "Wild"

    Geeft dict terug met:
      abbrev, full_name, gp, wins, losses, ot_losses, points, points_pct,
      gf_avg, ga_avg, home_record, road_record, last10, streak
    Of leeg dict als team niet gevonden.
    """
    cache_key = f"nhl_form_{team_name.strip().upper()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    standings = _nhl_get(f"{NHL_BASE}/standings/now").get("standings", [])
    search = team_name.strip().lower()
    result = {}

    for t in standings:
        abbrev     = t.get("teamAbbrev", {}).get("default", "")
        common     = t.get("teamCommonName", {}).get("default", "")   # "Panthers"
        place      = t.get("teamPlaceName", {}).get("default", "")    # "Florida"
        full       = f"{place} {common}".strip()                      # "Florida Panthers"

        if not any([
            abbrev.lower() == search,
            common.lower() == search,
            full.lower()   == search,
            search in full.lower(),
            search in common.lower(),
        ]):
            continue

        gp = max(t.get("gamesPlayed", 1), 1)

        # Last 10 games
        l10w  = t.get("l10Wins",      0)
        l10l  = t.get("l10Losses",    0)
        l10ot = t.get("l10OtLosses",  0)
        l10pts = l10w * 2 + l10ot          # punten laatste 10

        # Home / road records
        hw  = t.get("homeWins",       0)
        hl  = t.get("homeLosses",     0)
        hot = t.get("homeOtLosses",   0)
        rw  = t.get("roadWins",       0)
        rl  = t.get("roadLosses",     0)
        rot = t.get("roadOtLosses",   0)

        gf_avg = round(t.get("goalFor",     0) / gp, 2)
        ga_avg = round(t.get("goalAgainst", 0) / gp, 2)
        pts    = t.get("points", 0)

        result = {
            "abbrev":      abbrev,
            "full_name":   full or common or abbrev,
            "gp":          gp,
            "wins":        t.get("wins",     0),
            "losses":      t.get("losses",   0),
            "ot_losses":   t.get("otLosses", 0),
            "points":      pts,
            "points_pct":  round(pts / (gp * 2), 3),  # maximaal 1.0
            "gf_avg":      gf_avg,
            "ga_avg":      ga_avg,
            "home_record": f"{hw}-{hl}-{hot}",
            "road_record": f"{rw}-{rl}-{rot}",
            "last10":      f"{l10w}-{l10l}-{l10ot}",
            "last10_pts":  l10pts,
            "streak":      t.get("streakCode", ""),
        }
        break

    if result:
        cache_set(cache_key, result, ttl_hours=6)
    return result


# ─── Last-10 goals (voor blended wedstrijd-model) ─────────────────────────────

def get_team_last10_goals(team_abbrev: str) -> dict:
    """
    Haalt goals-for en goals-against op voor de laatste 10 afgeronde wedstrijden
    van een NHL team via de club-schedule-season API.

    Geeft:
      {"last10_gf_avg": float, "last10_ga_avg": float, "last10_games": int}
    of {} als data niet beschikbaar is (fallback naar seizoensgemiddelde).
    """
    if not team_abbrev:
        return {}

    cache_key = f"nhl_last10_{team_abbrev.upper()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    data  = _nhl_get(f"{NHL_BASE}/club-schedule-season/{team_abbrev.upper()}/{SEASON}")
    games = data.get("games", [])
    if not games:
        return {}

    # Filter afgeronde wedstrijden: score aanwezig aan beide kanten
    completed = [
        g for g in games
        if g.get("homeTeam", {}).get("score") is not None
        and g.get("awayTeam", {}).get("score") is not None
    ]
    if not completed:
        return {}

    # Spellen staan chronologisch — pak de laatste 10
    recent_10   = completed[-10:]
    abbrev_up   = team_abbrev.upper()
    gf_list: list = []
    ga_list: list = []

    for g in recent_10:
        home        = g.get("homeTeam", {})
        away        = g.get("awayTeam", {})
        home_abbrev = str(home.get("abbrev", "")).upper()
        away_abbrev = str(away.get("abbrev", "")).upper()
        home_score  = home.get("score")
        away_score  = away.get("score")
        if home_score is None or away_score is None:
            continue
        if home_abbrev == abbrev_up:
            gf_list.append(float(home_score))
            ga_list.append(float(away_score))
        elif away_abbrev == abbrev_up:
            gf_list.append(float(away_score))
            ga_list.append(float(home_score))

    if not gf_list:
        return {}

    n      = len(gf_list)
    result = {
        "last10_gf_avg": round(sum(gf_list) / n, 2),
        "last10_ga_avg": round(sum(ga_list) / n, 2),
        "last10_games":  n,
    }
    cache_set(cache_key, result, ttl_hours=2)
    print(f"  📊  NHL last-10 {abbrev_up}: GF {result['last10_gf_avg']} | GA {result['last10_ga_avg']} ({n} games)")
    return result


# ─── Auto-props helpers ───────────────────────────────────────────────────────

def get_today_teams() -> list:
    """Alle teamafkortingen die vandaag een NHL-wedstrijd spelen."""
    try:
        return list(set(_today_schedule().keys()))
    except Exception:
        return []


# ─── Venue-split last-10 goals (voor home/away weging) ───────────────────────

def get_team_split_last10_goals(team_abbrev: str, venue: str) -> dict:
    """
    Haalt last-10 goals-for/against op voor uitsluitend thuis- (venue='home') of
    uitwedstrijden (venue='away') via de club-schedule-season API.

    Geeft {} als minder dan 5 venue-specifieke wedstrijden beschikbaar zijn
    (→ caller valt terug op overall last-10).
    Geeft {"last10_gf_avg": float, "last10_ga_avg": float, "last10_games": int}.
    """
    if not team_abbrev:
        return {}
    venue     = venue.lower()
    abbrev_up = team_abbrev.upper()
    cache_key = f"nhl_split_{venue}_{abbrev_up}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    data  = _nhl_get(f"{NHL_BASE}/club-schedule-season/{abbrev_up}/{SEASON}")
    games = data.get("games", [])
    if not games:
        return {}

    # Filter op afgeronde wedstrijden + juiste venue
    if venue == "home":
        filtered = [
            g for g in games
            if g.get("homeTeam", {}).get("score") is not None
            and g.get("awayTeam", {}).get("score") is not None
            and str(g.get("homeTeam", {}).get("abbrev", "")).upper() == abbrev_up
        ]
    else:
        filtered = [
            g for g in games
            if g.get("homeTeam", {}).get("score") is not None
            and g.get("awayTeam", {}).get("score") is not None
            and str(g.get("awayTeam", {}).get("abbrev", "")).upper() == abbrev_up
        ]

    if len(filtered) < 5:
        return {}   # onvoldoende data → fallback naar overall in caller

    recent  = filtered[-10:]
    gf_list: list = []
    ga_list: list = []
    for g in recent:
        h_score = g.get("homeTeam", {}).get("score")
        a_score = g.get("awayTeam", {}).get("score")
        if h_score is None or a_score is None:
            continue
        if venue == "home":
            gf_list.append(float(h_score))
            ga_list.append(float(a_score))
        else:
            gf_list.append(float(a_score))
            ga_list.append(float(h_score))

    if not gf_list:
        return {}

    n      = len(gf_list)
    result = {
        "last10_gf_avg": round(sum(gf_list) / n, 2),
        "last10_ga_avg": round(sum(ga_list) / n, 2),
        "last10_games":  n,
    }
    cache_set(cache_key, result, ttl_hours=2)
    print(f"  📊  NHL {venue}-split {abbrev_up}: GF {result['last10_gf_avg']} | GA {result['last10_ga_avg']} ({n} games)")
    return result


# ─── Head-to-head resultaten ──────────────────────────────────────────────────

def get_h2h_results(home_abbrev: str, away_abbrev: str, n: int = 5) -> dict:
    """
    Haalt de laatste n head-to-head resultaten op tussen twee NHL teams
    (huidige seizoen; aangevuld met vorig seizoen als er < n wedstrijden zijn).

    Win/verlies wordt berekend vanuit het perspectief van home_abbrev
    (ongeacht of zij thuis of uit speelden in de H2H wedstrijd).

    Geeft:
      {"home_wins": int, "away_wins": int, "draws": int,
       "total": int, "home_win_rate": float}
    of {} als geen H2H data beschikbaar is.
    """
    if not home_abbrev or not away_abbrev:
        return {}
    h = home_abbrev.upper()
    a = away_abbrev.upper()
    cache_key = f"nhl_h2h_{h}_{a}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    def _fetch_season(season_str: str) -> list:
        data  = _nhl_get(f"{NHL_BASE}/club-schedule-season/{h}/{season_str}")
        found = []
        for g in data.get("games", []):
            ht       = str(g.get("homeTeam", {}).get("abbrev", "")).upper()
            at       = str(g.get("awayTeam", {}).get("abbrev", "")).upper()
            h_score  = g.get("homeTeam", {}).get("score")
            a_score  = g.get("awayTeam", {}).get("score")
            # Afgeronde H2H wedstrijd tussen deze twee teams
            if h_score is None or a_score is None:
                continue
            if {ht, at} != {h, a}:
                continue
            found.append({"home_t": ht, "away_t": at,
                           "home_s": float(h_score), "away_s": float(a_score)})
        return found

    games = _fetch_season(SEASON)
    if len(games) < n:
        prev_year   = int(SEASON[:4]) - 1
        prev_season = f"{prev_year}{prev_year + 1}"
        try:
            games += _fetch_season(prev_season)
        except Exception:
            pass

    games = games[-n:]   # neem de meest recente n
    if not games:
        return {}

    home_wins = away_wins = draws = 0
    for g in games:
        if g["home_t"] == h:
            # h was het thuisteam in deze H2H wedstrijd
            if g["home_s"] > g["away_s"]:   home_wins += 1
            elif g["home_s"] < g["away_s"]: away_wins += 1
            else:                            draws     += 1
        else:
            # h was het uitteam in deze H2H wedstrijd
            if g["away_s"] > g["home_s"]:   home_wins += 1
            elif g["away_s"] < g["home_s"]: away_wins += 1
            else:                            draws     += 1

    total  = home_wins + away_wins + draws
    result = {
        "home_wins":     home_wins,
        "away_wins":     away_wins,
        "draws":         draws,
        "total":         total,
        "home_win_rate": round(home_wins / total, 3) if total > 0 else 0.5,
    }
    cache_set(cache_key, result, ttl_hours=4)
    print(f"  🔁  NHL H2H {h} vs {a}: {home_wins}W-{away_wins}L-{draws}D ({total} games, wr={result['home_win_rate']})")
    return result


def get_team_players(team_abbrev: str, n: int = 8) -> list:
    """
    Geeft lijst van {name, id, team, position} voor skaters (geen goalies) in een team.
    Gebruikt de _TEAM_PLAYERS_CACHE die gevuld wordt door _all_rosters().
    """
    _all_rosters()   # zorgt dat cache gevuld is
    return list(_TEAM_PLAYERS_CACHE.get(team_abbrev, []))[:n]
