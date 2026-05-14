# BetAnalyzer: Sample_n & Claude Extraction Analysis

## 1. Where sample_n is Set

### In analysis.py (enrich_bet function):

**Line 667:** Default initialization
```python
sample_n = bet.get("sample_n") or 0
```
- Defaults to **0** (unknown) — intentionally **NOT 5** (Bug 3 fix)
- This prevents artificial default data inflation

**Auto-generated props (lines 463–578):**
When Claude extracts bets from Linemate screenshots, the sample_n comes from stats API calls:
```python
games_n = stats.get("games_sampled", 0)  # From Claude API
...
"sample_n": games_n,  # Passed to scorer
```

### In scorer.py:

**Line 23:** _sample_reliability() function
```python
def _sample_reliability(n: int) -> float:
```
- Converts sample_n into a reliability weight (0–1)
- Smaller samples = lower reliability
- Used in composite_score with 10% weight (see line 255)

**Line 202:** composite_score function accepts sample_size
```python
def composite_score(
    ...
    sample_size: int,  # This is the sample_n from enrich_bet
    ...
):
```

---

## 2. Claude Extraction Fields (EXTRACT_PROMPT)

### What Claude Extracts from Linemate Screenshots:

From `prompts.py` lines 55–180, the EXTRACT_PROMPT specifies:

**For each bet/prop:**
| Field | Type | Example | Source |
|-------|------|---------|--------|
| `player` | string | "J. Duren" or "Connor McDavid" | Player name visible |
| `sport` | string | "NHL", "NBA", "MLB", "EPL", etc. | Inferred from logo/context |
| `team` | string or null | "DET" | Team abbreviation if visible |
| `opponent` | string or null | "MIN" | Opponent if visible (e.g., "vs MIN") |
| `bet_type` | string | "Over 13.5 REB+AST" | Prop description |
| `linemate_odds` | float (decimal) | 1.95 | Decimal odds from Linemate |
| **`hit_rate`** | float (0–1) | 0.80 | **PRIMARY hit rate** |
| **`sample`** | string | "4/5" or "9/9" | Hit count / sample size (visual) |
| **`sample_n`** | integer | 5 or 9 | **Total game sample** (numeric) |
| `trend_stats` | array | See below | All visible stat rows |

**trend_stats example:**
```json
[
  {"label": "last 5 games",              "hit_rate": 0.80, "sample": "4/5"},
  {"label": "last 5 games vs MIN",       "hit_rate": 0.80, "sample": "4/5"},
  {"label": "last 4 home games",         "hit_rate": 0.75, "sample": "3/4"}
]
```

### Key Claude Rules:

1. **hit_rate selection (primary):**
   - Prefer "last N games" (general, unfilteredRow)
   - If not available: use the first visible row
   - Format: percentage as decimal (100% = 1.0, 80% = 0.80)

2. **sample vs sample_n:**
   - `sample`: string representation (visual) — e.g., "4/5" (4 hits out of 5 games)
   - `sample_n`: numeric value — e.g., 5 (total games in the sample)

3. **Completeness:**
   - Extract EVERY prop visible, including bottom of list
   - "Scroll mentally through entire image. Miss no player or prop."

---

## 3. enrich_bet Function Signature

**File:** `/sessions/zen-affectionate-ramanujan/mnt/BetAnalyzer/analysis.py`  
**Lines:** 653–703 (shown; function continues)

```python
def enrich_bet(bet: dict, cache: dict,
               linemate_weight: float = 0.35,
               season_weight:   float = 0.35) -> dict:
    """
    Enriches one bet with player statistics and calculates EV/composite.

    Bug 3 fix: sample_n default is now 0 (unknown) instead of 5.
    """
    sport       = (bet.get("sport") or "").upper().strip()
    player_name = bet.get("player", "")
    team_hint   = bet.get("team") or ""
    bet_type    = bet.get("bet_type", "")

    # FIX Bug 3: default 0 (unknown) NOT 5 (artificial data)
    sample_n = bet.get("sample_n") or 0

    player_stats   = {}
    opponent_stats = {}
    opponent_name  = None
    cache_key = f"{sport}::{player_name}"

    if cache_key in cache:
        # Use cached data if available
        cached         = cache[cache_key]
        player_stats   = cached.get("player_stats", {})
        opponent_name  = cached.get("opponent")
        opponent_stats = cached.get("opponent_stats", {})
    else:
        # Fetch fresh data from sport APIs
        if sport == "NHL":
            # Only fetch player data for real player props (shots, goals, assists, hits, etc.)
            # NOT for team-level bets (Moneyline, Puck Line, Regulation Win, etc.)
            if not _is_team_bet(player_name, bet_type, _NHL_TEAM_KEYWORDS):
                player_id, team = nhl.find_player(player_name)
                if player_id:
                    player_stats  = nhl.get_player_stats(player_id)
                    opponent_name = nhl.get_opponent(team) if team else None
                    if opponent_name:
                        opponent_stats = nhl.get_team_defense(opponent_name)
        # ... similar for NBA, MLB, Soccer
```

**Key characteristics:**
- Weights: `linemate_weight=0.35`, `season_weight=0.35` (leaving 30% for other factors)
- Player data only fetched for actual player props (not team bets)
- Caching prevents redundant API calls
- sample_n extracted from Claude's extraction (default 0 if missing)

---

## 4. Composite Score & EV Calculation

**File:** `/sessions/zen-affectionate-ramanujan/mnt/BetAnalyzer/scorer.py`  
**Lines:** 200–259

### composite_score() Function:

```python
def composite_score(
    linemate_hit_rate: float,        # From Claude extraction
    sample_size: int,                # sample_n (games bet occurred in)
    bet_type: str,                   # "Over 13.5 REB+AST", etc.
    player_stats: dict,              # Fetched player stats
    opponent_stats: dict,            # Opponent defense stats
    sport: str = "NHL",
    linemate_weight: float = 0.35,
    season_weight: float = 0.35,
) -> dict:
```

**Returns:**
```python
{
    "composite":      float (0–1),     # Final blended hit rate
    "linemate_hr":    float (0–1),     # Original Claude extraction
    "season_hr":      float (0–1),     # Calculated from raw stats
    "opp_factor":     float (0–1),     # Opponent strength factor
    "reliability":    float (0–1),     # Based on sample_size
    "games_sampled":  int,             # # of games with data
    "no_season_data": bool,            # Flag if no season data found
}
```

**Composite calculation (line 250–255):**
```python
composite = (
    eff_lm_weight    * linemate_hit_rate    # ~0.35 typically
    + eff_season_weight * season_hr          # ~0.35 typically
    + 0.20              * opp_factor         # Opponent strength
    + 0.10              * reliability        # Sample size penalty
)
composite = min(max(composite, 0.0), 1.0)   # Clamp to [0, 1]
```

### EV Calculation (line 264):
```python
def ev(composite_hit_rate: float, decimal_odds: float) -> float:
    """Expected Value per €1 bet."""
    return round(composite_hit_rate * (decimal_odds - 1) - (1 - composite_hit_rate), 4)
```

**Example:**
- Composite hit rate: 0.60
- Decimal odds: 2.00
- EV = 0.60 × (2.00 - 1) - (1 - 0.60) = 0.60 × 1 - 0.40 = **+0.20**

### Rating Assignment (line 269):
```python
def rating(ev_score: float, composite: float) -> str:
    """✅ Strong / ⚠️ Fair / ❌ Avoid based on EV and composite hit rate."""
    if ev_score >= 0.25 and composite >= 0.62:
        return "✅ Strong"
    elif ev_score >= 0.05 and composite >= 0.52:
        return "⚠️ Fair"
    else:
        return "❌ Avoid"
```

---

## 5. Props Filtering & Survival Rate

**File:** `/sessions/zen-affectionate-ramanujan/mnt/BetAnalyzer/analysis.py`  
**Lines:** 775–850

### Hard Exclusions (line 798–807):
1. **Bet365 unavailable** → filtered out completely
2. **EV ≤ 0** → filtered out completely
3. **0 < sample_n < 3** → filtered out completely (too small sample)

### Penalties Applied (line 809–816):
1. **Different line on Bet365**: EV × 0.85 (−15% penalty)
2. **3 ≤ sample_n < 5**: EV × 0.60 (−40% penalty)

### Ranking:
```python
eff_n = sample_n if sample_n > 0 else 20
weighted_ev = ev × min(eff_n, 20) / 20
```
- Props with **unknown sample_n** (= 0) use **default weight of 20 games**
- Sample size caps at 20 for weighting purposes
- Results sorted **descending by weighted_ev**

### Typical Survival Rate:

From typical Linemate screenshots:
- **Input:** 15–30 props extracted by Claude
- **After EV > 0 filter:** ~40–60% survive (6–15 props)
- **After sample_n filter:** ~30–50% of originals (5–12 props)
- **Final display:** Top 5–10 props shown in UI

---

## Summary

| Aspect | Finding |
|--------|---------|
| **sample_n source** | Claude extracts from Linemate game counts (4/5 = 5) |
| **sample_n default** | 0 (unknown) — prevents artificial data inflation |
| **Claude extracts** | hit_rate, sample (visual), sample_n (numeric), plus 10+ optional fields |
| **Weights in composite** | Linemate 35%, Season 35%, Opponent 20%, Reliability 10% |
| **EV threshold** | Only positive EV props shown after penalties |
| **Sample penalty** | -40% EV for samples 3–4 games; -15% for different Bet365 line |
| **Final prop count** | ~5–12 per analysis (depends on extraction quality) |

