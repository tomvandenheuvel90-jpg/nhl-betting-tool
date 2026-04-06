"""
Moneypuck lokale data — leest vooraf gedownloade seizoenssummary CSVs.
Seizoenen 2021–2025, regular + playoffs.

Fallback volgorde per seizoen:
  1. raw/{type}/{jaar}/skaters.csv           (lokaal, volledig)
  2. filtered/{type}/{jaar}/skaters_filtered.csv  (lokaal, gefilterd)
  3. /tmp/moneypuck/{type}/{jaar}/skaters_filtered.csv  (GDrive cache)
  4. Google Drive download via file ID uit gdrive_file_ids.json

Publieke API:
  career_averages(player_id)  → dict met hist_*_avg sleutels
  playoff_averages(player_id) → zelfde structuur maar voor playoffs
  set_gdrive_credentials(creds_dict)  → activeer GDrive fallback
"""

import csv
import io
import json
import math
from pathlib import Path

BASE_DIR     = Path(__file__).parent.parent
RAW_DIR      = BASE_DIR / "moneypuck_data" / "raw"
FILTERED_DIR = BASE_DIR / "moneypuck_data" / "filtered"
FLAT_DIR     = BASE_DIR / "data" / "moneypuck"
FILE_IDS     = BASE_DIR / "gdrive_file_ids.json"
TMP_DIR      = Path("/tmp") / "moneypuck"


def _current_mp_year() -> int:
    """Eindjaar van het lopende NHL-seizoen (okt start → jun eind)."""
    import datetime
    today = datetime.date.today()
    return today.year + 1 if today.month >= 7 else today.year


# Seizoenen om te doorzoeken: 2021 t/m huidig jaar inclusief
SEASONS = list(range(2021, _current_mp_year() + 1))

# Module-level cache: key = "year/type" → list[dict]
_CACHE: dict = {}

# Google Drive credentials (ingesteld via set_gdrive_credentials)
_gdrive_creds: dict = {}


# ─── GDrive credentials instellen (vanuit streamlit_app.py) ──────────────────

def set_gdrive_credentials(creds_dict: dict):
    """Stel Google Drive service account credentials in voor cloud fallback."""
    global _gdrive_creds
    _gdrive_creds = creds_dict


# ─── Interne helpers ──────────────────────────────────────────────────────────

def _f(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _read_csv_rows(path: Path) -> list:
    """Lees CSV en filter op situation == 'all'."""
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("situation") == "all"]


def _download_from_gdrive(rel_key: str) -> list:
    """
    Download skaters_filtered.csv van Google Drive als lokale bestanden ontbreken.
    rel_key bijv. "regular/2025/skaters_filtered.csv"
    Resultaat wordt gecacht in /tmp/moneypuck/.
    """
    if not _gdrive_creds:
        return []

    if not FILE_IDS.exists():
        return []

    try:
        file_ids = json.loads(FILE_IDS.read_text())
    except Exception:
        return []

    file_id = file_ids.get(rel_key)
    if not file_id:
        return []

    # Cache pad in /tmp
    tmp_path = TMP_DIR / rel_key
    if tmp_path.exists():
        try:
            return _read_csv_rows(tmp_path)
        except Exception:
            pass

    # Download via Google Drive API
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        from google.oauth2 import service_account as _sa

        creds = _sa.Credentials.from_service_account_info(
            _gdrive_creds,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        request = service.files().get_media(fileId=file_id)

        content = io.BytesIO()
        downloader = MediaIoBaseDownload(content, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        # Sla op in /tmp voor volgende keer (binnen dezelfde sessie)
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(content.getvalue())

        text = content.getvalue().decode("utf-8")
        rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("situation") == "all"]
        return rows

    except Exception as e:
        print(f"  ⚠️  GDrive download mislukt ({rel_key}): {e}")
        return []


def _load_season(year: int, season_type: str) -> list:
    key = f"{year}/{season_type}"
    if key in _CACHE:
        return _CACHE[key]

    # 1. Flat data/moneypuck/skaters_{year}.csv
    flat_path = FLAT_DIR / f"skaters_{year}.csv"
    if flat_path.exists():
        rows = _read_csv_rows(flat_path)
        _CACHE[key] = rows
        return rows

    # 2. Lokaal raw
    raw_path = RAW_DIR / season_type / str(year) / "skaters.csv"
    if raw_path.exists():
        rows = _read_csv_rows(raw_path)
        _CACHE[key] = rows
        return rows

    # 3. Lokaal filtered
    filtered_path = FILTERED_DIR / season_type / str(year) / "skaters_filtered.csv"
    if filtered_path.exists():
        rows = _read_csv_rows(filtered_path)
        _CACHE[key] = rows
        return rows

    # 4. /tmp cache (eerder gedownload in deze sessie)
    tmp_path = TMP_DIR / season_type / str(year) / "skaters_filtered.csv"
    if tmp_path.exists():
        try:
            rows = _read_csv_rows(tmp_path)
            _CACHE[key] = rows
            return rows
        except Exception:
            pass

    # 5. Google Drive download
    rel_key = f"{season_type}/{year}/skaters_filtered.csv"
    rows = _download_from_gdrive(rel_key)
    _CACHE[key] = rows
    return rows


def _player_seasons(player_id, season_type: str) -> list:
    """Geeft lijst van seizoensrijen voor één speler (één rij per seizoen)."""
    pid = str(player_id)
    result = []
    for year in SEASONS:
        for row in _load_season(year, season_type):
            if row.get("playerId") == pid:
                result.append(row)
                break
    return result


def _compute_averages(seasons: list) -> dict:
    """Bereken gewogen gemiddelden per game over alle seizoenen."""
    if not seasons:
        return {}

    total_gp = sum(_f(r.get("games_played", 0)) for r in seasons)
    if total_gp == 0:
        return {}

    def wavg(col):
        return round(sum(_f(r.get(col, 0)) for r in seasons) / total_gp, 3)

    pa = sum(_f(r.get("I_F_primaryAssists", 0)) for r in seasons)
    sa = sum(_f(r.get("I_F_secondaryAssists", 0)) for r in seasons)

    return {
        "hist_shots_avg":   wavg("I_F_shotsOnGoal"),
        "hist_goals_avg":   wavg("I_F_goals"),
        "hist_assists_avg": round((pa + sa) / total_gp, 3),
        "hist_points_avg":  wavg("I_F_points"),
        "hist_hits_avg":    wavg("I_F_hits"),
        "hist_blocks_avg":  wavg("I_F_blockedShotAttempts"),
        "hist_xgoals_avg":  wavg("I_F_xGoals"),
        "hist_seasons":     len(seasons),
        "hist_gp_total":    int(total_gp),
    }


# ─── Publieke API ─────────────────────────────────────────────────────────────

def career_averages(player_id) -> dict:
    """Regular season carrière-gemiddelden per game (2021–2025)."""
    return _compute_averages(_player_seasons(player_id, "regular"))


def playoff_averages(player_id) -> dict:
    """Playoffs carrière-gemiddelden per game (2021–2024)."""
    data = _compute_averages(_player_seasons(player_id, "playoffs"))
    return {k.replace("hist_", "playoff_"): v for k, v in data.items()}


# ─── Poisson hit rate (gebruikt door scorer.py) ───────────────────────────────

def poisson_hit_rate(lam: float, threshold: float, gte: bool = False) -> float:
    """
    Schat P(X > threshold) of P(X >= threshold) via Poisson(lambda=lam).

    Over 2.5 shots  → gte=False, threshold=2.5 → P(X >= 3) = 1 – CDF(2)
    Anytime scorer  → gte=True,  threshold=1.0 → P(X >= 1) = 1 – CDF(0)
    """
    if lam <= 0:
        return 0.5

    k = int(threshold) if not gte else int(threshold) - 1
    cdf = sum(
        math.exp(-lam) * (lam ** i) / math.factorial(i)
        for i in range(k + 1)
    )
    return round(max(0.0, min(1.0, 1.0 - cdf)), 4)
