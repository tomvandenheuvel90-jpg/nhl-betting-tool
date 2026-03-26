"""
NBA module — nba_api (pip install nba_api) voor per-game stats van NBA.com.
Volledig gratis, geen API key nodig.

Ondersteunde bet types:
  points, assists, rebounds, 3-pointers made, blocks, steals
"""

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


# ─── Spelerstats via game log ─────────────────────────────────────────────────

def get_player_stats(player_id: int, n_games: int = 20) -> dict:
    """
    Per-game stats voor de huidige NBA seizoen via nba_api.
    Geeft raw waarden + gemiddelden voor dynamische hit rate berekening.
    """
    if not NBA_API_AVAILABLE:
        return {"games_sampled": 0, "source": "nba_api niet geïnstalleerd"}

    season    = _current_season()
    cache_key = f"nba_gamelog_{player_id}_{season}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

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
        print(f"  ⚠️  NBA game log fout (speler {player_id}): {e}")
        return {"games_sampled": 0, "source": "nba_api fout"}

    if df is None or df.empty:
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
    min_  = col("MIN") if "MIN" in recent.columns else []

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
