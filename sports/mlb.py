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


# ─── Head-to-head resultaten ──────────────────────────────────────────────────

def _find_team_id(team_name: str) -> int:
    """Zoek MLB team ID op (deel van) naam of afkorting via de standings API. Geeft 0 als niet gevonden."""
    season = _current_season()
    data   = _get(
        f"{BASE}/standings?leagueId=103,104&season={season}"
        f"&standingsTypes=regularSeason&hydrate=team"
    )
    search = team_name.strip().lower()
    for record in data.get("records", []):
        for tr in record.get("teamRecords", []):
            team   = tr.get("team", {})
            name   = team.get("name", "").lower()
            abbrev = team.get("abbreviation", "").lower()
            if any([search == name, search in name, search == abbrev, abbrev in search]):
                return int(team.get("id", 0))
    return 0


def get_h2h_results(home_name: str, away_name: str, n: int = 5) -> dict:
    """
    Haalt de laatste n head-to-head resultaten op tussen twee MLB teams via
    de schedule API (huidig seizoen + vorig seizoen als aanvulling).

    Win/verlies vanuit het perspectief van home_name (ongeacht locatie).

    Geeft:
      {"home_wins": int, "away_wins": int, "draws": int,
       "total": int, "home_win_rate": float}
    of {} als geen data beschikbaar is.
    """
    cache_key = (f"mlb_h2h_{home_name.strip().lower().replace(' ','_')}"
                 f"_{away_name.strip().lower().replace(' ','_')}")
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    home_id = _find_team_id(home_name)
    away_id = _find_team_id(away_name)
    if not home_id or not away_id:
        return {}

    def _fetch_h2h(yr: int) -> list:
        data   = _get(
            f"{BASE}/schedule?sportId=1&teamId={home_id}"
            f"&season={yr}&gameType=R&hydrate=team"
        )
        found  = []
        for date in data.get("dates", []):
            for g in date.get("games", []):
                status = g.get("status", {}).get("abstractGameState", "")
                if status != "Final":
                    continue
                teams  = g.get("teams", {})
                ht     = teams.get("home", {})
                at     = teams.get("away", {})
                h_id   = ht.get("team", {}).get("id", 0)
                a_id   = at.get("team", {}).get("id", 0)
                # Alleen H2H tussen de twee opgegeven teams
                if {h_id, a_id} != {home_id, away_id}:
                    continue
                h_score = ht.get("score")
                a_score = at.get("score")
                if h_score is None or a_score is None:
                    continue
                found.append({
                    "home_id": h_id, "away_id": a_id,
                    "home_s":  int(h_score), "away_s": int(a_score),
                })
        return found

    season = _current_season()
    games  = _fetch_h2h(season)
    if len(games) < n:
        try:
            games += _fetch_h2h(season - 1)
        except Exception:
            pass

    games = games[-n:]
    if not games:
        return {}

    home_wins = away_wins = draws = 0
    for g in games:
        if g["home_id"] == home_id:
            if g["home_s"] > g["away_s"]:   home_wins += 1
            elif g["home_s"] < g["away_s"]: away_wins += 1
            else:                            draws     += 1
        else:
            # home_name was het uitteam in deze wedstrijd
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
    print(f"  🔁  MLB H2H {home_name} vs {away_name}: {home_wins}W-{away_wins}L-{draws}D ({total} games, wr={result['home_win_rate']})")
    return result


# ─── Startende werper (probable pitcher) ─────────────────────────────────────

_LEAGUE_ERA = 4.35  # MLB gemiddelde ERA (referentie voor normalisatie)


_W_CURRENT = 0.60   # gewicht huidig seizoen
_W_PREV    = 0.40   # gewicht vorig seizoen


def _fetch_pitching_stat(player_id: int, season: int) -> dict:
    """Haal ruwe pitching stats op voor één seizoen. Geeft leeg dict als geen data."""
    data   = _get(f"{BASE}/people/{player_id}/stats?stats=season&season={season}&group=pitching&sportId=1")
    splits = data.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return {}
    stat = splits[0].get("stat", {})

    def _f(k, default=0.0):
        try:
            return float(stat.get(k) or default)
        except (TypeError, ValueError):
            return default

    ip = _f("inningsPitched", 0.0)
    k  = _f("strikeOuts", 0.0)
    return {
        "era":             _f("era",  _LEAGUE_ERA),
        "whip":            _f("whip", 1.30),
        "k_per_9":         round(k / ip * 9, 2) if ip > 0 else 0.0,
        "innings_pitched": round(ip, 1),
        "games_started":   int(stat.get("gamesStarted") or 0),
        "hits_per_9":      round(_f("hits") / ip * 9, 2) if ip > 0 else 0.0,
        "bb_per_9":        round(_f("baseOnBalls") / ip * 9, 2) if ip > 0 else 0.0,
        "hr_per_9":        round(_f("homeRuns") / ip * 9, 2) if ip > 0 else 0.0,
        "win_pct":         round(_f("wins") / max(_f("wins") + _f("losses"), 1), 3),
    }


def get_pitcher_season_stats(player_id: int) -> dict:
    """
    Haal pitchingstatistieken op, gewogen over huidig en vorig seizoen:
      60% huidig seizoen + 40% vorig seizoen (voor alle metrics).

    Als het huidige seizoen geen data heeft (begin seizoen / rookie),
    wordt 100% vorig seizoen gebruikt. Als beide ontbreken, leeg dict.

    Geeft: era, whip, k_per_9, hits_per_9, bb_per_9, hr_per_9, win_pct,
           innings_pitched, games_started, era_current, era_prev.
    """
    season    = _current_season()
    cache_key = f"mlb_pitcher_stats_v2_{player_id}_{season}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    cur  = _fetch_pitching_stat(player_id, season)
    prev = _fetch_pitching_stat(player_id, season - 1)

    if not cur and not prev:
        return {}

    # Als één seizoen ontbreekt, gebruik het andere volledig
    if not cur:
        result = {**prev, "era_current": None, "era_prev": round(prev["era"], 2),
                  "blend_note": "100% vorig seizoen (geen huidig seizoen data)"}
        cache_set(cache_key, result, ttl_hours=6)
        return result
    if not prev:
        result = {**cur, "era_current": round(cur["era"], 2), "era_prev": None,
                  "blend_note": "100% huidig seizoen (geen vorig seizoen data)"}
        cache_set(cache_key, result, ttl_hours=6)
        return result

    # Blend: 60% huidig + 40% vorig voor alle numerieke stats
    def _blend(key):
        return round(cur[key] * _W_CURRENT + prev[key] * _W_PREV, 2)

    result = {
        "era":             _blend("era"),
        "whip":            _blend("whip"),
        "k_per_9":         _blend("k_per_9"),
        "hits_per_9":      _blend("hits_per_9"),
        "bb_per_9":        _blend("bb_per_9"),
        "hr_per_9":        _blend("hr_per_9"),
        "win_pct":         _blend("win_pct"),
        "innings_pitched": round(cur["innings_pitched"], 1),
        "games_started":   cur["games_started"],
        # Bewaar ongewogen waarden voor weergave in UI
        "era_current":     round(cur["era"], 2),
        "era_prev":        round(prev["era"], 2),
        "blend_note":      f"60% {season} + 40% {season - 1}",
    }
    cache_set(cache_key, result, ttl_hours=6)
    return result


def get_probable_pitchers(home_team_name: str, away_team_name: str,
                          game_date: str = "") -> dict:
    """
    Zoek de startende werpers voor een wedstrijd op via de MLB schedule API
    met 'probablePitcher' hydration.

    Geeft dict: {"home": {name, era, whip, k_per_9, ...}, "away": {...}}
    Leeg sub-dict als de werper nog niet bekend is (bv. 2+ dagen vooruit).
    """
    if not game_date:
        game_date = datetime.date.today().isoformat()

    cache_key = f"mlb_pitchers_{home_team_name}_{away_team_name}_{game_date}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get(
        f"{BASE}/schedule?date={game_date}&sportId=1&gameType=R"
        f"&hydrate=probablePitcher"
    )

    home_lower = home_team_name.strip().lower()
    away_lower = away_team_name.strip().lower()
    result     = {"home": {}, "away": {}}

    for date_obj in data.get("dates", []):
        for game in date_obj.get("games", []):
            teams     = game.get("teams", {})
            home_team = teams.get("home", {}).get("team", {})
            away_team = teams.get("away", {}).get("team", {})
            h_name    = home_team.get("name", "").lower()
            a_name    = away_team.get("name", "").lower()

            # Controleer of dit de juiste wedstrijd is
            h_match = home_lower in h_name or h_name in home_lower or \
                      home_team.get("abbreviation","").lower() in home_lower
            a_match = away_lower in a_name or a_name in away_lower or \
                      away_team.get("abbreviation","").lower() in away_lower
            if not (h_match and a_match):
                continue

            for side in ("home", "away"):
                pitcher = teams.get(side, {}).get("probablePitcher")
                if not pitcher:
                    continue
                pid   = pitcher.get("id")
                pname = pitcher.get("fullName", pitcher.get("lastName", "Onbekend"))
                stats = get_pitcher_season_stats(pid) if pid else {}
                result[side] = {"name": pname, "player_id": pid, **stats}
            break
        if result["home"] or result["away"]:
            break

    # Kort cachen: werpers kunnen tot vlak voor de wedstrijd veranderen
    cache_set(cache_key, result, ttl_hours=2)
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
