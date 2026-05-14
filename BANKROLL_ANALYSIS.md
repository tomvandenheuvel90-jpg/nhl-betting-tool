# BetAnalyzer Bankroll System — Complete Analysis

**Date:** 2026-04-03  
**Codebase Location:** `/sessions/zen-affectionate-ramanujan/mnt/BetAnalyzer/`  
**Project Owner:** Tom van den Heuvel

---

## 1. CURRENT BANKROLL IMPLEMENTATION

### 1.1 Data Layer (`db.py`)

#### Settings Storage (Lines 506–537)
- **File:** `settings.json` (in project root)
- **Content:** Simple key-value JSON, currently stores only:
  ```json
  {
    "start_bankroll": 1000.00
  }
  ```
- **Functions:**
  - `load_settings()` → dict
  - `get_setting(key, default)` → value
  - `set_setting(key, value)` → saves to JSON

#### Resultaten Table (Lines 28–35, 344–415)
**Supabase schema:**
```sql
CREATE TABLE resultaten (
    id TEXT PRIMARY KEY,
    datum TEXT,
    speler TEXT,
    bet TEXT,
    odds REAL,
    inzet REAL,          -- stake/wager
    uitkomst TEXT,       -- "gewonnen", "verloren", "open"
    winst_verlies REAL,  -- profit/loss amount
    sport TEXT,
    ev_score REAL,
    is_parlay BOOLEAN,
    source_session_id TEXT
);
```

**Local fallback:** `resultaten.json` (same structure as list of dicts)

**Key functions:**
- `load_resultaten()` → list of bet dicts
- `upsert_resultaat(id, bet_dict, outcome, stake)` → inserts/updates with P&L calc
- `remove_resultaat(id)` → deletes a bet

**P&L Calculation (line 372):**
```python
wl = round(inzet * (odds - 1), 2) if uitkomst == "gewonnen" else round(-inzet, 2)
# Example: €10 @ 2.5 odds = win €15, loss €10
```

#### Parlays Table (separate, not detailed here)
- When a parlay is settled (won/lost), it's also saved to `resultaten` with `id = f"parlay_{parlay_id}"`

### 1.2 UI Layer (`streamlit_app.py`)

#### Dashboard Tab (Lines 291, 339)
```python
_dsh_start_bk = float(db.get_setting("start_bankroll") or 0.0)
_dsh_total_wl = sum(r.get("winst_verlies", 0) for r in resultaten)
_dsh_balance = _dsh_start_bk + _dsh_total_wl

# Display:
# If start_bk > 0: "start €1000  ·  P&L +€250"
# Otherwise:       "Stel startbankroll in via Bankroll tab"
```

#### Bankroll Tab (Lines 999–1750+)

**Header Section (Lines 1057–1082):**
- Displays "Total Balance" in large purple card
- Shows 7-day P&L with color coding (green if positive, red if negative)
- Calculation: `balance = start_bankroll + sum(all_winst_verlies) if start_bankroll > 0`

**Period Selector (Lines 1084–1100):**
- Radio buttons: "7 Dagen", "Maand", "All Time"
- Stored in `st.session_state.bk_view`

**P&L Graph Section:**
- X-axis: date ranges
- Y-axis: cumulative P&L or balance
- Filtered by period selection

**Bets Table / Bet Details (Lines 1300+):**
- Lists all settled bets (outcome = "gewonnen" or "verloren")
- Columns: Date, Player, Bet, Sport, Odds, Stake, Outcome, P&L
- Collapsible details per bet

**Settings Section (Lines 1428–1439):**
```python
_start_bk_saved = float(db.get_setting("start_bankroll") or 0.0)
with st.expander("⚙️ Bankroll instellingen", expanded=(_start_bk_saved == 0)):
    _start_bk_input = st.number_input("Startbankroll (€)", ...)
    if st.button("💾 Opslaan"):
        db.set_setting("start_bankroll", float(_start_bk_input))
        st.rerun()
```

**Filters (Lines 1448–1475):**
- Sport: "Alles", "NHL", "NBA", "MLB", "Voetbal"
- Bet type: "Alles", "Goals", "Assists", "Shots on Goal", etc.
- Period: "Alles", "Laatste 7 dagen", "Laatste 30 dagen"
- Type: "Alles", "Singles", "Parlays"

**Kelly Calculator (Line 1749+):**
- Separate section for bankroll management strategy

---

## 2. CURRENT LIMITATIONS

### 2.1 No Cash Flow Tracking
**Problem:** Only `start_bankroll` + cumulative P&L
- If user deposits €500 mid-month, no way to record it
- If user withdraws €200 for rent, no way to record it
- Bankroll becomes **inaccurate** if cash moves

**Impact:** 
- Discrepancy between actual balance and calculated balance
- Misleading performance metrics
- Can't track "true" ROI

### 2.2 No Transaction History
**Problem:** No table for deposits, withdrawals, or adjustments
- No audit trail of cash movements
- Can't see "when did I add money?"
- No explanation for sudden balance changes

### 2.3 No Manual Balance Adjustment
**Problem:** If calculated balance disagrees with actual, no way to fix it
- User adds €100 in person (not via app) → balance off by €100 forever
- User loses €50 in cash gambling (not via app) → balance off
- Can't "reset" to actual balance without deleting all bets

### 2.4 Limited Reporting
**Problem:** Filters exist but reporting is basic
- No ROI per sport
- No P&L per opponent
- No heat map of riskiest periods
- No variance/volatility analysis
- No "worst loss" or "best win" tracking

---

## 3. FILE STRUCTURE REFERENCE

```
/sessions/zen-affectionate-ramanujan/mnt/BetAnalyzer/
├── streamlit_app.py           (1780 lines, entry point)
│   ├── Tab: Dashboard          (lines ~250–340)
│   ├── Tab: Analyse            (lines ~340–998)
│   ├── Tab: Shortlist          (lines ~998–1000) [est.]
│   ├── Tab: Parlay Builder     (lines ~1000–1000) [est.]
│   ├── Tab: Geplaatste Bets    (lines ~1000–1428)
│   ├── Tab: Bankroll           (lines 999–1750+)  ← YOU ARE HERE
│   └── Tab: Geschiedenis       (lines ~1750–end)
│
├── db.py                       (553 lines, data layer)
│   ├── Settings mgmt           (lines 506–537)
│   ├── Resultaten load/save    (lines 344–415)
│   ├── Parlays (separate)      (not detailed)
│   └── Supabase + JSON fallback
│
├── analysis.py                 (777 lines)
├── match_analysis.py           (620 lines)
├── scorer.py                   (276 lines)
├── ui_components.py            (499 lines)
├── styles.py                   (328 lines)
├── prompts.py                  (constants)
│
├── sports/                     (module per sport)
│   ├── nhl.py
│   ├── nba.py
│   ├── mlb.py
│   ├── soccer.py
│   ├── cache.py
│   ├── rate_limiter.py
│   └── moneypuck_local.py
│
├── settings.json               (start_bankroll only)
├── resultaten.json             (fallback if Supabase down)
├── .streamlit/secrets.toml     (API keys, database creds)
└── requirements.txt
```

---

## 4. DATA FLOW FOR BANKROLL CALCULATION

```
User places a bet
    ↓
Favorite saved: db.save_favorite(bet_dict)
    ↓
User marks as "Placed" (moves to 📋 tab)
    ↓
User clicks "✅ Win" or "❌ Loss"
    ↓
db.upsert_resultaat(id, bet_dict, outcome="gewonnen"/"verloren", stake)
    ├─ Calculates: wl = inzet * (odds - 1) if won, else -inzet
    └─ Stores in resultaten table
    ↓
Dashboard/Bankroll tab reads:
    ├─ start_bankroll from settings.json
    ├─ All resultaten records
    ├─ Calculates: balance = start_bankroll + sum(all winst_verlies)
    └─ Displays in header card

Bankroll Tab also:
    ├─ Filters by period (7d, 30d, all-time)
    ├─ Renders P&L graph
    ├─ Shows per-sport ROI (aggregated from resultaten)
    └─ Allows Kelly Calculator input
```

---

## 5. EXAMPLE RECORDS

### settings.json
```json
{
  "start_bankroll": 1000.00
}
```

### resultaten.json (single record example)
```json
{
  "id": "fav_20260403_001",
  "datum": "2026-04-03",
  "speler": "Connor McDavid",
  "bet": "Anytime Goal Scorer",
  "odds": 2.15,
  "inzet": 50.00,
  "uitkomst": "gewonnen",
  "winst_verlies": 57.50,
  "sport": "NHL",
  "ev_score": 0.085,
  "is_parlay": false,
  "source_session_id": "abc123"
}
```

### Calculation Example
```
Start bankroll: €1000
Bet 1: €50 @ 2.15 odds → won → +€57.50
Bet 2: €25 @ 1.85 odds → lost → −€25.00
Bet 3: €100 @ 3.00 odds → won → +€200.00

Total balance = €1000 + €57.50 − €25.00 + €200.00 = €1232.50
7-day P&L = €57.50 − €25.00 + €200.00 = €232.50
```

---

## 6. KEY CODE REFERENCES

### Getting Current Balance (streamlit_app.py line 291)
```python
_dsh_start_bk = float(db.get_setting("start_bankroll") or 0.0)
_bk_all_settled = [r for r in db.load_resultaten() 
                   if r.get("uitkomst") in ("gewonnen", "verloren")]
_bk_total_wl = sum(r.get("winst_verlies", 0) for r in _bk_all_settled)
_bk_balance = _dsh_start_bk + _bk_total_wl if _dsh_start_bk > 0 else None
```

### Settling a Bet (streamlit_app.py ~line 1350)
```python
if st.button("✅ Win"):
    db.upsert_resultaat(bet_id, bet_dict, "gewonnen", stake_amount)
    st.rerun()
```

### P&L Calculation (db.py line 372)
```python
odds = float(fav.get("odds", 1.0))
wl = round(inzet * (odds - 1), 2) if uitkomst == "gewonnen" else round(-inzet, 2)
```

---

## 7. PROPOSED ENHANCEMENT OPTIONS

### Option A: Simple Deposits/Withdrawals (Est. 2–3 hours)

**Add to db.py:**
```python
TRANSACTIONS_TABLE = "transactions"  # or transactions.json

def load_transactions() -> list:
    # Returns: [{"id": "tx_001", "datum": "2026-04-03", "type": "deposit", 
    #           "amount": 500.00, "notes": "Reinvestment"}]
    pass

def add_transaction(type_: str, amount: float, notes: str = "") -> None:
    # type_ in ["deposit", "withdrawal", "adjustment"]
    pass
```

**Update calculation:**
```python
balance = start_bankroll + sum(transactions) + sum(P&L)
```

**UI Changes (Bankroll tab):**
- Add "➕ Deposit" button
- Add "➖ Withdrawal" button
- Show transaction history table
- Transaction dialog with date/amount/notes

**Pro:** Simple, directly addresses cash flow issue
**Con:** No audit trail, no ability to flag "suspicious" adjustments

---

### Option B: Manual Balance Reset (Est. 4–6 hours)

Option A + ability to set "true balance":

**Add to db.py:**
```python
def set_balance_adjustment(calculated_balance: float, actual_balance: float, reason: str) -> None:
    # Stores: {"datum": today, "discrepancy": actual - calculated, "reason": reason}
    pass
```

**UI Changes:**
- New section: "Balance Reconciliation"
- Shows: "Calculated: €1234.56 | Actual: €1300.00 | Diff: +€65.44"
- Option to record adjustment with reason
- Adjustment is treated as a "transaction" with type="balance_adjustment"

**Pro:** Handles discrepancies, audit trail
**Con:** Still manual, doesn't prevent future discrepancies

---

### Option C: Advanced Bankroll Risk Management (Est. 8+ hours)

Option B + risk metrics:

**New Features:**
1. **Bet Size Warning:** Flag if any single bet > X% of current bankroll (e.g., 5%)
2. **Exposure Tracker:** Show total risk (sum of open bets) vs. available bankroll
3. **Variance Analysis:** Show win/loss streaks, volatility
4. **Heat Map:** Calendar view of P&L per day
5. **ROI Benchmarking:** Compare to Kelly Criterion optimal sizing
6. **Scenario Simulator:** "If I had bet this amount instead..." calculation

**Pro:** Comprehensive risk management, helps prevent overexposure
**Con:** Requires significant UI redesign, more complex queries

---

## 8. RECOMMENDATIONS

**For Tom (non-technical owner):**
1. **Start with Option A** (deposits/withdrawals) — solves the main problem (cash flow)
2. **Then add Option B** (balance reset) for peace of mind
3. **Option C** (risk management) is nice-to-have but less urgent

**Implementation notes:**
- ✅ All changes are **backward-compatible** (add table, don't break existing)
- ✅ Supabase + JSON fallback both work the same way
- ✅ No changes needed to settled bets (resultaten) — only add new transaction table
- ✅ Testing: verify balance calculation with deposits/withdrawals in different order

---

## 9. TEST SCENARIOS

Once implemented, verify:
1. **Initial setup:** Start €1000, deposit €500 → balance = €1500
2. **Bet settlement:** Place €100 bet @ 2.0 odds, win → balance = €1600 (€1500 + €100 profit)
3. **Withdrawal:** Withdraw €200 → balance = €1400
4. **Loss:** Place €50 bet @ 1.5 odds, lose → balance = €1350 (€1400 − €50)
5. **Balance reset:** Set actual balance = €1400 → discrepancy recorded, balance adjusted
6. **Parlay in resultaten:** Settlement of parlay also appears in resultaten table with correct P&L

---

**Questions for clarification (ask Tom):**
1. Do you ever withdraw money from the betting account? (confirms need for deposits/withdrawals feature)
2. Have you ever had a discrepancy between the app balance and actual balance? (confirms need for reset feature)
3. What's the maximum % of bankroll you'd want to risk on a single bet? (for risk warnings)

