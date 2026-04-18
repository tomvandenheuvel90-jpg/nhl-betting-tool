"""
Composite scoring model voor sportsweddenschappen.

Score = gewogen combinatie van:
  Linemate hit rate (recente form via screenshot)   35%
  Seizoens hit rate (berekend uit raw game values)  35%
  Tegenstander factor                               20%
  Sample size betrouwbaarheid                       10%

Raw values aanpak: elke sport slaat ruwe per-game waarden op zodat we voor
elk drempelwaardeniveau dynamisch de hit rate kunnen berekenen.
"""

import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "sports"))
from moneypuck_local import poisson_hit_rate as _poisson_hr


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sample_reliability(n: int) -> float:
    """Betrouwbaarheid: 0 bij 0 games, max 1.0 bij ≥20 games."""
    return min(n / 20.0, 1.0)


def _hit_rate(values: list, threshold: float, gte: bool = False) -> float:
    """Hit rate voor lijst van waarden boven (of >=) een drempelwaarde."""
    if not values:
        return 0.5  # neutraal bij geen data
    if gte:
        return sum(1 for v in values if v >= threshold) / len(values)
    return sum(1 for v in values if v > threshold) / len(values)


def _extract_line(bet_type: str) -> float:
    """Haal de numerieke lijn uit bet_type, bijv. 'Over 2.5 Shots' → 2.5."""
    m = re.search(r"over\s+([\d.]+)", bet_type.lower())
    return float(m.group(1)) if m else 0.5


# ─── Bet type → raw values mapping ───────────────────────────────────────────

def _get_raw_and_line(bet_type: str, player_stats: dict) -> tuple[list, float, bool]:
    """
    Geeft (raw_values, threshold, use_gte) voor het gegeven bet-type.
    use_gte=True voor "anytime scorer" (≥1), anders gebruik > threshold.
    Ondersteunt NBA combo-stats: PRA, RA, PA, PR (gesommeerde per-game waarden).
    """
    bt   = bet_type.lower()
    line = _extract_line(bt)

    # ── NBA combo stats: PRA / PA / RA / PR ──────────────────────────────────
    # Herken via "+" separator of combinaties van bekende afkortingen
    _has_pts = "pts" in bt or "point" in bt
    _has_reb = "reb" in bt or "rebound" in bt
    _has_ast = "ast" in bt or "assist" in bt
    _is_combo = ("+" in bt) or (_has_pts and _has_reb) or (_has_pts and _has_ast) or (_has_reb and _has_ast)

    if _is_combo:
        raw_pts = player_stats.get("raw_pts", [])
        raw_reb = player_stats.get("raw_reb", [])
        raw_ast = player_stats.get("raw_ast", [])
        # Gebruik het kortste beschikbare subset, sommeer per game
        _games = max(len(raw_pts), len(raw_reb), len(raw_ast))
        if _games > 0:
            def _v(lst, i): return lst[i] if i < len(lst) else 0.0
            combo_raw = [
                (_v(raw_pts, i) if _has_pts else 0.0)
                + (_v(raw_reb, i) if _has_reb else 0.0)
                + (_v(raw_ast, i) if _has_ast else 0.0)
                for i in range(_games)
            ]
            return combo_raw, line, False
        # Geen raw data → fallback op lege lijst (scorer gebruikt dan hist_lam)
        return [], line, False

    # ── NHL ──
    if ("shot" in bt or "sog" in bt) and "block" not in bt and "attempt" not in bt:
        return player_stats.get("raw_shots", []), line, False

    if "block" in bt and "shot" in bt:
        return player_stats.get("raw_blocks", []), line, False

    if "anytime" in bt and ("goal" in bt or "scorer" in bt):
        return player_stats.get("raw_goals", []), 1.0, True

    if "goal" in bt:
        return player_stats.get("raw_goals", []), line, False

    if "assist" in bt:
        # NHL: raw_assists, NBA: niet van toepassing (ast), maar NHL heeft prioriteit
        raw = player_stats.get("raw_assists") or player_stats.get("raw_ast", [])
        return raw, line, False

    if "point" in bt and "three" not in bt and "3-point" not in bt:
        raw = player_stats.get("raw_points") or player_stats.get("raw_pts", [])
        return raw, line, False

    if "hit" in bt and "base" not in bt:
        # NHL hits (raw_hits) of MLB hits (raw_mlb_hits)
        raw = player_stats.get("raw_hits") or player_stats.get("raw_mlb_hits", [])
        return raw, line, False

    # ── NBA ──
    if "rebound" in bt or " reb" in bt:
        return player_stats.get("raw_reb", []), line, False

    if "three" in bt or "3-point" in bt or "3pt" in bt or "3pm" in bt:
        return player_stats.get("raw_threes", []), line, False

    if "steal" in bt:
        return player_stats.get("raw_stl", []), line, False

    if "block" in bt:
        # NBA blocks (raw_blk)
        raw = player_stats.get("raw_blk") or player_stats.get("raw_blocks", [])
        return raw, line, False

    if "pts" in bt or ("point" in bt and "3" not in bt):
        return player_stats.get("raw_pts", []), line, False

    if "assist" in bt or "ast" in bt:
        return player_stats.get("raw_ast", []), line, False

    # ── MLB ──
    if "total base" in bt:
        return player_stats.get("raw_total_bases", []), line, False

    if "home run" in bt or "homer" in bt:
        return player_stats.get("raw_home_runs", []), line, False

    if "rbi" in bt or "run batted" in bt:
        return player_stats.get("raw_rbi", []), line, False

    if "run" in bt and "home" not in bt:
        return player_stats.get("raw_runs", []), line, False

    if "strikeout" in bt or " k " in bt or bt.endswith(" k"):
        return player_stats.get("raw_strikeouts", []), line, False

    # ── Voetbal ──
    if "scorer" in bt or "goal" in bt:
        return player_stats.get("raw_goals", []), 1.0, True

    # Onbekend
    return [], line, False


# ─── Historische lambda (voor Poisson blending) ───────────────────────────────

def _get_hist_lam(bet_type: str, player_stats: dict) -> float:
    """
    Geeft historisch gemiddelde per game voor het stat-type in dit bet_type.
    Ondersteunt ook NBA combo-stats (PRA, RA, PA) en "pts"-notatie.
    """
    bt = bet_type.lower()

    # ── NHL shots / Soccer shots ──
    if ("shot" in bt or "sog" in bt) and "block" not in bt and "attempt" not in bt:
        return player_stats.get("hist_shots_avg", 0.0)
    if "block" in bt and "shot" in bt:
        return player_stats.get("hist_blocks_avg", 0.0)

    # ── NHL / Soccer goals ──
    if "anytime" in bt and ("goal" in bt or "scorer" in bt):
        return player_stats.get("hist_goals_avg", 0.0)
    if "goal" in bt:
        return player_stats.get("hist_goals_avg", 0.0)

    # ── NHL / MLB hits ──
    if "hit" in bt and "base" not in bt:
        return player_stats.get("hist_mlb_hits_avg") or player_stats.get("hist_hits_avg", 0.0)

    # ── NBA combo stats: PRA / RA / PA / PR ──
    # Detecteer via "+" separator of bekende afkortingen
    _has_pts = "pts" in bt or "point" in bt
    _has_reb = "reb" in bt or "rebound" in bt
    _has_ast = "ast" in bt or "assist" in bt
    _is_combo = ("+" in bt) or (_has_pts and _has_reb) or (_has_pts and _has_ast) or (_has_reb and _has_ast)

    if _is_combo:
        _lam = 0.0
        if _has_pts:
            _lam += player_stats.get("hist_points_avg", 0.0)
        if _has_reb:
            _lam += player_stats.get("hist_rebounds_avg") or player_stats.get("hist_reb_avg", 0.0)
        if _has_ast:
            _lam += player_stats.get("hist_assists_avg") or player_stats.get("hist_ast_avg", 0.0)
        return _lam

    # ── NBA / NHL assists ──
    if "assist" in bt or "ast" in bt:
        return player_stats.get("hist_assists_avg") or player_stats.get("hist_ast_avg", 0.0)

    # ── NBA rebounds ──
    if "rebound" in bt or " reb" in bt or bt.endswith("reb"):
        return player_stats.get("hist_rebounds_avg") or player_stats.get("hist_reb_avg", 0.0)

    # ── NBA / NHL points ("PTS" of "Points") ──
    if "point" in bt and "3" not in bt and "three" not in bt:
        return player_stats.get("hist_points_avg", 0.0)
    if "pts" in bt and "3pt" not in bt:
        return player_stats.get("hist_points_avg", 0.0)

    # ── NBA 3-pointers ──
    if "three" in bt or "3-point" in bt or "3pt" in bt or "3pm" in bt:
        return player_stats.get("hist_threes_avg", 0.0)

    # ── NBA blocks / steals ──
    if "block" in bt:
        return player_stats.get("hist_blocks_avg") or player_stats.get("hist_blk_avg", 0.0)
    if "steal" in bt:
        return player_stats.get("hist_steals_avg") or player_stats.get("hist_stl_avg", 0.0)

    # ── MLB ──
    if "home run" in bt or "homer" in bt:
        return player_stats.get("hist_mlb_home_runs_avg", 0.0)
    if "total base" in bt:
        return player_stats.get("hist_mlb_total_bases_avg", 0.0)
    if "rbi" in bt or "run batted" in bt:
        return player_stats.get("hist_mlb_rbi_avg", 0.0)
    if "run" in bt and "home" not in bt:
        return player_stats.get("hist_mlb_runs_avg", 0.0)
    if "strikeout" in bt or " k " in bt or bt.endswith(" k"):
        return player_stats.get("hist_mlb_strikeouts_avg", 0.0)

    return 0.0


# ─── Tegenstander factor ──────────────────────────────────────────────────────

def _opponent_factor(bet_type: str, opponent_stats: dict, sport: str = "NHL") -> float:
    """
    Hoe gunstig is de tegenstander voor dit bet-type?
    0.30 = moeilijk, 0.50 = neutraal, 0.70 = gunstig.
    """
    if not opponent_stats:
        return 0.5

    bt  = bet_type.lower()
    gaa = opponent_stats.get("goals_against_avg", 0.0)

    if sport == "NHL":
        if ("shot" in bt and "block" not in bt) or "point" in bt or "assist" in bt or "goal" in bt:
            if gaa > 3.2:
                return 0.70
            elif gaa > 2.8:
                return 0.55
            else:
                return 0.38
        if "hit" in bt or "block" in bt:
            return 0.5  # weinig afhankelijk van tegenstander

    elif sport == "SOCCER" or sport == "VOETBAL":
        if "scorer" in bt or "goal" in bt:
            if gaa > 1.8:
                return 0.68
            elif gaa > 1.2:
                return 0.52
            else:
                return 0.38

    elif sport == "MLB":
        bt = bet_type.lower()

        # ── Pitcher strikeout prop → opposing team K-rate ─────────────────
        # Hoge K-rate van de tegenliggende lineup = gunstig voor de pitcher.
        # MLB seizoensgemiddelde K-rate ≈ 22–23%.
        if "strikeout" in bt or (bt.endswith(" k") and "k_9" not in bt):
            k_rate = float(opponent_stats.get("team_k_rate") or 0.0)
            if k_rate > 0.26:    return 0.70   # hoog K-team → makkelijk te strikeoutten
            elif k_rate > 0.22:  return 0.55   # gemiddeld
            elif k_rate > 0.0:   return 0.36   # laag K-team → lineup maakt veel contact
            return 0.50                         # geen data beschikbaar

        # ── Hitter props (hits, RBI, runs, total bases, HR) → pitcher ERA ──
        # Slechte pitcher (hoge ERA) = gunstig voor de batter.
        # MLB seizoensgemiddelde ERA ≈ 4.35.
        pitcher_era = float(opponent_stats.get("pitcher_era") or 0.0)
        if pitcher_era > 0.0:
            if pitcher_era > 4.50:    return 0.68   # ruim onder gemiddeld → gunstig
            elif pitcher_era > 3.80:  return 0.52   # iets boven gemiddeld → neutraal
            else:                     return 0.36   # elite pitcher → moeilijk voor batters
        return 0.50                                  # geen pitcher-data beschikbaar

    # NBA: te weinig defensieve data voor betrouwbare factor
    return 0.5


# ─── Composite score ──────────────────────────────────────────────────────────

def composite_score(
    linemate_hit_rate: float,
    sample_size: int,
    bet_type: str,
    player_stats: dict,
    opponent_stats: dict,
    sport: str = "NHL",
    linemate_weight: float = 0.35,
    season_weight: float = 0.35,
) -> dict:
    """
    Berekent composite score voor één bet.

    Returns dict:
      composite, linemate_hr, season_hr, opp_factor, reliability, games_sampled
    """
    raw_values, threshold, use_gte = _get_raw_and_line(bet_type, player_stats)
    season_hr    = _hit_rate(raw_values, threshold, gte=use_gte)
    games_sampled = len(raw_values)

    # Blend met historische/CSV Poisson-schatting
    hist_lam = _get_hist_lam(bet_type, player_stats)
    if hist_lam > 0:
        hist_hr = _poisson_hr(hist_lam, threshold, gte=use_gte)
        if games_sampled == 0:
            # Geen raw data: gebruik puur Poisson-schatting (geen vervuiling met 0.5 default)
            season_hr = hist_hr
        else:
            # Groter gewicht op historie bij weinig recente games (bijv. vroeg in seizoen)
            hist_weight = 0.45 if games_sampled < 10 else 0.25
            season_hr = round((1 - hist_weight) * season_hr + hist_weight * hist_hr, 4)

    opp_factor   = _opponent_factor(bet_type, opponent_stats, sport)
    reliability  = _sample_reliability(sample_size)

    # Als er geen seizoensdata en geen historische data beschikbaar is:
    # gebruik alleen Linemate hit rate (redistribute season_weight naar linemate)
    no_season_data = (games_sampled == 0 and hist_lam == 0)
    if no_season_data:
        eff_lm_weight     = linemate_weight + season_weight
        eff_season_weight = 0.0
    else:
        eff_lm_weight     = linemate_weight
        eff_season_weight = season_weight

    composite = (
        eff_lm_weight    * linemate_hit_rate
        + eff_season_weight * season_hr
        + 0.20              * opp_factor
        + 0.10              * reliability
    )
    composite = min(max(composite, 0.0), 1.0)

    return {
        "composite":      round(composite, 4),
        "linemate_hr":    round(linemate_hit_rate, 4),
        "season_hr":      round(season_hr, 4),
        "opp_factor":     round(opp_factor, 4),
        "reliability":    round(reliability, 4),
        "games_sampled":  games_sampled,
        "no_season_data": no_season_data,
    }


def ev(composite_hit_rate: float, decimal_odds: float) -> float:
    """Expected Value per €1 inzet."""
    return round(composite_hit_rate * (decimal_odds - 1) - (1 - composite_hit_rate), 4)


def rating(ev_score: float, composite: float) -> str:
    """✅ Sterk / ⚠️ Matig / ❌ Vermijd op basis van EV en composite hit rate."""
    if ev_score >= 0.25 and composite >= 0.62:
        return "✅ Sterk"
    elif ev_score >= 0.05 and composite >= 0.52:
        return "⚠️ Matig"
    else:
        return "❌ Vermijd"
