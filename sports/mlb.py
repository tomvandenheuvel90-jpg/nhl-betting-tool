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
import csv
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from cache import get as cache_get, set as cache_set
from rate_limiter import mlb_limiter

BASE    = "https://statsapi.mlb.com/api/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 BetAnalyzer/1.0"}

_log = logging.getLogger(__name__)

# MLB CSV data directory (mlb_data/ naast de project root)
_CSV_DIR = Path(__file__).parent.parent / "mlb_data"


# ─── HTTP helper ──────────────────────────────────────────────────────────────

def _get(url: str) -> dict:
    mlb_limiter.wait()
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning(f"[MLB] API fout voor {url}: {type(e).__name__}: {e}")
        print(f"  ⚠️  MLB API fout ({type(e).__name__}): {e}")
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
    # MLB seizoen start laat maart; vóór 15 april gebruiken we vorig jaar
    # zodat we genoeg data hebben (seizoen net begonnen = weinig splits)
    if today.month < 4 or (today.month == 4 and today.day < 15):
        return today.year - 1
    return today.year


# ─── CSV fallback ─────────────────────────────────────────────────────────────

def _stats_from_csv(player_id: int, season: int, position_type: str) -> dict:
    """
    Laad geaggregeerde seizoensdata uit lokale CSV als API-fallback.
    Geeft dict met hist_mlb_* gemiddelden per game (voor Poisson blending).
    """
    fname  = "pitchers.csv" if position_type == "pitching" else "hitters.csv"
    csv_path = _CSV_DIR / str(season) / fname
    if not csv_path.exists():
        _log.debug(f"[MLB] CSV niet gevonden: {csv_path}")
        return {}

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if str(row.get("playerId", "")) != str(player_id):
                    continue
                gp = max(int(row.get("gamesPlayed", 1) or 1), 1)

                def _f(k):
                    try:
                        return float(row.get(k, 0) or 0)
                    except (TypeError, ValueError):
                        return 0.0

                if position_type != "pitching":
                    hits = _f("hits")
                    avg  = _f("avg") or 1e-9  # voorkom /0
                    slg  = _f("slg")
                    # total_bases = hits × (slg / avg)
                    tb = hits * (slg / avg) if avg > 0 else hits

                    result = {
                        "games_sampled":          0,   # geen per-game log
                        "source":                 f"MLB CSV (batting) — {season}",
                        "position_type":          "hitting",
                        "hist_mlb_hits_avg":      round(hits     / gp, 4),
                        "hist_mlb_home_runs_avg": round(_f("homeRuns") / gp, 4),
                        "hist_mlb_rbi_avg":       round(_f("rbi")      / gp, 4),
                        "hist_mlb_runs_avg":      round(_f("runs")     / gp, 4),
                        "hist_mlb_total_bases_avg": round(tb / gp, 4),
                        "hist_mlb_strikeouts_avg": round(_f("strikeOuts") / gp, 4),
                    }
                    _log.info(f"[MLB] CSV fallback {player_id} season={season}: {result}")
                    return result
                else:
                    result = {
                        "games_sampled":            0,
                        "source":                   f"MLB CSV (pitching) — {season}",
                        "position_type":            "pitching",
                        "hist_mlb_strikeouts_avg":  round(_f("strikeOuts") / gp, 4),
                    }
                    _log.info(f"[MLB] CSV fallback {player_id} season={season}: {result}")
                    return result
    except Exception as e:
        _log.warning(f"[MLB] CSV leesfout {csv_path}: {e}")

    return {}


def get_player_stats(player_id: int, position_type: str = "hitting", n_games: int = 20) -> dict:
    """
    Per-game stats voor de huidige MLB seizoen.
    position_type: "hitting" of "pitching"

    Geeft raw waarden + gemiddelden voor dynamische hit rate berekening.
    Fallback volgorde: API huidig seizoen → API vorig seizoen → lokale CSV
    """
    season    = _current_season()
    cache_key = f"mlb_stats_{player_id}_{position_type}_{season}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    group = "pitching" if position_type == "pitching" else "hitting"

    def _fetch_splits(s):
        data = _get(
            f"{BASE}/people/{player_id}/stats"
            f"?stats=gameLog&season={s}&group={group}&sportId=1"
        )
        return data.get("stats", [{}])[0].get("splits", [])

    splits = _fetch_splits(season)

    # Fallback: probeer vorig seizoen als huidig seizoen nog geen data heeft
    if not splits:
        prev = season - 1
        _log.info(f"[MLB] Geen splits voor {player_id} in {season}, probeer {prev}")
        splits = _fetch_splits(prev)
        if splits:
            _log.info(f"[MLB] Vorig seizoen ({prev}) gebruikt voor speler {player_id}")

    if not splits:
        # Laatste fallback: lokale CSV
        _log.info(f"[MLB] Geen API-data voor {player_id}, probeer CSV (season={season})")
        csv_result = _stats_from_csv(player_id, season, position_type)
        if not csv_result:
            csv_result = _stats_from_csv(player_id, season - 1, position_type)
        if csv_result:
            cache_set(cache_key, csv_result, ttl_hours=6)
            return csv_result
        _log.warning(f"[MLB] Geen data gevonden voor speler {player_id}")
        return {"games_sampled": 0, "source": f"MLB API ({group}) — geen data"}

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


def get_team_form_for_match(team_name: str) -> dict:
    """
    Zoek een MLB team op naam en geef seizoensstatistieken terug voor het wedstrijd-model.
    Geeft dict met: abbrev, full_name, gp, runs_avg, opp_runs_avg, wins, losses,
                    home_record, road_record, last10, streak
    """
    cache_key = f"mlb_team_form_{team_name.strip().lower().replace(' ', '_')}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    season = _current_season()
    data = _get(
        f"{BASE}/standings?leagueId=103,104&season={season}"
        f"&standingsTypes=regularSeason&hydrate=team,record,streak,division"
    )

    search = team_name.strip().lower()
    result = {}

    for record in data.get("records", []):
        for tr in record.get("teamRecords", []):
            team   = tr.get("team", {})
            name   = team.get("name", "").lower()
            abbrev = team.get("abbreviation", "").lower()
            if not any([search == name, search in name, search == abbrev, abbrev in search]):
                continue

            gp     = max(int(tr.get("gamesPlayed", 1) or 1), 1)
            wins   = int(tr.get("wins", 0) or 0)
            losses = int(tr.get("losses", 0) or 0)

            # Runs for/against from leagueRecord splitting
            runs_avg     = round(float(tr.get("runsScored",   0) or 0) / gp, 2)
            opp_runs_avg = round(float(tr.get("runsAllowed",  0) or 0) / gp, 2)

            # Fallback to league average if 0
            if runs_avg == 0:
                runs_avg = 4.35
            if opp_runs_avg == 0:
                opp_runs_avg = 4.35

            home_rec  = tr.get("records", {}).get("splitRecords", [])
            home_wins = home_loss = road_wins = road_loss = 0
            for sr in home_rec:
                t = sr.get("type", "")
                w = int(sr.get("wins", 0) or 0)
                l = int(sr.get("losses", 0) or 0)
                if t == "home":
                    home_wins, home_loss = w, l
                elif t == "road":
                    road_wins, road_loss = w, l

            # Last 10
            l10 = tr.get("records", {}).get("expectedRecords", [])
            streak_info = tr.get("streak", {})
            streak = str(streak_info.get("streakCode", "") or "")

            result = {
                "abbrev":       team.get("abbreviation", ""),
                "full_name":    team.get("name", team_name),
                "gp":           gp,
                "wins":         wins,
                "losses":       losses,
                "runs_avg":     runs_avg,
                "opp_runs_avg": opp_runs_avg,
                "home_record":  f"{home_wins}-{home_loss}",
                "road_record":  f"{road_wins}-{road_loss}",
                "last10":       f"{wins}-{losses}" if gp <= 10 else "",
                "streak":       streak,
            }
            break
        if result:
            break

    if result:
        cache_set(cache_key, result, ttl_hours=6)
    return result


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
