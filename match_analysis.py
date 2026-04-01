"""
match_analysis.py — Wedstrijd-analyse (NHL, NBA, MLB, Soccer)
Geen Streamlit-afhankelijkheden.
"""

import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from sports import nhl, nba, mlb, soccer, odds_api

# ─── Gedeelde helpers ─────────────────────────────────────────────────────────

_BLEND_LAST10_WEIGHT = 0.60   # gewicht voor last-10 rolling average
_BLEND_SEASON_WEIGHT = 0.40   # gewicht voor seizoensgemiddelde


def _blend(season_val: float, last10_val, w_last10: float = _BLEND_LAST10_WEIGHT) -> float:
    """
    Blend seizoensgemiddelde met last-10 rolling average.

      blended = (last10 × 0.60) + (season × 0.40)

    Als last10_val None of 0 is (geen data beschikbaar), wordt alleen het
    seizoensgemiddelde teruggegeven als fallback.
    """
    if last10_val is None or float(last10_val) <= 0:
        return season_val
    return round(float(last10_val) * w_last10 + float(season_val) * (1.0 - w_last10), 2)


def _poisson_p(k: int, lam: float) -> float:
    """P(X = k) voor Poisson-verdeling."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _match_ev(model_prob: float, odds: float) -> float:
    """EV = p × (odds - 1) - (1 - p)"""
    if not odds or odds <= 1.0:
        return -1.0
    return round(model_prob * (odds - 1) - (1 - model_prob), 4)


def _match_rating(ev_val: float) -> str:
    if ev_val >= 0.05:
        return "✅ Waarde"
    if ev_val >= -0.05:
        return "⚠️ Neutraal"
    return "❌ Vermijd"


def _make_option(prob_key: str, probs: dict, odds_val, label: str) -> dict:
    p = probs.get(prob_key, 0.0)
    ev_val = _match_ev(p, odds_val) if odds_val else None
    return {
        "label":  label,
        "prob":   p,
        "odds":   odds_val,
        "ev":     ev_val,
        "rating": _match_rating(ev_val) if ev_val is not None else "—",
    }


def _best_option(options: list):
    return max(
        (o for o in options if o["ev"] is not None),
        key=lambda o: o["ev"],
        default=None,
    )


# ─── NHL Wedstrijd-analyse ────────────────────────────────────────────────────

_NHL_LEAGUE_GF_AVG = 3.05
_NHL_HOME_ICE      = 1.08
_NHL_OT_BASE_RATE  = 0.235


def _nhl_match_probs(home_form: dict, away_form: dict) -> dict:
    lH = home_form.get("gf_avg", _NHL_LEAGUE_GF_AVG) * \
         (away_form.get("ga_avg", _NHL_LEAGUE_GF_AVG) / _NHL_LEAGUE_GF_AVG) * \
         _NHL_HOME_ICE
    lA = away_form.get("gf_avg", _NHL_LEAGUE_GF_AVG) * \
         (home_form.get("ga_avg", _NHL_LEAGUE_GF_AVG) / _NHL_LEAGUE_GF_AVG)
    lH = max(1.5, min(lH, 5.0))
    lA = max(1.5, min(lA, 5.0))
    p_home = p_draw = p_away = 0.0
    for h in range(9):
        for a in range(9):
            p = _poisson_p(h, lH) * _poisson_p(a, lA)
            if h > a:      p_home += p
            elif h == a:   p_draw += p
            else:          p_away += p
    tot = p_home + p_draw + p_away or 1.0
    return {
        "p_home": round(p_home / tot, 4),
        "p_draw": round(p_draw / tot, 4),
        "p_away": round(p_away / tot, 4),
        "lH": round(lH, 2),
        "lA": round(lA, 2),
    }


def analyze_nhl_matches(matches: list) -> list:
    results = []
    fallback = {"gf_avg": _NHL_LEAGUE_GF_AVG, "ga_avg": _NHL_LEAGUE_GF_AVG}

    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper()
        if "NHL" not in sport and "HOCKEY" not in sport:
            continue
        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        if not home_name or not away_name:
            continue

        home_form = {}
        away_form = {}
        try:
            home_form = nhl.get_team_form(home_name)
        except Exception:
            pass
        try:
            away_form = nhl.get_team_form(away_name)
        except Exception:
            pass

        # ── Last-10 goals ophalen en blenden met seizoensgemiddelden ──────────
        # blended = (last10 × 0.60) + (season × 0.40)
        # Fallback naar seizoensgemiddelde als last-10 niet beschikbaar is.
        home_last10: dict = {}
        away_last10: dict = {}
        try:
            home_abbrev = (home_form or {}).get("abbrev", "")
            if home_abbrev:
                home_last10 = nhl.get_team_last10_goals(home_abbrev)
        except Exception:
            pass
        try:
            away_abbrev = (away_form or {}).get("abbrev", "")
            if away_abbrev:
                away_last10 = nhl.get_team_last10_goals(away_abbrev)
        except Exception:
            pass

        # Bouw blended form-dicts voor het Poisson-model
        hf = dict(home_form or fallback)
        af = dict(away_form or fallback)
        hf["gf_avg"] = _blend(hf.get("gf_avg", _NHL_LEAGUE_GF_AVG), home_last10.get("last10_gf_avg"))
        hf["ga_avg"] = _blend(hf.get("ga_avg", _NHL_LEAGUE_GF_AVG), home_last10.get("last10_ga_avg"))
        af["gf_avg"] = _blend(af.get("gf_avg", _NHL_LEAGUE_GF_AVG), away_last10.get("last10_gf_avg"))
        af["ga_avg"] = _blend(af.get("ga_avg", _NHL_LEAGUE_GF_AVG), away_last10.get("last10_ga_avg"))

        scr_odds  = m.get("screenshot_odds") or {}
        b365_odds = {}
        odds_bron = "screenshot"
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                b365_odds = odds_api.get_match_odds_h2h("NHL", home_name, away_name)
                if b365_odds.get("source") == "bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass

        def _odds(key_b365, key_scr):
            v = b365_odds.get(key_b365) or scr_odds.get(key_scr)
            return float(v) if v else None

        probs = {}
        if hf or af:
            probs = _nhl_match_probs(hf, af)

        home_odds = _odds("home_odds", "home")
        draw_odds = _odds("draw_odds", "draw")
        away_odds = _odds("away_odds", "away")

        options = [
            _make_option("p_home", probs, home_odds, f"🏠 {home_name} wint"),
            _make_option("p_draw", probs, draw_odds, "🔄 OT / SO"),
            _make_option("p_away", probs, away_odds, f"✈️ {away_name} wint"),
        ]

        results.append({
            "home_team":    home_name,
            "away_team":    away_name,
            "time":         m.get("time"),
            "home_form":    home_form,          # origineel (voor weergave)
            "away_form":    away_form,           # origineel (voor weergave)
            "home_last10":  home_last10,         # last-10 goals data
            "away_last10":  away_last10,
            "probs":        probs,
            "odds_bron":    odds_bron,
            "options":      options,
            "best":         _best_option(options),
        })
    return results


# ─── Soccer Wedstrijd-analyse ─────────────────────────────────────────────────

_SOCCER_LEAGUE_AVG  = 1.35
_SOCCER_HOME_FACTOR = 1.15

_SOCCER_SPORTS = {
    "EPL", "PREMIERLEAGUE", "LALIGA", "BUNDESLIGA",
    "SERIEA", "LIGUE1", "VOETBAL", "SOCCER", "UCL",
}


def _soccer_form_from_api(team_name: str, competition: str) -> dict:
    try:
        comp = competition.upper() if competition else "EPL"
        raw  = soccer.get_team_stats_for_match(team_name, comp)
        if not raw:
            return {}
        return {
            "full_name":   raw.get("name", team_name),
            "abbrev":      raw.get("name", team_name)[:3].upper(),
            "gf_avg":      raw.get("avg_goals_for", _SOCCER_LEAGUE_AVG),
            "ga_avg":      raw.get("avg_goals_against", _SOCCER_LEAGUE_AVG),
            "form":        raw.get("form", ""),
            "last10":      raw.get("form", ""),
            "streak":      "",
            "home_record": "—",
            "road_record": "—",
        }
    except Exception:
        return {}


def _soccer_match_probs(home_form: dict, away_form: dict) -> dict:
    lH = home_form.get("gf_avg", _SOCCER_LEAGUE_AVG) * \
         (away_form.get("ga_avg", _SOCCER_LEAGUE_AVG) / _SOCCER_LEAGUE_AVG) * \
         _SOCCER_HOME_FACTOR
    lA = away_form.get("gf_avg", _SOCCER_LEAGUE_AVG) * \
         (home_form.get("ga_avg", _SOCCER_LEAGUE_AVG) / _SOCCER_LEAGUE_AVG)
    lH = max(0.4, min(lH, 5.0))
    lA = max(0.4, min(lA, 5.0))
    p_home = p_draw = p_away = 0.0
    for h in range(8):
        for a in range(8):
            p = _poisson_p(h, lH) * _poisson_p(a, lA)
            if h > a:      p_home += p
            elif h == a:   p_draw += p
            else:          p_away += p
    tot = p_home + p_draw + p_away or 1.0
    return {
        "p_home": round(p_home / tot, 4),
        "p_draw": round(p_draw / tot, 4),
        "p_away": round(p_away / tot, 4),
        "lH": round(lH, 2),
        "lA": round(lA, 2),
    }


def analyze_soccer_matches(matches: list) -> list:
    results = []
    fallback = {"gf_avg": _SOCCER_LEAGUE_AVG, "ga_avg": _SOCCER_LEAGUE_AVG}
    sp_map = {
        "EPL": "EPL", "PREMIERLEAGUE": "EPL", "LALIGA": "LALIGA",
        "BUNDESLIGA": "BUNDESLIGA", "SERIEA": "SERIEA",
        "LIGUE1": "LIGUE1", "UCL": "UCL",
    }

    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper().replace(" ", "")
        is_soccer = any(s.replace(" ", "") in sport for s in _SOCCER_SPORTS) or \
                    not any(x in sport for x in ("NHL", "HOCKEY", "NBA", "BASKETBALL", "MLB", "BASEBALL"))
        if not is_soccer:
            continue
        home_name   = m.get("home_team", "")
        away_name   = m.get("away_team", "")
        if not home_name or not away_name:
            continue
        competition = m.get("competition") or m.get("sport") or "EPL"
        home_form   = _soccer_form_from_api(home_name, competition)
        away_form   = _soccer_form_from_api(away_name, competition)
        probs       = _soccer_match_probs(home_form or fallback, away_form or fallback)

        scr_odds  = m.get("screenshot_odds") or {}
        b365      = {}
        odds_bron = "screenshot"
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                sp = sp_map.get(competition.upper().replace(" ", ""), "EPL")
                b365 = odds_api.get_match_odds_h2h(sp, home_name, away_name)
                if b365.get("source") == "bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass

        home_odds = b365.get("home_odds") if odds_bron == "Bet365" else scr_odds.get("home")
        draw_odds = b365.get("draw_odds") if odds_bron == "Bet365" else scr_odds.get("draw")
        away_odds = b365.get("away_odds") if odds_bron == "Bet365" else scr_odds.get("away")

        options = [
            _make_option("p_home", probs, home_odds, f"🏠 {home_name} wint"),
            _make_option("p_draw", probs, draw_odds, "🤝 Gelijkspel"),
            _make_option("p_away", probs, away_odds, f"✈️ {away_name} wint"),
        ]
        results.append({
            "home_team":   home_name,
            "away_team":   away_name,
            "time":        m.get("time"),
            "competition": competition,
            "home_form":   home_form,
            "away_form":   away_form,
            "probs":       probs,
            "odds_bron":   odds_bron,
            "options":     options,
            "best":        _best_option(options),
        })
    return results


# ─── NBA Wedstrijd-analyse ────────────────────────────────────────────────────

_NBA_LEAGUE_PTS_AVG = 112.0
_NBA_HOME_ADV       = 3.0
_NBA_MARGIN_STD     = 13.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _nba_match_probs(home_form: dict, away_form: dict, spread: float = 0.0) -> dict:
    pts_h = home_form.get("pts_avg", _NBA_LEAGUE_PTS_AVG)
    opp_h = home_form.get("opp_pts_avg", _NBA_LEAGUE_PTS_AVG)
    pts_a = away_form.get("pts_avg", _NBA_LEAGUE_PTS_AVG)
    opp_a = away_form.get("opp_pts_avg", _NBA_LEAGUE_PTS_AVG)
    exp_home   = (pts_h + opp_a) / 2.0 + _NBA_HOME_ADV
    exp_away   = (pts_a + opp_h) / 2.0
    exp_margin = exp_home - exp_away
    p_home        = _norm_cdf(exp_margin / _NBA_MARGIN_STD)
    p_cover_home  = _norm_cdf((exp_margin - spread) / _NBA_MARGIN_STD) if spread else p_home
    return {
        "p_home":         round(p_home, 4),
        "p_away":         round(1 - p_home, 4),
        "p_cover_home":   round(p_cover_home, 4),
        "p_cover_away":   round(1 - p_cover_home, 4),
        "exp_margin":     round(exp_margin, 1),
        "exp_home_pts":   round(exp_home, 1),
        "exp_away_pts":   round(exp_away, 1),
    }


def analyze_nba_matches(matches: list) -> list:
    results = []
    fallback = {"pts_avg": _NBA_LEAGUE_PTS_AVG, "opp_pts_avg": _NBA_LEAGUE_PTS_AVG}

    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper()
        if "NBA" not in sport and "BASKETBALL" not in sport:
            continue
        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        if not home_name or not away_name:
            continue

        home_form = nba.get_team_form_for_match(home_name)
        away_form = nba.get_team_form_for_match(away_name)

        # ── Last-10 stats ophalen en blenden met seizoensgemiddelden ──────────
        # blended = (last10 × 0.60) + (season × 0.40)
        # Fallback naar seizoensgemiddelde als last-10 niet beschikbaar is.
        home_last10: dict = {}
        away_last10: dict = {}
        try:
            home_last10 = nba.get_team_last10_stats(home_name)
        except Exception:
            pass
        try:
            away_last10 = nba.get_team_last10_stats(away_name)
        except Exception:
            pass

        # Bouw blended form-dicts voor het Normal-distributie-model
        hf = dict(home_form or {"pts_avg": _NBA_LEAGUE_PTS_AVG, "opp_pts_avg": _NBA_LEAGUE_PTS_AVG})
        af = dict(away_form or {"pts_avg": _NBA_LEAGUE_PTS_AVG, "opp_pts_avg": _NBA_LEAGUE_PTS_AVG})
        hf["pts_avg"]     = _blend(hf.get("pts_avg",     _NBA_LEAGUE_PTS_AVG), home_last10.get("last10_pts"))
        hf["opp_pts_avg"] = _blend(hf.get("opp_pts_avg", _NBA_LEAGUE_PTS_AVG), home_last10.get("last10_opp_pts"))
        af["pts_avg"]     = _blend(af.get("pts_avg",     _NBA_LEAGUE_PTS_AVG), away_last10.get("last10_pts"))
        af["opp_pts_avg"] = _blend(af.get("opp_pts_avg", _NBA_LEAGUE_PTS_AVG), away_last10.get("last10_opp_pts"))

        scr_odds  = m.get("screenshot_odds") or {}
        b365_h2h  = {}
        b365_sp   = {}
        odds_bron = "screenshot"
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                b365_sp  = odds_api.get_match_odds_spreads("NBA", home_name, away_name)
                b365_h2h = odds_api.get_match_odds_h2h("NBA", home_name, away_name)
                if b365_sp.get("source") == "bet365" or b365_h2h.get("source") == "bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass

        home_ml    = b365_h2h.get("home_odds") or scr_odds.get("home")
        away_ml    = b365_h2h.get("away_odds") or scr_odds.get("away")
        hs         = b365_sp.get("home_spread")
        as_        = b365_sp.get("away_spread")
        h_sp_odds  = b365_sp.get("home_spread_odds")
        a_sp_odds  = b365_sp.get("away_spread_odds")
        spread_val = float(hs) if hs is not None else 0.0
        probs      = _nba_match_probs(hf, af, spread=spread_val)

        options = [
            _make_option("p_home", probs, home_ml, f"🏠 {home_name} wint"),
            _make_option("p_away", probs, away_ml, f"✈️ {away_name} wint"),
        ]
        if hs is not None and h_sp_odds:
            options.append(_make_option("p_cover_home", probs, h_sp_odds, f"🏠 {home_name} {hs:+.1f}"))
            options.append(_make_option("p_cover_away", probs, a_sp_odds,
                                        f"✈️ {away_name} {as_:+.1f}" if as_ else f"✈️ {away_name} spread"))

        results.append({
            "home_team":   home_name,
            "away_team":   away_name,
            "time":        m.get("time"),
            "home_form":   home_form,           # origineel (voor weergave)
            "away_form":   away_form,            # origineel (voor weergave)
            "home_last10": home_last10,          # last-10 stats data
            "away_last10": away_last10,
            "probs":       probs,
            "odds_bron":   odds_bron,
            "options":     options,
            "best":        _best_option(options),
            "spread":      hs,
        })
    return results


# ─── MLB Wedstrijd-analyse ────────────────────────────────────────────────────

_MLB_LEAGUE_RUNS_AVG = 4.35
_MLB_LEAGUE_ERA      = 4.35   # referentie-ERA voor normalisatie (MLB gem.)
_MLB_HOME_FACTOR     = 1.05
_MLB_RUN_LINE        = 1.5
_MLB_PITCHER_WEIGHT  = 0.65   # gewicht van ERA-correctie vs. team-defensie
                               # 0.65 = pitcher verklaart ~65% van runs-toegelaten


def _mlb_match_probs(home_form: dict, away_form: dict,
                     home_pitcher: dict = None, away_pitcher: dict = None) -> dict:
    """
    Bereken verwachte runs per team via Poisson-model.

    Zonder pitchers: gebruikt team opp_runs_avg als maat voor defensie.
    Met pitchers: vervangt defensie-component door pitcher ERA (genormaliseerd
    naar league gemiddelde), gewogen via _MLB_PITCHER_WEIGHT.

    Formule (per team):
      defense_factor = (pitcher_ERA / LEAGUE_ERA) × weight
                     + (team_opp_runs / LEAGUE_RUNS) × (1 - weight)
      λ = team_runs_avg × defense_factor × [home_factor]
    """
    def _defense_factor(team_form: dict, pitcher: dict) -> float:
        """Normaliseer het defensieve vermogen van de tegenpartij (pitcher + team)."""
        team_def = team_form.get("opp_runs_avg", _MLB_LEAGUE_RUNS_AVG) / _MLB_LEAGUE_RUNS_AVG
        if pitcher and pitcher.get("era"):
            era_factor = pitcher["era"] / _MLB_LEAGUE_ERA
            return era_factor * _MLB_PITCHER_WEIGHT + team_def * (1 - _MLB_PITCHER_WEIGHT)
        return team_def

    # Away pitcher staat tegenover de home batters (en omgekeerd)
    home_defense = _defense_factor(away_form, away_pitcher)  # away pitcher
    away_defense = _defense_factor(home_form, home_pitcher)  # home pitcher

    lH = home_form.get("runs_avg", _MLB_LEAGUE_RUNS_AVG) * home_defense * _MLB_HOME_FACTOR
    lA = away_form.get("runs_avg", _MLB_LEAGUE_RUNS_AVG) * away_defense
    lH = max(1.5, min(lH, 9.0))
    lA = max(1.5, min(lA, 9.0))
    p_home = p_away = p_home_rl = p_away_rl = 0.0
    for h in range(20):
        for a in range(20):
            p = _poisson_p(h, lH) * _poisson_p(a, lA)
            if h > a:
                p_home += p
                if h - a >= 2:
                    p_home_rl += p
            elif h < a:
                p_away    += p
                p_away_rl += p
    tot    = p_home + p_away or 1.0
    rl_tot = p_home_rl + p_away_rl or 1.0
    return {
        "p_home":    round(p_home / tot, 4),
        "p_away":    round(p_away / tot, 4),
        "p_home_rl": round(p_home_rl / rl_tot, 4),
        "p_away_rl": round(p_away_rl / rl_tot, 4),
        "lH": round(lH, 2),
        "lA": round(lA, 2),
    }


def analyze_mlb_matches(matches: list) -> list:
    results = []
    fallback = {"runs_avg": _MLB_LEAGUE_RUNS_AVG, "opp_runs_avg": _MLB_LEAGUE_RUNS_AVG}

    for m in matches:
        sport = (m.get("sport") or m.get("competition") or "").upper()
        if not any(x in sport for x in ("MLB", "BASEBALL", "HONKBAL")):
            continue
        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        if not home_name or not away_name:
            continue

        home_form = mlb.get_team_form_for_match(home_name)
        away_form = mlb.get_team_form_for_match(away_name)

        # Startende werpers opzoeken
        pitchers     = {}
        home_pitcher = None
        away_pitcher = None
        try:
            pitchers     = mlb.get_probable_pitchers(home_name, away_name)
            home_pitcher = pitchers.get("home") or None
            away_pitcher = pitchers.get("away") or None
        except Exception:
            pass

        probs = _mlb_match_probs(
            home_form or fallback,
            away_form or fallback,
            home_pitcher=home_pitcher,
            away_pitcher=away_pitcher,
        )
        scr_odds  = m.get("screenshot_odds") or {}
        b365_h2h  = {}
        b365_sp   = {}
        odds_bron = "screenshot"
        try:
            if odds_api._API_KEY and not odds_api.is_limit_reached():
                b365_h2h = odds_api.get_match_odds_h2h("MLB", home_name, away_name)
                b365_sp  = odds_api.get_match_odds_spreads("MLB", home_name, away_name)
                if b365_h2h.get("source") == "bet365" or b365_sp.get("source") == "bet365":
                    odds_bron = "Bet365"
        except Exception:
            pass

        home_ml   = b365_h2h.get("home_odds") or scr_odds.get("home")
        away_ml   = b365_h2h.get("away_odds") or scr_odds.get("away")
        hs        = b365_sp.get("home_spread")
        rl_val    = abs(float(hs)) if hs is not None else _MLB_RUN_LINE
        h_rl_odds = b365_sp.get("home_spread_odds")
        a_rl_odds = b365_sp.get("away_spread_odds")

        lH = probs.get("lH", _MLB_LEAGUE_RUNS_AVG)
        lA = probs.get("lA", _MLB_LEAGUE_RUNS_AVG)

        def _p_home_rl_n(n):
            p = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                    for h in range(20) for a in range(20) if h - a >= int(n))
            tot = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                      for h in range(20) for a in range(20) if h != a)
            return round(p / tot, 4) if tot > 0 else 0.0

        def _p_away_rl_n(n):
            p = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                    for h in range(20) for a in range(20) if a - h >= 0)
            tot = sum(_poisson_p(h, lH) * _poisson_p(a, lA)
                      for h in range(20) for a in range(20) if h != a)
            return round(p / tot, 4) if tot > 0 else 0.0

        options = [
            _make_option("p_home",    probs, home_ml,   f"🏠 {home_name} wint"),
            _make_option("p_away",    probs, away_ml,   f"✈️ {away_name} wint"),
            _make_option("p_home_rl", probs, h_rl_odds, f"🏠 {home_name} -{rl_val:.1f} RL"),
            _make_option("p_away_rl", probs, a_rl_odds, f"✈️ {away_name} +{rl_val:.1f} RL"),
        ]
        # Voeg -2.5 / +2.5 opties toe (zonder odds, alleen voor model-inzicht)
        p_h25 = _p_home_rl_n(3)
        p_a25 = _p_away_rl_n(0)
        options.append({"label": f"🏠 {home_name} -2.5 RL", "prob": p_h25, "odds": None,
                         "ev": None, "rating": "—"})
        options.append({"label": f"✈️ {away_name} +2.5 RL", "prob": p_a25, "odds": None,
                         "ev": None, "rating": "—"})

        results.append({
            "home_team":    home_name,
            "away_team":    away_name,
            "time":         m.get("time"),
            "home_form":    home_form,
            "away_form":    away_form,
            "home_pitcher": home_pitcher,
            "away_pitcher": away_pitcher,
            "probs":        probs,
            "odds_bron":    odds_bron,
            "options":      options,
            "best":         _best_option(options),
            "run_line":     rl_val,
        })
    return results
