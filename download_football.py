#!/usr/bin/env python3
"""
Football data downloader
- football-data.org  : wedstrijden + standen (10 req/min, geen dagelijkse limiet)
- api-sports.io      : spelersstatistieken  (100 req/DAG — zuinig mee zijn!)

Gebruik:
  python3 download_football.py                          # EPL 2025 test
  python3 download_football.py --leagues epl            # alleen EPL
  python3 download_football.py --leagues all            # alle 7 competities
  python3 download_football.py --seasons 2024 2025      # meerdere seizoenen
  python3 download_football.py --skip-players           # geen api-sports (spaart quota)
"""

import csv
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime

# ─── API KEYS ─────────────────────────────────────────────────────────────────

FD_TOKEN    = "913f579478284ffba8b9e22ddd5634c6"   # football-data.org
AS_KEY      = "db7c80f49b2efaacba4a28c2142abe3b"   # api-sports.io

# ─── CONFIG ───────────────────────────────────────────────────────────────────

FD_BASE  = "https://api.football-data.org/v4"
AS_BASE  = "https://v3.football.api-sports.io"
BASE_DIR = Path(__file__).parent / "football_data"

# Rate limiting
FD_DELAY = 7.0    # football-data.org: 10 req/min → 6s, iets ruimer
AS_DELAY = 2.5    # api-sports.io: 30 req/min
MAX_RETRIES = 3
RETRY_DELAY = 15.0

# Competities: interne naam → (football-data id, api-sports id)
COMPETITIONS = {
    "epl":          (2021, 39,  "Premier League"),
    "championship": (2016, 40,  "Championship"),
    "la_liga":      (2014, 140, "La Liga"),
    "bundesliga":   (2002, 78,  "Bundesliga"),
    "serie_a":      (2019, 135, "Serie A"),
    "ligue_1":      (2015, 61,  "Ligue 1"),
    "eredivisie":   (2003, 88,  "Eredivisie"),
}

# Kolommen wedstrijden
MATCH_COLS = [
    "match_id", "date", "season", "competition",
    "home_team", "away_team",
    "home_score_ft", "away_score_ft",
    "home_score_ht", "away_score_ht",
    "status",
]

# Kolommen standen
STANDING_COLS = [
    "season", "competition", "position", "team",
    "played", "won", "draw", "lost",
    "goals_for", "goals_against", "goal_diff", "points",
]

# Kolommen spelers (api-sports.io)
PLAYER_COLS = [
    "player_id", "name", "team", "position", "season", "competition",
    "appearances", "minutes",
    "goals", "assists",
    "shots_total", "shots_on",
    "yellow_cards", "red_cards",
    "tackles_total",
    "duels_total", "duels_won",
    "passes_total", "passes_accuracy",
]


# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

def _fd_get(path: str, params: dict = None):
    """football-data.org GET."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{FD_BASE}{path}",
                headers={"X-Auth-Token": FD_TOKEN},
                params=params or {},
                timeout=30,
            )
            if resp.status_code == 429:
                print(f"   ⚠ Rate limit FD (429), wachten {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue
            if resp.status_code == 404:
                return None   # seizoen niet beschikbaar
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"   ⚠ FD fout (poging {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def _as_get(path: str, params: dict = None):
    """api-sports.io GET."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{AS_BASE}{path}",
                headers={"x-apisports-key": AS_KEY},
                params=params or {},
                timeout=30,
            )
            if resp.status_code == 429:
                print(f"   ⚠ Rate limit AS (429), wachten {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue
            resp.raise_for_status()
            data = resp.json()
            # Controleer dagelijkse quota
            remaining = data.get("results", None)
            return data
        except Exception as e:
            print(f"   ⚠ AS fout (poging {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


# ─── METADATA ─────────────────────────────────────────────────────────────────

def load_meta() -> dict:
    p = BASE_DIR / "metadata.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"downloaded": [], "errors": [], "as_requests_today": 0,
            "as_requests_date": None, "last_run": None}


def save_meta(meta: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    meta["last_run"] = datetime.now().isoformat()
    (BASE_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))


def _as_quota_ok(meta: dict, needed: int = 1) -> bool:
    """Check of api-sports.io dagquota niet overschreden wordt."""
    today = datetime.now().date().isoformat()
    if meta.get("as_requests_date") != today:
        meta["as_requests_today"] = 0
        meta["as_requests_date"] = today
    used = meta.get("as_requests_today", 0)
    remaining = 100 - used
    if remaining < needed:
        print(f"   ⚠ api-sports.io dagquota bijna op ({used}/100 gebruikt). Spelers overgeslagen.")
        return False
    return True


def _as_use(meta: dict, n: int = 1):
    meta["as_requests_today"] = meta.get("as_requests_today", 0) + n


# ─── WEDSTRIJDEN (football-data.org) ──────────────────────────────────────────

def download_matches(fd_id: int, comp_name: str, season: int, dest: Path) -> int:
    # football-data.org gebruikt startjaar: 2024-25 → season=2024
    fd_season = season - 1 if season >= 2024 else season
    data = _fd_get(f"/competitions/{fd_id}/matches", {"season": fd_season})
    if not data:
        return 0

    matches = data.get("matches", [])
    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_COLS)
        writer.writeheader()
        for m in matches:
            score  = m.get("score", {})
            ft     = score.get("fullTime", {})
            ht     = score.get("halfTime", {})
            writer.writerow({
                "match_id":       m.get("id"),
                "date":           (m.get("utcDate", "") or "")[:10],
                "season":         season,
                "competition":    comp_name,
                "home_team":      m.get("homeTeam", {}).get("name", ""),
                "away_team":      m.get("awayTeam", {}).get("name", ""),
                "home_score_ft":  ft.get("home", ""),
                "away_score_ft":  ft.get("away", ""),
                "home_score_ht":  ht.get("home", ""),
                "away_score_ht":  ht.get("away", ""),
                "status":         m.get("status", ""),
            })
    return len(matches)


# ─── STANDEN (football-data.org) ──────────────────────────────────────────────

def download_standings(fd_id: int, comp_name: str, season: int, dest: Path) -> int:
    fd_season = season - 1 if season >= 2024 else season
    data = _fd_get(f"/competitions/{fd_id}/standings", {"season": fd_season})
    if not data:
        return 0

    standings_list = data.get("standings", [])
    # Neem de totaalstand (TOTAL), niet thuis/uit
    total = next((s for s in standings_list if s.get("type") == "TOTAL"), None)
    if not total:
        total = standings_list[0] if standings_list else None
    if not total:
        return 0

    table = total.get("table", [])
    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STANDING_COLS)
        writer.writeheader()
        for row in table:
            team = row.get("team", {})
            writer.writerow({
                "season":       season,
                "competition":  comp_name,
                "position":     row.get("position"),
                "team":         team.get("name", ""),
                "played":       row.get("playedGames"),
                "won":          row.get("won"),
                "draw":         row.get("draw"),
                "lost":         row.get("lost"),
                "goals_for":    row.get("goalsScored"),
                "goals_against": row.get("goalsConceded"),
                "goal_diff":    row.get("goalDifference"),
                "points":       row.get("points"),
            })
    return len(table)


# ─── SPELERS (api-sports.io) ──────────────────────────────────────────────────

def download_players(as_id: int, comp_name: str, season: int, dest: Path,
                     meta: dict) -> int:
    """Download spelersstatistieken. api-sports.io: 100 req/dag!"""
    # Schat benodigde requests: ~25 pages van 20 spelers = 25 requests
    if not _as_quota_ok(meta, needed=5):  # check minimaal 5 over
        return -1  # overgeslagen (quota)

    # Seizoensformat api-sports.io: startjaar (2024 voor 2024-25)
    as_season = season - 1 if season >= 2024 else season

    rows = []
    page = 1
    while True:
        if not _as_quota_ok(meta, needed=1):
            break
        data = _as_get("/players", {"league": as_id, "season": as_season, "page": page})
        _as_use(meta, 1)
        time.sleep(AS_DELAY)

        if not data:
            break
        results = data.get("response", [])
        if not results:
            break

        for item in results:
            p    = item.get("player", {})
            stats = (item.get("statistics") or [{}])[0]
            team  = stats.get("team", {})
            games = stats.get("games", {})
            shots = stats.get("shots", {})
            goals = stats.get("goals", {})
            cards = stats.get("cards", {})
            tack  = stats.get("tackles", {})
            duel  = stats.get("duels", {})
            pas   = stats.get("passes", {})
            rows.append({
                "player_id":       p.get("id"),
                "name":            p.get("name"),
                "team":            team.get("name", ""),
                "position":        games.get("position", ""),
                "season":          season,
                "competition":     comp_name,
                "appearances":     games.get("appearences", 0),
                "minutes":         games.get("minutes", 0),
                "goals":           goals.get("total", 0),
                "assists":         goals.get("assists", 0),
                "shots_total":     shots.get("total", 0),
                "shots_on":        shots.get("on", 0),
                "yellow_cards":    cards.get("yellow", 0),
                "red_cards":       cards.get("red", 0),
                "tackles_total":   tack.get("total", 0),
                "duels_total":     duel.get("total", 0),
                "duels_won":       duel.get("won", 0),
                "passes_total":    pas.get("total", 0),
                "passes_accuracy": pas.get("accuracy", 0),
            })

        paging = data.get("paging", {})
        if page >= paging.get("total", 1):
            break
        page += 1

    if not rows:
        return 0

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PLAYER_COLS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run(league_keys: list, seasons: list, skip_players: bool = False):
    meta = load_meta()
    done = set(meta.get("downloaded", []))

    comp_list = [(k, COMPETITIONS[k]) for k in league_keys if k in COMPETITIONS]

    total = len(comp_list) * len(seasons) * (2 if skip_players else 3)
    print(f"\n{'─'*60}")
    print(f"  Football downloader — {len(comp_list)} competities × {len(seasons)} seizoenen")
    print(f"  api-sports.io quota vandaag: {meta.get('as_requests_today', 0)}/100 gebruikt")
    print(f"{'─'*60}\n")

    for season in seasons:
        season_dir = BASE_DIR / str(season)
        (season_dir / "matches").mkdir(parents=True, exist_ok=True)
        (season_dir / "standings").mkdir(parents=True, exist_ok=True)
        (season_dir / "players").mkdir(parents=True, exist_ok=True)

        for key, (fd_id, as_id, label) in comp_list:

            # ── Wedstrijden ──
            k_m = f"{season}/{key}/matches"
            dest_m = season_dir / "matches" / f"{key}.csv"
            if dest_m.exists() and k_m in done:
                print(f"  ✓ {season} {label} wedstrijden (cache)")
            else:
                print(f"  ⬇ {season} {label} wedstrijden...", end=" ", flush=True)
                n = download_matches(fd_id, label, season, dest_m)
                if n > 0:
                    print(f"✓ ({n} wedstrijden)")
                    done.add(k_m)
                elif n == 0:
                    print("— geen data (seizoen nog niet begonnen?)")
                else:
                    print("✗ mislukt")
                time.sleep(FD_DELAY)

            # ── Standen ──
            k_s = f"{season}/{key}/standings"
            dest_s = season_dir / "standings" / f"{key}.csv"
            if dest_s.exists() and k_s in done:
                print(f"  ✓ {season} {label} stand (cache)")
            else:
                print(f"  ⬇ {season} {label} stand...", end=" ", flush=True)
                n = download_standings(fd_id, label, season, dest_s)
                if n > 0:
                    print(f"✓ ({n} teams)")
                    done.add(k_s)
                else:
                    print("✗ mislukt / geen data")
                time.sleep(FD_DELAY)

            # ── Spelers (api-sports.io — 100/dag!) ──
            if not skip_players:
                k_p = f"{season}/{key}/players"
                dest_p = season_dir / "players" / f"{key}.csv"
                if dest_p.exists() and k_p in done:
                    print(f"  ✓ {season} {label} spelers (cache)")
                else:
                    quota_str = f"[quota: {meta.get('as_requests_today',0)}/100]"
                    print(f"  ⬇ {season} {label} spelers {quota_str}...", end=" ", flush=True)
                    n = download_players(as_id, label, season, dest_p, meta)
                    if n == -1:
                        print("overgeslagen (quota)")
                    elif n > 0:
                        print(f"✓ ({n} spelers)")
                        done.add(k_p)
                    else:
                        print("✗ mislukt / geen data")
                    save_meta(meta)  # sla quota tussentijds op

    meta["downloaded"] = list(done)
    save_meta(meta)
    print(f"\n  Data opgeslagen in: {BASE_DIR.resolve()}")
    print(f"  api-sports.io requests vandaag: {meta.get('as_requests_today', 0)}/100\n")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--leagues", nargs="+",
        default=["epl"],
        choices=list(COMPETITIONS.keys()) + ["all"],
        help="Competities (default: epl). Gebruik 'all' voor alle 7.",
    )
    p.add_argument("--seasons", nargs="+", type=int, default=[2025])
    p.add_argument("--skip-players", action="store_true",
                   help="Sla api-sports.io spelers over (spaart quota)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    leagues = list(COMPETITIONS.keys()) if "all" in args.leagues else args.leagues
    run(leagues, args.seasons, skip_players=args.skip_players)
