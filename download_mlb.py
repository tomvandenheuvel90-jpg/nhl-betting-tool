#!/usr/bin/env python3
"""
MLB data downloader — MLB Stats API (gratis, geen key nodig)
Downloadt hitters, pitchers en wedstrijden voor seizoenen 2023-2025.

Gebruik:
  python3 download_mlb.py                    # standaard: 2025, 2024, 2023
  python3 download_mlb.py --seasons 2024 2025
"""

import csv
import json
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BASE_URL  = "https://statsapi.mlb.com/api/v1"
BASE_DIR  = Path(__file__).parent / "mlb_data"

REQUEST_DELAY = 0.5
MAX_RETRIES   = 3
RETRY_DELAY   = 3.0
LIMIT         = 1000    # spelers per request (ruim genoeg)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "application/json",
}

# Hitter-kolommen
HITTER_COLS = [
    "playerId", "fullName", "team", "position",
    "gamesPlayed", "avg", "hits", "homeRuns",
    "rbi", "runs", "stolenBases", "strikeOuts",
    "baseOnBalls", "obp", "slg", "ops",
]

# Pitcher-kolommen
PITCHER_COLS = [
    "playerId", "fullName", "team",
    "gamesPlayed", "era", "wins", "losses",
    "strikeOuts", "baseOnBalls",
    "inningsPitched", "whip", "saves",
]

# Game-kolommen
GAME_COLS = [
    "game_id", "date", "season",
    "home_team", "away_team",
    "home_score", "away_score", "status",
]


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _get(url: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"   ⚠ Fout (poging {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def load_meta() -> dict:
    p = BASE_DIR / "metadata.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"downloaded": [], "errors": [], "last_run": None}


def save_meta(meta: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    meta["last_run"] = datetime.now().isoformat()
    (BASE_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))


def _team_name(split: dict) -> str:
    return split.get("team", {}).get("name", "")


def _position(split: dict) -> str:
    return split.get("position", {}).get("abbreviation", "")


# ─── HITTERS ──────────────────────────────────────────────────────────────────

def download_hitters(year: int, dest: Path) -> int:
    url = f"{BASE_URL}/stats?stats=season&group=hitting&season={year}&limit={LIMIT}&sportId=1"
    data = _get(url)
    if not data:
        return 0

    splits = data.get("stats", [{}])[0].get("splits", [])

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HITTER_COLS + ["season"])
        writer.writeheader()
        for s in splits:
            stat   = s.get("stat", {})
            player = s.get("player", {})
            gp = int(stat.get("gamesPlayed", 0) or 0)
            if gp < 5:
                continue
            writer.writerow({
                "playerId":    player.get("id"),
                "fullName":    player.get("fullName"),
                "team":        _team_name(s),
                "position":    _position(s),
                "gamesPlayed": gp,
                "avg":         stat.get("avg"),
                "hits":        stat.get("hits"),
                "homeRuns":    stat.get("homeRuns"),
                "rbi":         stat.get("rbi"),
                "runs":        stat.get("runs"),
                "stolenBases": stat.get("stolenBases"),
                "strikeOuts":  stat.get("strikeOuts"),
                "baseOnBalls": stat.get("baseOnBalls"),
                "obp":         stat.get("obp"),
                "slg":         stat.get("slg"),
                "ops":         stat.get("ops"),
                "season":      year,
            })

    return sum(1 for _ in open(dest)) - 1


# ─── PITCHERS ─────────────────────────────────────────────────────────────────

def download_pitchers(year: int, dest: Path) -> int:
    url = f"{BASE_URL}/stats?stats=season&group=pitching&season={year}&limit={LIMIT}&sportId=1"
    data = _get(url)
    if not data:
        return 0

    splits = data.get("stats", [{}])[0].get("splits", [])

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PITCHER_COLS + ["season"])
        writer.writeheader()
        for s in splits:
            stat   = s.get("stat", {})
            player = s.get("player", {})
            gp = int(stat.get("gamesPlayed", 0) or 0)
            if gp < 2:
                continue
            writer.writerow({
                "playerId":       player.get("id"),
                "fullName":       player.get("fullName"),
                "team":           _team_name(s),
                "gamesPlayed":    gp,
                "era":            stat.get("era"),
                "wins":           stat.get("wins"),
                "losses":         stat.get("losses"),
                "strikeOuts":     stat.get("strikeOuts"),
                "baseOnBalls":    stat.get("baseOnBalls"),
                "inningsPitched": stat.get("inningsPitched"),
                "whip":           stat.get("whip"),
                "saves":          stat.get("saves"),
                "season":         year,
            })

    return sum(1 for _ in open(dest)) - 1


# ─── WEDSTRIJDEN ──────────────────────────────────────────────────────────────

def download_games(year: int, dest: Path) -> int:
    # Haal totaal aantal wedstrijden op om te pagineren
    url = f"{BASE_URL}/schedule?sportId=1&season={year}&gameType=R&limit=50"
    first = _get(url)
    if not first:
        return 0

    total = first.get("totalItems", 0)
    # Sla alles op in één grote request (max 2500 per seizoen)
    url_all = f"{BASE_URL}/schedule?sportId=1&season={year}&gameType=R&limit={max(total, 100)}&offset=0"
    data = _get(url_all)
    if not data:
        return 0

    games = []
    for date_entry in data.get("dates", []):
        date_str = date_entry.get("date", "")
        for game in date_entry.get("games", []):
            status_detail = game.get("status", {}).get("abstractGameState", "")
            home = game.get("teams", {}).get("home", {})
            away = game.get("teams", {}).get("away", {})
            games.append({
                "game_id":    game.get("gamePk"),
                "date":       date_str,
                "season":     year,
                "home_team":  home.get("team", {}).get("name", ""),
                "away_team":  away.get("team", {}).get("name", ""),
                "home_score": home.get("score", ""),
                "away_score": away.get("score", ""),
                "status":     status_detail,
            })

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GAME_COLS)
        writer.writeheader()
        writer.writerows(games)

    return len(games)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run(seasons):
    meta = load_meta()
    done = set(meta.get("downloaded", []))

    print(f"\n{'─'*55}")
    print(f"  MLB downloader — seizoenen: {seasons}")
    print(f"  Bron: MLB Stats API (geen key nodig)")
    print(f"{'─'*55}\n")

    for year in seasons:
        season_dir = BASE_DIR / str(year)
        season_dir.mkdir(parents=True, exist_ok=True)

        # Hitters
        key_h = f"{year}/hitters"
        dest_h = season_dir / "hitters.csv"
        if dest_h.exists() and key_h in done:
            print(f"  ✓ {year} MLB hitters (cache)")
        else:
            print(f"  ⬇ {year} MLB hitters...", end=" ", flush=True)
            n = download_hitters(year, dest_h)
            if n > 0:
                print(f"✓ ({n} spelers)")
                done.add(key_h)
            else:
                print("✗ mislukt")
            time.sleep(REQUEST_DELAY)

        # Pitchers
        key_p = f"{year}/pitchers"
        dest_p = season_dir / "pitchers.csv"
        if dest_p.exists() and key_p in done:
            print(f"  ✓ {year} MLB pitchers (cache)")
        else:
            print(f"  ⬇ {year} MLB pitchers...", end=" ", flush=True)
            n = download_pitchers(year, dest_p)
            if n > 0:
                print(f"✓ ({n} pitchers)")
                done.add(key_p)
            else:
                print("✗ mislukt")
            time.sleep(REQUEST_DELAY)

        # Wedstrijden
        key_g = f"{year}/games"
        dest_g = season_dir / "games.csv"
        if dest_g.exists() and key_g in done:
            print(f"  ✓ {year} MLB wedstrijden (cache)")
        else:
            print(f"  ⬇ {year} MLB wedstrijden...", end=" ", flush=True)
            n = download_games(year, dest_g)
            if n > 0:
                print(f"✓ ({n} wedstrijden)")
                done.add(key_g)
            else:
                print("✗ mislukt")
            time.sleep(REQUEST_DELAY)

    meta["downloaded"] = list(done)
    save_meta(meta)
    print(f"\n  Data opgeslagen in: {BASE_DIR.resolve()}\n")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--seasons", nargs="+", type=int, default=[2025, 2024, 2023])
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.seasons)
