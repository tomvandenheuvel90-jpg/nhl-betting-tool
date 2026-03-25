"""
Moneypuck lokale data — leest vooraf gedownloade seizoenssummary CSVs.
Seizoenen 2021–2025, regular + playoffs.
Geen netwerk nodig.

Publieke API:
  career_averages(player_id)  → dict met hist_*_avg sleutels
  playoff_averages(player_id) → zelfde structuur maar voor playoffs
"""

import csv
import math
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "moneypuck_data" / "raw"
SEASONS = list(range(2021, 2026))   # 2020 niet opnemen

# Module-level cache: key = "year/type" → list[dict]
_CACHE: dict = {}


# ─── Interne helpers ──────────────────────────────────────────────────────────

def _f(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _load_season(year: int, season_type: str) -> list:
    key = f"{year}/{season_type}"
    if key in _CACHE:
        return _CACHE[key]
    path = DATA_DIR / season_type / str(year) / "skaters.csv"
    if not path.exists():
        _CACHE[key] = []
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("situation") == "all"]
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
    # Hernoem sleutels naar playoff_* zodat ze naast hist_* bestaan
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
    # P(X <= k) = Σ e^(-lam) * lam^i / i!
    cdf = sum(
        math.exp(-lam) * (lam ** i) / math.factorial(i)
        for i in range(k + 1)
    )
    return round(max(0.0, min(1.0, 1.0 - cdf)), 4)
