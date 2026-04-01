"""
NBA module — nba_api (pip install nba_api) voor per-game stats van NBA.com.
Volledig gratis, geen API key nodig.

Ondersteunde bet types:
  points, assists, rebounds, 3-pointers made, blocks, steals
"""

import csv
import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set
from rate_limiter import nba_limiter

try:
    from nba_api.stats.endpoints import playergamelog
    from nba_api.stats.static import players as _nba_static
    NBA_API_AVAILABLE = True
except ImportError:
    NBA_API_AVAILABLE = False
    print("⚠️  nba_api niet gevonden. Installeer met: pip install nba_api")

_NBA_DATA_DIR = Path(__file__).parent.parent / "nba_data"


def _current_season() -> str:
    today = datetime.date.today()
    year  = today.year - (1 if today.month < 7 else 0)
    return f"{year}-{str(year + 1)[-2:]}"


# ─── Speler opzoeken ──────────────────────────────────────────────────────────

def find_player(name: str):
    """Zoek NBA speler op naam. Geeft player dict of None."""
    if not NBA_API_AVAILABLE:
        return None

    cache_key = f"nba_player_{name.lower().replace(' ', '_')}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    all_players = _nba_static.get_players()
    name_lower  = name.strip().lower()
    parts       = name_lower.split()

    # Exacte match
    for p in all_players:
        if p["full_name"].lower() == name_lower:
            cache_set(cache_key, p, ttl_hours=24)
            return p

    # Initiaal + achternaam: "L. James" → LeBron James
    if len(parts) >= 2 and len(parts[0].rstrip(".")) <= 2:
        initial = parts[0].rstrip(".")
        ln      = parts[-1]
        for p in all_players:
            if (
                p.get("is_active", False)
                and p["last_name"].lower() == ln
                and p["first_name"].lower().startswith(initial)
            ):
                cache_set(cache_key, p, ttl_hours=24)
                return p

    # Achternaam only
    if parts:
        ln = parts[-1]
        matches = [
            p for p in all_players
            if p["last_name"].lower() == ln and p.get("is_active", False)
        ]
        if len(matches) == 1:
            cache_set(cache_key, matches[0], ttl_hours=24)
            return matches[0]

    return None


# ─── CSV fallback (uit lokale nba_data/ bestanden) ────────────────────────────

def _stats_from_csv(player_id: int) -> dict:
    """
    Fallback: lees seizoensgemiddelden uit lokale nba_data/{jaar}/players.csv.
    Geeft Poisson-geschikte gemiddelden terug (geen raw per-game waarden).
    Probeert nieuwste seizoen eerst.
    """
    today = datetime.date.today()
    current_year = today.year if today.month >= 7 else today.year - 1
    years_to_try = [current_year + 1, current_year, current_year - 1]

    pid_str = str(player_id)
    for year in years_to_try:
        csv_path = _NBA_DATA_DIR / str(year) / "players.csv"
        if not csv_path.exists():
            continue
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("player_id", "")).strip() == pid_str:
                        def _f(val):
                            try: return float(val)
                            except (TypeError, ValueError): return 0.0
                        pts = _f(row.get("pts", 0))
                        ast = _f(row.get("ast", 0))
                        reb = _f(row.get("reb", 0))
                        fg3m = _f(row.get("fg3m", 0))
                        blk  = _f(row.get("blk", 0))
                        stl  = _f(row.get("stl", 0))
                        gp   = max(int(_f(row.get("games_played", 1))), 1)
                        print(f"  📋  NBA CSV fallback: {row.get('name','?')} ({year}, {gp} games)")
                        return {
                            "games_sampled": gp,
                            "source": f"NBA CSV lokaal ({year})",
                            # Geen raw per-game lijsten — scorer gebruikt avg_ als Poisson lam
                            "raw_pts":    [], "raw_ast": [], "raw_reb":    [],
                            "raw_threes": [], "raw_blk": [], "raw_stl":    [],
                            "raw_tov":    [],
                            "avg_points":   pts,
                            "avg_assists":  ast,
                            "avg_rebounds": reb,
                            "avg_threes":   fg3m,
                            "avg_blocks":   blk,
                            "avg_steals":   stl,
                            "avg_turnovers": 0.0,
                            # Poisson lambda hints voor scorer
                            "hist_pts_avg":    pts,
                            "hist_ast_avg":    ast,
                            "hist_reb_avg":    reb,
                            "hist_threes_avg": fg3m,
                            "hist_blk_avg":    blk,
                            "hist_stl_avg":    stl,
                        }
        except Exception as e:
            print(f"  ⚠️  NBA CSV leesfout ({csv_path}): {e}")
    return {}


# ─── Spelerstats via game log ─────────────────────────────────────────────────

def get_player_stats(player_id: int, n_games: int = 20) -> dict:
    """
    Per-game stats voor de huidige NBA seizoen via nba_api.
    Geeft raw waarden + gemiddelden voor dynamische hit rate berekening.
    Fallback: lokale nba_data/ CSV als nba_api faalt of rate-limited is.
    """
    season    = _current_season()
    cache_key = f"nba_gamelog_{player_id}_{season}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    if not NBA_API_AVAILABLE:
        result = _stats_from_csv(player_id)
        if result:
            return result
        return {"games_sampled": 0, "source": "nba_api niet geïnstalleerd"}

    nba_limiter.wait()
    try:
        log = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star="Regular Season",
            timeout=30,
        )
        df = log.get_data_frames()[0]
    except Exception as e:
        print(f"  ⚠️  NBA game log fout (speler {player_id}): {type(e).__name__}: {e}")
        # Fallback op lokale CSV seizoensgemiddelden
        result = _stats_from_csv(player_id)
        if result:
            cache_set(cache_key, result, ttl_hours=1)
            return result
        return {"games_sampled": 0, "source": f"nba_api fout ({type(e).__name__})"}

    if df is None or df.empty:
        result = _stats_from_csv(player_id)
        if result:
            return result
        return {"games_sampled": 0, "source": "nba_api (geen data)"}

    recent = df.head(n_games)

    def col(name):
        if name not in recent.columns:
            return []
        return [float(v) if v is not None else 0.0 for v in recent[name].tolist()]

    pts   = col("PTS")
    ast   = col("AST")
    reb   = col("REB")
    fg3m  = col("FG3M")
    blk   = col("BLK")
    stl   = col("STL")
    tov   = col("TOV")

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    n = len(recent)
    result = {
        "games_sampled": n,
        "source": f"NBA.com (nba_api) — {season}",

        # Raw per-game waarden
        "raw_pts":    pts,
        "raw_ast":    ast,
        "raw_reb":    reb,
        "raw_threes": fg3m,
        "raw_blk":    blk,
        "raw_stl":    stl,
        "raw_tov":    tov,

        # Gemiddelden
        "avg_points":   avg(pts),
        "avg_assists":  avg(ast),
        "avg_rebounds": avg(reb),
        "avg_threes":   avg(fg3m),
        "avg_blocks":   avg(blk),
        "avg_steals":   avg(stl),
        "avg_turnovers": avg(tov),
    }

    cache_set(cache_key, result, ttl_hours=6)
    return result


def get_team_defense(team_name: str) -> dict:
    """Beperkte teamstats (nba_api gratis tier heeft geen uitgebreide defensieve stats)."""
    return {"team": team_name}


# ─── Auto-props helpers ───────────────────────────────────────────────────────

def find_team(name: str):
    """Zoek NBA team op (deel van) naam/stad/afkorting. Geeft team dict of None."""
    if not NBA_API_AVAILABLE:
        return None
    try:
        from nba_api.stats.static import teams as _nba_teams_static
        all_teams = _nba_teams_static.get_teams()
    except Exception:
        return None
    n = name.strip().lower()
    for t in all_teams:
        if (n in t.get("full_name", "").lower()
                or n == t.get("nickname", "").lower()
                or n == t.get("abbreviation", "").lower()
                or n == t.get("city", "").lower()):
            return t
    return None


def get_team_players(team_id: int, n: int = 5) -> list:
    """Haal actieve NBA roster op voor team_id via nba_api."""
    if not NBA_API_AVAILABLE:
        return []
    cache_key = f"nba_roster_{team_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached[:n]
    try:
        from nba_api.stats.endpoints import commonteamroster
        nba_limiter.wait()
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=30)
        df = roster.get_data_frames()[0]
        players = [
            {"name": row["PLAYER"], "id": int(row["PLAYER_ID"]), "team_id": team_id}
            for _, row in df.iterrows()
        ]
        cache_set(cache_key, players, ttl_hours=6)
        return players[:n]
    except Exception as e:
        print(f"  ⚠️  NBA roster fout (team {team_id}): {e}")
        return []


def get_team_form_for_match(team_name: str) -> dict:
    """
    Zoek een NBA team op naam en geef seizoensstatistieken terug voor het wedstrijd-model.
    Geeft dict met: abbrev, full_name, gp, pts_avg, opp_pts_avg, wins, losses,
                    home_record, road_record, last10, streak
    """
    if not NBA_API_AVAILABLE:
        return {}

    cache_key = f"nba_team_form_{team_name.strip().lower().replace(' ', '_')}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        from nba_api.stats.endpoints import leaguestandingsv3
        nba_limiter.wait()
        season = _current_season()
        standings = leaguestandingsv3.LeagueStandingsV3(
            season=season,
            season_type="Regular Season",
            timeout=30,
        )
        df = standings.get_data_frames()[0]
    except Exception as e:
        print(f"  ⚠️  NBA standings fout: {e}")
        return {}

    search = team_name.strip().lower()
    result = {}
    for _, row in df.iterrows():
        team_city    = str(row.get("TeamCity", "")).lower()
        team_nm      = str(row.get("TeamName", "")).lower()
        team_slug    = str(row.get("TeamSlug", "")).lower()
        full         = f"{team_city} {team_nm}"
        if not any([
            search == full,
            search == team_nm,
            search in full,
            search in team_slug,
        ]):
            continue

        gp    = max(int(row.get("WINS", 0)) + int(row.get("LOSSES", 0)), 1)
        wins  = int(row.get("WINS", 0))
        loss  = int(row.get("LOSSES", 0))

        pts_avg     = round(float(row.get("PointsPerGame", 110.0) or 110.0), 1)
        opp_pts_avg = round(float(row.get("OppPointsPerGame", 110.0) or 110.0), 1)

        hw  = int(row.get("HOME_WINS",   0) or 0)
        hl  = int(row.get("HOME_LOSSES", 0) or 0)
        rw  = int(row.get("ROAD_WINS",   0) or 0)
        rl  = int(row.get("ROAD_LOSSES", 0) or 0)

        l10w = int(row.get("L10_WINS",   0) or 0)
        l10l = int(row.get("L10_LOSSES", 0) or 0)

        result = {
            "abbrev":       str(row.get("TeamAbbreviation", "")),
            "full_name":    f"{row.get('TeamCity', '')} {row.get('TeamName', '')}".strip(),
            "gp":           gp,
            "wins":         wins,
            "losses":       loss,
            "pts_avg":      pts_avg,
            "opp_pts_avg":  opp_pts_avg,
            "home_record":  f"{hw}-{hl}",
            "road_record":  f"{rw}-{rl}",
            "last10":       f"{l10w}-{l10l}",
            "streak":       str(row.get("strCurrentStreak", "") or ""),
        }
        break

    if result:
        cache_set(cache_key, result, ttl_hours=6)
    return result


def get_team_last10_stats(team_name: str) -> dict:
    """
    Haalt pts en opp_pts op voor de laatste 10 afgeronde wedstrijden van een NBA team
    via nba_api TeamGameLog.

    opp_pts wordt afgeleid als: PTS - PLUS_MINUS (= gescoorde punten - marge = toegestane punten).

    Geeft:
      {"last10_pts": float, "last10_opp_pts": float, "last10_games": int}
    of {} als data niet beschikbaar is (fallback naar seizoensgemiddelde).
    """
    if not NBA_API_AVAILABLE:
        return {}

    cache_key = f"nba_last10_{team_name.strip().lower().replace(' ', '_')}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    team = find_team(team_name)
    if not team:
        return {}
    team_id = team.get("id")
    if not team_id:
        return {}

    try:
        from nba_api.stats.endpoints import teamgamelog
        nba_limiter.wait()
        season = _current_season()
        log = teamgamelog.TeamGameLog(
            team_id=team_id,
            season=season,
            season_type_all_star="Regular Season",
            timeout=30,
        )
        df = log.get_data_frames()[0]
    except Exception as e:
        print(f"  ⚠️  NBA team game log fout ({team_name}): {e}")
        return {}

    if df is None or df.empty:
        return {}

    # nba_api geeft nieuwste games eerst — pak de eerste 10 rijen
    recent_10 = df.head(10)
    if "PTS" not in recent_10.columns or "PLUS_MINUS" not in recent_10.columns:
        return {}

    pts_list     = [float(v) for v in recent_10["PTS"].tolist()        if v is not None]
    pm_list      = [float(v) for v in recent_10["PLUS_MINUS"].tolist() if v is not None]

    if not pts_list or len(pts_list) != len(pm_list):
        return {}

    # PLUS_MINUS = team_pts - opp_pts → opp_pts = team_pts - PLUS_MINUS
    opp_pts_list = [p - pm for p, pm in zip(pts_list, pm_list)]

    n      = len(pts_list)
    result = {
        "last10_pts":     round(sum(pts_list)     / n, 1),
        "last10_opp_pts": round(sum(opp_pts_list) / n, 1),
        "last10_games":   n,
    }
    cache_set(cache_key, result, ttl_hours=2)
    print(f"  📊  NBA last-10 {team_name}: PTS {result['last10_pts']} | OPP {result['last10_opp_pts']} ({n} games)")
    return result


def get_today_games() -> list:
    """Geeft vandaag's NBA wedstrijden: [{home_team_id, away_team_id}]."""
    if not NBA_API_AVAILABLE:
        return []
    cache_key = "nba_today_games"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from nba_api.stats.endpoints import scoreboardv2
        nba_limiter.wait()
        board = scoreboardv2.ScoreboardV2(timeout=30)
        df = board.get_data_frames()[0]  # GameHeader
        games = [
            {
                "home_team_id": int(row.get("HOME_TEAM_ID", 0)),
                "away_team_id": int(row.get("VISITOR_TEAM_ID", 0)),
            }
            for _, row in df.iterrows()
            if row.get("HOME_TEAM_ID") and row.get("VISITOR_TEAM_ID")
        ]
        cache_set(cache_key, games, ttl_hours=2)
        return games
    except Exception as e:
        print(f"  ⚠️  NBA scoreboard fout: {e}")
        return []
