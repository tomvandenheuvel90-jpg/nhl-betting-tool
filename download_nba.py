#!/usr/bin/env python3
"""
NBA data downloader — nba_api package (wraps NBA.com Stats API)
Downloadt speler-seizoensgemiddelden en wedstrijden voor seizoenen 2023-2025.

Gebruik:
  python3 download_nba.py                    # standaard: 2025, 2024, 2023
  python3 download_nba.py --seasons 2024 2025
"""

import csv
import json
import time
import argparse
from pathlib import Path
from datetime import datetime

from nba_api.stats.endpoints import leaguedashplayerstats, leaguegamelog

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent / "nba_data"
REQUEST_DELAY = 2.0   # extra buffer bovenop ingebouwde nba_api rate limiting

# Kolommen die we bewaren
PLAYER_KEEP = [
    "PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION",
    "GP", "MIN",
    "PTS", "AST", "REB", "STL", "BLK",
    "FGM", "FGA", "FG_PCT",
    "FG3M", "FG3A", "FG3_PCT",
    "FTM", "FTA", "FT_PCT",
]

PLAYER_RENAME = {
    "PLAYER_ID":         "player_id",
    "PLAYER_NAME":       "name",
    "TEAM_ABBREVIATION": "team",
    "GP":                "games_played",
    "MIN":               "min",
    "PTS":               "pts",
    "AST":               "ast",
    "REB":               "reb",
    "STL":               "stl",
    "BLK":               "blk",
    "FGM":               "fgm",
    "FGA":               "fga",
    "FG_PCT":            "fg_pct",
    "FG3M":              "fg3m",
    "FG3A":              "fg3a",
    "FG3_PCT":           "fg3_pct",
    "FTM":               "ftm",
    "FTA":               "fta",
    "FT_PCT":            "ft_pct",
}

GAME_COLS = [
    "game_id", "date", "season",
    "home_team", "away_team",
    "home_score", "away_score", "status",
]


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _api_season(user_year: int) -> str:
    """Eindjaar → NBA.com seizoensformat. 2025 → '2024-25'."""
    start = user_year - 1
    return f"{start}-{str(user_year)[2:]}"


def load_meta() -> dict:
    p = BASE_DIR / "metadata.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"downloaded": [], "errors": [], "last_run": None}


def save_meta(meta: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    meta["last_run"] = datetime.now().isoformat()
    (BASE_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))


# ─── SPELERS ──────────────────────────────────────────────────────────────────

def download_players(user_year: int, dest: Path) -> int:
    season = _api_season(user_year)
    try:
        result = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season,
            season_type_all_star="Regular Season",
            per_mode_detailed="PerGame",
            timeout=120,
        )
        df = result.get_data_frames()[0]
    except Exception as e:
        print(f"   ⚠ Fout: {e}")
        return 0

    keep = [c for c in PLAYER_KEEP if c in df.columns]
    df = df[keep].copy()
    df = df.rename(columns={c: PLAYER_RENAME.get(c, c.lower()) for c in keep})
    df["season"] = user_year
    df = df[df["games_played"] >= 5]

    df.to_csv(dest, index=False)
    return len(df)


# ─── WEDSTRIJDEN ──────────────────────────────────────────────────────────────

def download_games(user_year: int, dest: Path) -> int:
    season = _api_season(user_year)
    try:
        result = leaguegamelog.LeagueGameLog(
            season=season,
            season_type_all_star="Regular Season",
            player_or_team_abbreviation="T",
            direction="ASC",
            timeout=120,
        )
        df = result.get_data_frames()[0]
    except Exception as e:
        print(f"   ⚠ Fout: {e}")
        return 0

    # Combineer twee rijen per game (home + away) naar één rij
    games_raw = {}
    for _, row in df.iterrows():
        gid     = str(row["GAME_ID"])
        matchup = str(row.get("MATCHUP", ""))
        team    = row.get("TEAM_ABBREVIATION", "")
        pts     = row.get("PTS", "")
        date    = row.get("GAME_DATE", "")

        if gid not in games_raw:
            games_raw[gid] = {"game_id": gid, "date": date,
                               "season": user_year, "status": "Final"}

        if " vs. " in matchup:
            games_raw[gid]["home_team"]  = team
            games_raw[gid]["home_score"] = pts
        elif " @ " in matchup:
            games_raw[gid]["away_team"]  = team
            games_raw[gid]["away_score"] = pts

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GAME_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(games_raw.values())

    return len(games_raw)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run(seasons):
    meta = load_meta()
    done = set(meta.get("downloaded", []))

    print(f"\n{'─'*55}")
    print(f"  NBA downloader — seizoenen: {seasons}")
    print(f"  Bron: nba_api (NBA.com Stats API)")
    print(f"{'─'*55}\n")

    for year in seasons:
        season_dir = BASE_DIR / str(year)
        season_dir.mkdir(parents=True, exist_ok=True)

        # Spelers
        key_p  = f"{year}/players"
        dest_p = season_dir / "players.csv"
        if dest_p.exists() and key_p in done:
            print(f"  ✓ {year} NBA spelers (cache)")
        else:
            print(f"  ⬇ {year} NBA spelers ({_api_season(year)})...", end=" ", flush=True)
            n = download_players(year, dest_p)
            if n > 0:
                print(f"✓ ({n} spelers)")
                done.add(key_p)
            else:
                print("✗ mislukt")
            time.sleep(REQUEST_DELAY)

        # Wedstrijden
        key_g  = f"{year}/games"
        dest_g = season_dir / "games.csv"
        if dest_g.exists() and key_g in done:
            print(f"  ✓ {year} NBA wedstrijden (cache)")
        else:
            print(f"  ⬇ {year} NBA wedstrijden...", end=" ", flush=True)
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
