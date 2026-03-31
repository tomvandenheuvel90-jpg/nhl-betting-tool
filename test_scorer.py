#!/usr/bin/env python3
"""
test_scorer.py — Snelle sanity-check voor scoring- en rankinglogica.

Gebruik:
    python test_scorer.py

Verwacht: alle tests PASS, geen AssertionError.
Vereist GEEN Streamlit, GEEN Anthropic API, GEEN database.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "✅ PASS"
FAIL = "❌ FAIL"
_results = []

def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    msg    = f"  {status}  {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    _results.append((name, condition))
    if not condition:
        raise AssertionError(f"Test mislukt: {name}  {detail}")

# ─── scorer.py tests ──────────────────────────────────────────────────────────

print("\n─── scorer.py ───")
from scorer import ev, rating, composite_score

# EV formule: EV = hr * (odds - 1) - (1 - hr)
_ev1 = ev(0.80, 2.00)   # 0.80 * 1.0 - 0.20 = +0.60
check("ev(0.80, 2.00) == +0.60",  abs(_ev1 - 0.60) < 1e-9, f"got {_ev1}")

_ev2 = ev(0.40, 2.00)   # 0.40 * 1.0 - 0.60 = -0.20
check("ev(0.40, 2.00) == -0.20",  abs(_ev2 - (-0.20)) < 1e-9, f"got {_ev2}")

_ev3 = ev(0.50, 2.00)   # breakevenpoint
check("ev(0.50, 2.00) == 0.00",   abs(_ev3) < 1e-9, f"got {_ev3}")

# rating: hoge EV + hoge composite → Sterk
_rat1 = rating(0.30, 0.70)
check("rating(0.30, 0.70) bevat 'Sterk'", "Sterk" in _rat1, f"got '{_rat1}'")

# rating: lage EV → Vermijd
_rat2 = rating(-0.10, 0.50)
check("rating(-0.10, 0.50) bevat 'Vermijd'", "Vermijd" in _rat2, f"got '{_rat2}'")

# composite_score: testoproep met minimale argumenten
_cs_result = composite_score(
    linemate_hit_rate=0.70,
    sample_size=10,
    bet_type="Over 1.5 Shots on Goal",
    player_stats={},
    opponent_stats={},
    sport="NHL",
    linemate_weight=0.35,
    season_weight=0.35,
)
# composite_score geeft een dict terug met "composite" key
_cs = _cs_result.get("composite", _cs_result) if isinstance(_cs_result, dict) else float(_cs_result)
check("composite_score met linemate_hr=0.70 geeft getal terug", isinstance(_cs, (int, float)), f"got {type(_cs_result)}")

# ─── analysis.py ranking tests ────────────────────────────────────────────────

print("\n─── analysis.py (filter_and_rank_props) ───")
from analysis import filter_and_rank_props

def _make_bet(player, ev_val, composite, sample_n, b365_status=""):
    rat = rating(ev_val, composite)
    return {
        "player":    player,
        "bet_type":  "Over 1.5 Shots",
        "sport":     "NHL",
        "team":      "TOR",
        "sample":    f"{int(sample_n*0.8)}/{sample_n}",
        "ev":        ev_val,
        "composite": composite,
        "linemate_hr": composite,
        "season_hr":   composite,
        "odds":      2.00,
        "sample_n":  sample_n,
        "rating":    rat,
        "bet365":    {"status": b365_status} if b365_status else {},
    }

_bets = [
    _make_bet("Alice",    0.30, 0.80, 15),              # goed → moet door
    _make_bet("Bob",      0.10, 0.60, 10),              # matig maar positief → door
    _make_bet("Charlie", -0.05, 0.55, 8),               # negatieve EV → uitgesloten
    _make_bet("Dave",     0.20, 0.75, 2),               # sample_n < 3 → uitgesloten
    _make_bet("Eve",      0.25, 0.78, 12, "unavailable"),  # unavailable → uitgesloten
    _make_bet("Frank",    0.20, 0.72, 4, "different_line"), # penalty −15% EV, dan −40%
]

_ranked = filter_and_rank_props(_bets)
_names  = [b["player"] for b in _ranked]

check("Alice in ranking (hoge EV)",           "Alice" in _names)
check("Bob in ranking (positieve EV)",         "Bob"   in _names)
check("Charlie NIET in ranking (neg. EV)",     "Charlie" not in _names)
check("Dave NIET in ranking (sample < 3)",     "Dave"    not in _names)
check("Eve NIET in ranking (unavailable)",     "Eve"     not in _names)
check("Frank in ranking (penalty toegepast)",  "Frank"   in _names)
check("Alice staat boven Bob (hogere EV)",     _names.index("Alice") < _names.index("Bob"))

# Bug 1 check: rating moet overeenkomen met ev na penalties
_frank = next(b for b in _ranked if b["player"] == "Frank")
_frank_ev = _frank["ev"]
_frank_rat = _frank["rating"]
_expected_rat = rating(_frank_ev, _frank["composite"])
check(
    "Bug 1 fix: Frank's rating klopt met penalized EV",
    _frank_rat == _expected_rat,
    f"rating='{_frank_rat}' expected='{_expected_rat}' ev={_frank_ev:.4f}"
)

# ─── Bug 2 check: detect_sports_from_matches ─────────────────────────────────

print("\n─── analysis.py (detect_sports_from_matches / is_nhl_match) ───")
from analysis import is_nhl_match, is_nba_match, is_mlb_match

# Match met alleen 'sport' veld (geen 'competition') — Bug 2 scenario
_m_nhl = {"home_team": "Toronto Maple Leafs", "away_team": "Boston Bruins",
           "sport": "NHL", "competition": None}
check("is_nhl_match met sport=NHL, competition=None", is_nhl_match(_m_nhl))

_m_nba = {"home_team": "Lakers", "away_team": "Celtics", "sport": "NBA", "competition": None}
check("is_nba_match met sport=NBA", is_nba_match(_m_nba))

_m_mlb = {"home_team": "Yankees", "away_team": "Red Sox", "sport": "MLB", "competition": None}
check("is_mlb_match met sport=MLB", is_mlb_match(_m_mlb))

# NHL fallback op teamnaam (beide velden leeg)
_m_nhl2 = {"home_team": "Colorado Avalanche", "away_team": "Seattle Kraken",
            "sport": "", "competition": ""}
check("is_nhl_match fallback op teamnaam (Avalanche/Kraken)", is_nhl_match(_m_nhl2))

# ─── Bug 3 check: sample_n default ───────────────────────────────────────────

print("\n─── analysis.py (enrich_bet — sample_n default) ───")
# We kunnen enrich_bet niet eenvoudig aanroepen zonder API, maar we kunnen de
# filter_and_rank logica testen: sample_n=0 moet behandeld worden als "onbekend",
# NIET als "kleine sample" (penalty)
_bet_no_sample = _make_bet("Ghost", 0.20, 0.75, 0)   # sample_n=0 = onbekend
_ranked_ghost  = filter_and_rank_props([_bet_no_sample])
check(
    "Bug 3 fix: sample_n=0 (onbekend) geeft GEEN kleine-sample penalty",
    len(_ranked_ghost) == 1,
    f"Ghost {'gevonden' if _ranked_ghost else 'NIET gevonden — ten onrechte uitgesloten'}",
)
if _ranked_ghost:
    _g = _ranked_ghost[0]
    check(
        "Bug 3 fix: Ghost heeft geen _sample_warning bij sample_n=0",
        "_sample_warning" not in _g,
        f"keys: {list(_g.keys())}"
    )

# ─── Prompts constants ────────────────────────────────────────────────────────

print("\n─── prompts.py ───")
from prompts import SCENARIO_WEIGHTS, SCENARIO_LABELS, EXTRACT_MODEL

check("Scenario 1 weights: linemate_weight==0.00", SCENARIO_WEIGHTS[1][0] == 0.00)
check("Scenario 2 weights: linemate_weight==0.42", SCENARIO_WEIGHTS[2][0] == 0.42)
check("Scenario 3 weights: beide ==0.35",           SCENARIO_WEIGHTS[3] == (0.35, 0.35))
check("EXTRACT_MODEL bevat 'haiku'",                "haiku" in EXTRACT_MODEL)
check("Alle 3 scenario labels aanwezig",            len(SCENARIO_LABELS) == 3)

# ─── Samenvatting ─────────────────────────────────────────────────────────────

print()
_passed = sum(1 for _, ok in _results if ok)
_total  = len(_results)
print(f"{'='*50}")
print(f"  Resultaat: {_passed}/{_total} tests geslaagd")
if _passed == _total:
    print("  ✅ Alle tests PASS — scorer en ranking werken correct.")
else:
    print("  ❌ Er zijn mislukte tests — zie details hierboven.")
print(f"{'='*50}")

sys.exit(0 if _passed == _total else 1)
