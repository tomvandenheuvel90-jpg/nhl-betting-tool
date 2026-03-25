#!/usr/bin/env python3
"""
Moneypuck NHL data downloader
Downloads skaters/goalies/lines/teams CSV per seizoen en filtert op betting-relevante kolommen.

Gebruik:
  python download_moneypuck.py                    # standaard: 2020-2025, regular, skaters
  python download_moneypuck.py --seasons 2008 2025
  python download_moneypuck.py --types regular playoffs
  python download_moneypuck.py --files skaters goalies lines teams
"""

import os
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BASE_URL = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{year}/{type}/{file}.csv"
BASE_DIR = Path(__file__).parent / "moneypuck_data"

MAX_RETRIES = 3
RETRY_DELAY = 2.0      # seconden tussen retry-pogingen
REQUEST_DELAY = 0.5    # seconden tussen normale requests

# Kolommen die we bewaren na het filteren (exacte Moneypuck namen)
SKATER_COLUMNS = [
    # Identiteit
    "playerId", "name", "team", "position", "season", "situation", "games_played",
    # Primaire stats
    "I_F_goals",
    "I_F_primaryAssists",
    "I_F_secondaryAssists",
    "I_F_points",
    "I_F_shotsOnGoal",
    "I_F_blockedShotAttempts",
    "I_F_hits",
    "icetime",
    # Geavanceerde stats
    "I_F_xGoals",
    "I_F_highDangerShots",
    "I_F_highDangerGoals",
    "onIce_xGoalsPercentage",
    "gameScore",
]

# Minimale kolommen voor andere bestandstypen (pas later uit te breiden)
GOALIE_COLUMNS = [
    "playerId", "name", "team", "season", "situation", "games_played",
    "saves", "savePct", "goals_against", "xGoals_against",
    "highDangerSaves", "highDangerGoals_against", "icetime",
]

LINES_COLUMNS = [
    "team", "season", "situation", "games_played",
    "playerId1", "playerId2", "playerId3",
    "I_F_goals", "I_F_points", "I_F_xGoals", "onIce_xGoalsPercentage",
]

TEAMS_COLUMNS = [
    "team", "season", "situation", "games_played",
    "goalsFor", "goalsAgainst", "xGoalsFor", "xGoalsAgainst",
    "xGoalsPercentage", "shotsOnGoalFor", "shotsOnGoalAgainst",
    "highDangerShotsFor", "highDangerShotsAgainst",
    "penaltiesFor", "penaltiesAgainst",
]

COLUMNS_MAP = {
    "skaters": SKATER_COLUMNS,
    "goalies": GOALIE_COLUMNS,
    "lines":   LINES_COLUMNS,
    "teams":   TEAMS_COLUMNS,
}

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def setup_dirs(season: int, season_type: str) -> tuple[Path, Path]:
    raw = BASE_DIR / "raw" / season_type / str(season)
    filtered = BASE_DIR / "filtered" / season_type / str(season)
    raw.mkdir(parents=True, exist_ok=True)
    filtered.mkdir(parents=True, exist_ok=True)
    return raw, filtered


def load_metadata() -> dict:
    meta_path = BASE_DIR / "metadata.json"
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    return {"downloaded": [], "missing_404": [], "last_run": None}


def save_metadata(meta: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    meta["last_run"] = datetime.now().isoformat()
    with open(BASE_DIR / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2)


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes}B"
    elif num_bytes < 1024 ** 2:
        return f"{num_bytes/1024:.1f}KB"
    else:
        return f"{num_bytes/1024**2:.1f}MB"


def seconds_to_mmss(seconds) -> str:
    """Zet icetime (seconden) om naar MM:SS string."""
    try:
        s = int(float(seconds))
        return f"{s // 60}:{s % 60:02d}"
    except (ValueError, TypeError):
        return str(seconds)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://moneypuck.com/data.htm",
}


def download_file(url: str, dest: Path, meta: dict) -> tuple[bool, int]:
    """
    Download één bestand met retry-logica.
    Returns (success, bytes_downloaded)
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code in (404, 403):
                return False, 0
            resp.raise_for_status()
            content = resp.content
            dest.write_bytes(content)
            return True, len(content)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                print(f"   ⚠ Poging {attempt} mislukt ({e}), opnieuw over {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"   ✗ Definitief mislukt na {MAX_RETRIES} pogingen: {e}")
                return False, 0
    return False, 0


def filter_and_save(raw_path: Path, filtered_path: Path, file_type: str) -> int:
    """
    Lees raw CSV, bewaar gewenste kolommen, voeg I_F_assists toe (skaters),
    filter op situation='all', sla op in filtered_path.
    Returns aantal rijen.
    """
    try:
        import csv

        with open(raw_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)
            if not all_rows:
                return 0
            headers = reader.fieldnames or []

        desired = COLUMNS_MAP.get(file_type, [])
        # Bewaar alleen kolommen die ook daadwerkelijk aanwezig zijn
        keep = [c for c in desired if c in headers]

        # Filter op situation='all' (alleen voor skaters en goalies)
        if file_type in ("skaters", "goalies") and "situation" in headers:
            rows = [r for r in all_rows if r.get("situation") == "all"]
        else:
            rows = all_rows

        # Voeg I_F_assists toe (skaters)
        if file_type == "skaters":
            if "I_F_assists" not in keep:
                keep.append("I_F_assists")
            for row in rows:
                try:
                    pa = float(row.get("I_F_primaryAssists", 0) or 0)
                    sa = float(row.get("I_F_secondaryAssists", 0) or 0)
                    row["I_F_assists"] = str(int(pa + sa))
                except ValueError:
                    row["I_F_assists"] = ""

        # Converteer icetime van seconden naar MM:SS
        if "icetime" in keep:
            for row in rows:
                row["icetime"] = seconds_to_mmss(row.get("icetime", ""))

        output_name = raw_path.stem + ("_filtered" if file_type == "skaters" else "") + ".csv"
        out_path = filtered_path / output_name

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keep, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        return len(rows)

    except Exception as e:
        print(f"   ⚠ Filter mislukt: {e}")
        return 0


# ─── CORE DOWNLOAD LOOP ───────────────────────────────────────────────────────

def run_download(
    seasons: list[int],
    season_types: list[str],
    file_types: list[str],
):
    meta = load_metadata()
    already_downloaded = set(meta.get("downloaded", []))
    already_missing = set(meta.get("missing_404", []))

    total_files = len(seasons) * len(season_types) * len(file_types)
    done = 0
    skipped = 0
    failed = 0

    print(f"\n{'─'*60}")
    print(f"  Moneypuck downloader — {total_files} bestanden te verwerken")
    print(f"  Seizoenen : {min(seasons)}–{max(seasons)}")
    print(f"  Types     : {', '.join(season_types)}")
    print(f"  Bestanden : {', '.join(file_types)}")
    print(f"{'─'*60}\n")

    for season in seasons:
        for stype in season_types:
            for ftype in file_types:
                key = f"{season}/{stype}/{ftype}"
                url = BASE_URL.format(year=season, type=stype, file=ftype)
                raw_dir, filtered_dir = setup_dirs(season, stype)
                dest = raw_dir / f"{ftype}.csv"

                label = f"{season}/{stype}/{ftype}.csv"

                # Skip als al eerder als 404 gelogd
                if key in already_missing:
                    print(f"  ↷ {label} (eerder 404, overgeslagen)")
                    skipped += 1
                    continue

                # Skip als bestand al bestaat
                if dest.exists() and key in already_downloaded:
                    size = format_size(dest.stat().st_size)
                    print(f"  ✓ {label} (cache, {size})")
                    skipped += 1
                    continue

                # Download
                print(f"  ⬇ {label}...", end=" ", flush=True)
                success, num_bytes = download_file(url, dest, meta)

                if not success:
                    print(f"geblokkeerd/niet beschikbaar")
                    already_missing.add(key)
                    meta.setdefault("missing_404", [])
                    if key not in meta["missing_404"]:
                        meta["missing_404"].append(key)
                    failed += 1
                    continue

                size_str = format_size(num_bytes)

                # Direct filteren
                n_rows = filter_and_save(dest, filtered_dir, ftype)
                row_label = f"{n_rows} rijen" if ftype != "skaters" else f"{n_rows} spelers"
                print(f"✓ ({size_str}) | gefilterd: {row_label}")

                already_downloaded.add(key)
                meta.setdefault("downloaded", [])
                if key not in meta["downloaded"]:
                    meta["downloaded"].append(key)

                done += 1
                time.sleep(REQUEST_DELAY)

    # Sla metadata op
    save_metadata(meta)

    print(f"\n{'─'*60}")
    print(f"  Klaar. Nieuw: {done} | Cache: {skipped} | Mislukt/404: {failed}")
    print(f"  Data opgeslagen in: {BASE_DIR.resolve()}")
    print(f"{'─'*60}\n")

    # Vraag of gebruiker wil uitbreiden
    if seasons == list(range(2020, 2026)) and season_types == ["regular"] and file_types == ["skaters"]:
        print("Wil je uitbreiden? Kies wat je wilt toevoegen:\n")
        print("  1. Eerdere seizoenen (2008–2019)")
        print("  2. Playoffs-data")
        print("  3. Goalies, lines en teams")
        print("  4. Alles bovenstaande")
        print()
        print("Voer uit met vlaggen, bijv:")
        print("  python download_moneypuck.py --seasons 2008 2025")
        print("  python download_moneypuck.py --types regular playoffs")
        print("  python download_moneypuck.py --files skaters goalies lines teams")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Download Moneypuck NHL data")
    parser.add_argument(
        "--seasons", nargs=2, type=int, metavar=("START", "END"),
        default=[2020, 2025],
        help="Seizoensbereik (eindjaar). Default: 2020 2025"
    )
    parser.add_argument(
        "--types", nargs="+", choices=["regular", "playoffs"],
        default=["regular"],
        help="regular en/of playoffs. Default: regular"
    )
    parser.add_argument(
        "--files", nargs="+", choices=["skaters", "goalies", "lines", "teams"],
        default=["skaters"],
        help="Bestanden om te downloaden. Default: skaters"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    seasons = list(range(args.seasons[0], args.seasons[1] + 1))
    run_download(
        seasons=seasons,
        season_types=args.types,
        file_types=args.files,
    )
