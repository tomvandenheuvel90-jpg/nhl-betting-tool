"""
Screenshot Bet Import — Vision API extractie en bevestigingsscherm.

Gebruik:
    from screenshot_import import render_screenshot_import
    render_screenshot_import("geplaatste_bets", client=anthropic_client)
    render_screenshot_import("parlay",          client=anthropic_client)
"""

import base64
import json
import re
import uuid
import datetime

import streamlit as st

try:
    from analysis import lookup_player_team
except Exception:  # defensief — bij import-fouten functioneert de rest nog
    def lookup_player_team(_p: str, _s: str) -> str:
        return ""

IMPORT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = (
    "You are a sports betting data extraction assistant. "
    "Extract all bet data from the screenshot and return ONLY valid JSON. "
    "No explanation, no markdown, no code blocks. Raw JSON only."
)

# ── Gedeelde JSON-structuur ────────────────────────────────────────────────────

_JSON_SPEC = """Return ONLY this JSON (no other text):
{
  "bookmaker": "<betcity or bet365>",
  "bet_type": "<single | parlay | bet_builder>",
  "sport": "<MLB | NBA | NHL | NFL | Soccer | Tennis | Other>",
  "stake": <number>,
  "total_odds": <number>,
  "potential_payout": <number>,
  "currency": "EUR",
  "status": "open",
  "reference": "<reference code or null>",
  "legs": [
    {
      "description": "<full description as on screen>",
      "market": "<market type in English>",
      "selection": "<e.g. Over 0.5 | Under 15.5 | HC Pustertal>",
      "line": <number or null>,
      "odds": <decimal odds or null>,
      "player": "<player name or null>",
      "team": "<team name or null>"
    }
  ],
  "match": "<Team A vs Team B or null>",
  "game_date": "<YYYY-MM-DD or null>"
}

Rules:
- Odds must be DECIMAL format (e.g. 2.20 not +120)
- Use null for any field not visible on screen
- stake and potential_payout are plain numbers without currency symbol
- bet_builder = treat the same as parlay (multiple legs)
- "Meer dan X" = Over X  |  "Minder dan X" = Under X
- status: use "open" for a freshly placed bet, "gewonnen" if GEWONNEN/WON is visible, "verloren" if VERLOREN/LOST is visible"""

# ── Unified prompt (auto-detectie BetCity / Bet365) ──────────────────────────

_UNIFIED_PROMPT_TEMPLATE = """This screenshot is from a Dutch sports betting app — either BetCity or Bet365 NL.
Today's date is {today} (year {year}). Use this year for all date interpretation.

STEP 1 — Identify the bookmaker:
- BetCity:  "Weddenschap geplaatst" banner (green) + "Sessie XX:XX" top-left
            OR "ENKEL" / "COMBI" / "BET BUILDER" in bold caps
            → set bookmaker = "betcity"
- Bet365:   "HOERA!" heading with green checkmark circle + "Je weddenschap is geplaatst."
            OR "MIJN BETS" header with cyan "DUBBEL" / "TREBLE" labels
            → set bookmaker = "bet365"

══════════════════════════════════════════════════════════
BETCITY LAYOUT
══════════════════════════════════════════════════════════
- Bet type: "BET BUILDER" (multi-leg) / "ENKEL" (single) / "COMBI" (parlay)
- Match line: "Team A @ Team B  [total_odds]  €[stake]"
  (soccer: "Team A - Team B  [total_odds]  €[stake]")
- "Uitbetaling €X" = potential payout
- Reference: "Ref. XXXXXX" in green text
- For ENKEL: legs[0].odds = total_odds
- For BET BUILDER: individual leg odds NOT shown → legs[].odds = null
- Legs in GREEN BOLD text; Dutch market label in gray below each leg:

  MLB:   "Slagen +/-"→"Player Hits O/U"  "Werper - Outs +/-"→"Pitcher Outs O/U"
         "Totaal aantal honken +/-"→"Total Bases O/U"  "Homerun"→"Home Run"
         "Totaal runs +/-"→"Total Runs O/U"  "Strikeouts +/-"→"Pitcher Strikeouts O/U"
  Soccer:"Wedstrijduitslag"→"Match Result (1X2)"  "Beide teams scoren"→"Both Teams to Score"
         "Totaal doelpunten +/-"→"Total Goals O/U"  "Aantal hoekschoppen +/-"→"Total Corners O/U"
         "Eerste doelpuntenmaker"→"First Goalscorer"  "Doelpuntenmaker op enig moment"→"Anytime Goalscorer"
         "Dubbele kans"→"Double Chance"  "Gelijkspel geen weddenschap"→"Draw No Bet"
         "Handicap"→"Asian Handicap"  "Totaal gele kaarten +/-"→"Total Bookings O/U"
  NHL:   "Totaal doelpunten +/-"→"Total Goals O/U"  "Puck lijn"→"Puck Line"
         "Wedstrijduitslag (incl. OT)"→"Match Result (incl. OT)"
         "Wedstrijduitslag (reguliere tijd)"→"Match Result (Regular Time)"
         "Schoten op doel +/-"→"Player Shots on Goal O/U"
  NBA:   "Punten +/-"→"Player Points O/U"  "Rebounds +/-"→"Player Rebounds O/U"
         "Assists +/-"→"Player Assists O/U"  "Totaal punten +/-"→"Total Points O/U"
         "Puntenlijn"→"Point Spread"

- Parse player/line from green bold leg text:
    "Andy Pages - Meer dan 0.5 slagen"       → player="Andy Pages", selection="Over 0.5", line=0.5
    "Justin Wrobleski - Minder dan 15.5 outs" → player="Justin Wrobleski", selection="Under 15.5", line=15.5
    "Ajax"                                    → player=null, team="Ajax", selection="Ajax"
- Comma = decimal separator: "37,78" = 37.78

══════════════════════════════════════════════════════════
BET365 LAYOUT
══════════════════════════════════════════════════════════
SCREEN TYPE 1 — Single (ENKELVOUDIG):
- "HOERA!" + "ENKELVOUDIG" in cyan caps
- Match: two team names (e.g. "HC Pustertal / HK Olimpija Ljubljana")
- Clock + time: "Vandaag 19:45" → use {today}; always year {year}
- Selection + odds in cyan badge: e.g. "HC Pustertal  2.20" → total_odds
- Gray summary box: "Odds X.XX  |  Inzet €X  |  Potentiële uitbetaling €X"
- "ID XXXXXXXXXX" at bottom = reference
- legs: one leg with market, selection, odds = total_odds

SCREEN TYPE 2 — Parlay (DUBBEL / TREBLE / VIERVOUDIG):
- "HOERA!" + bet type in cyan caps
- Each leg: match name, clock+time, market, selection + individual leg odds in cyan badge
- ⚠️ CRITICAL: use the COMBINED odds from the gray summary box at the bottom as total_odds
  (NOT the individual leg odds)
- game_date = date of FIRST leg ("Vandaag" → {today}, always year {year})
- match = first two leg match names combined
- For parlay legs: each leg has its own odds in cyan badge → set in legs[].odds

SCREEN TYPE 3 — "MIJN BETS" overview:
- Extract the FIRST/topmost bet only
- status = "open" (unless "GEWONNEN" / "VERLOREN" badge visible)

SCREEN TYPE 4 — Settled:
- "GEWONNEN" → status = "gewonnen"
- "VERLOREN" → status = "verloren"

Bet365 Dutch → English mappings:
- bet_type: "ENKELVOUDIG"→"single"  "DUBBEL"→"parlay"  "TREBLE"→"parlay"  "VIERVOUDIG"→"parlay"
- selections: "Meer dan X"→"Over X"  "Minder dan X"→"Under X"  "Thuis"→"Home"  "Uit"→"Away"
- markets: "Match Odds - Regular Time"→"Match Result (Regular Time)"
  "Match Odds - Incl. Overtime"→"Match Result (incl. OT)"
  "Wedstrijduitslag"→"Match Result (1X2)"  "Beide Teams Scoren"→"Both Teams to Score"
  "Totaal Doelpunten"→"Total Goals O/U"  "Asian Handicap"→"Asian Handicap"
  "Dubbele Kans"→"Double Chance"  "Gelijkspel Geen Weddenschap"→"Draw No Bet"
  "Hoekschoppen Over/Under"→"Total Corners O/U"
  "Strikeouts Geworden door Speler - Inclusief Extra Innings"→"Pitcher Strikeouts O/U"
  "Slagen"→"Player Hits O/U"  "Totaal Honken"→"Total Bases O/U"
  "Werper Outs"→"Pitcher Outs O/U"  "Punten"→"Player Points O/U"
  "Rebounds Over/Under"→"Player Rebounds O/U"  "Assists Over/Under"→"Player Assists O/U"
  "Totaal Punten"→"Total Points O/U"  "Schoten Op Doel"→"Player Shots on Goal O/U"
  "Eerste Doelpuntenmaker"→"First Goalscorer"  "Doelpuntenmaker Op Enig Moment"→"Anytime Goalscorer"
- "Inzet" = stake, "Potentiële uitbetaling" = potential_payout
- Comma = decimal separator: "39,98" = 39.98
- ALWAYS use year {year} for all dates.

"""

# ─── Vision API ───────────────────────────────────────────────────────────────

def _detect_mime(data: bytes) -> str:
    if data[:4] == b'\x89PNG':
        return "image/png"
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    return "image/jpeg"


def extract_bet_from_screenshot(client, image_bytes: bytes, bookmaker: str = "auto") -> dict:
    """
    Stuur screenshot naar Vision API en retourneer parsed JSON dict.
    bookmaker : "auto" (default, laat model detecteren) | "betcity" | "bet365"
    """
    mime  = _detect_mime(image_bytes)
    b64   = base64.standard_b64encode(image_bytes).decode()

    today = datetime.date.today()
    # Unified prompt — model detecteert bookmaker zelf uit de screenshot
    prompt = _UNIFIED_PROMPT_TEMPLATE.format(today=today.isoformat(), year=today.year) + _JSON_SPEC

    resp = client.messages.create(
        model=IMPORT_MODEL,
        max_tokens=4096,
        temperature=0,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()

    # Als het model tekst voor/na de JSON heeft gezet, extraheer alleen het JSON-gedeelte
    if '{' in raw and '}' in raw:
        raw = raw[raw.index('{'):raw.rindex('}') + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Ongeldige JSON van Vision API: {exc}\n\nRaw response:\n{raw[:600]}"
        ) from exc


# ─── Session-state helpers ────────────────────────────────────────────────────

def _sk(context: str, suffix: str) -> str:
    return f"sc_{context}_{suffix}"


def _init_state(context: str) -> None:
    defaults = [("state", "idle"), ("data", None), ("saving", False)]
    for k, v in defaults:
        if _sk(context, k) not in st.session_state:
            st.session_state[_sk(context, k)] = v


# ─── Publieke functie ─────────────────────────────────────────────────────────

def render_screenshot_import(context: str, client=None) -> None:
    """
    Render de '📸 Importeer via screenshot' sectie.

    context : "geplaatste_bets" | "parlay" | "shortlist"
    client  : anthropic.Anthropic instantie
    """
    import db

    _init_state(context)
    state = st.session_state[_sk(context, "state")]

    if state == "idle":
        _render_upload(context, client)
    elif state == "confirm":
        _render_confirmation(context, db)


# ─── Upload-paneel ────────────────────────────────────────────────────────────

def _render_upload(context: str, client) -> None:
    with st.expander("📸 Importeer via screenshot", expanded=False):
        st.caption(
            "Upload een bevestigingsscherm van Bet365 of BetCity. "
            "De bookmaker wordt automatisch herkend. "
            "Je kunt de gegevens nog aanpassen voor je opslaat."
        )

        uploaded = st.file_uploader(
            "Kies een PNG of JPG (max 10 MB)",
            type=["png", "jpg", "jpeg"],
            key=_sk(context, "uploader"),
        )
        if uploaded is None:
            return

        mb = uploaded.size / 1_048_576
        if mb > 10:
            st.error("⛔ Bestand groter dan 10 MB. Kies een kleinere screenshot.")
            return
        if mb > 5:
            st.warning(f"⚠️ Groot bestand ({mb:.1f} MB). Verwerking duurt iets langer.")

        if st.button(
            "📸 Analyseer screenshot",
            type="primary", key=_sk(context, "analyze_btn"),
        ):
            if client is None:
                st.error("Anthropic API niet beschikbaar. Controleer de API-sleutel.")
                return

            img_bytes = uploaded.read()
            with st.spinner("Screenshot analyseren… even geduld."):
                last_exc = None
                for attempt in range(2):
                    try:
                        data = extract_bet_from_screenshot(client, img_bytes)
                        st.session_state[_sk(context, "data")]  = data
                        st.session_state[_sk(context, "state")] = "confirm"
                        st.rerun()
                        return
                    except Exception as exc:
                        last_exc = exc
                        if attempt == 0:
                            continue
                st.error("❌ Kon screenshot niet uitlezen. Probeer opnieuw of voer handmatig in.")
                if last_exc is not None:
                    with st.expander("🔍 Foutdetails (voor debug)", expanded=True):
                        st.code(str(last_exc), language=None)


# ─── Bevestigingsscherm ───────────────────────────────────────────────────────

def _render_confirmation(context: str, db) -> None:
    data = st.session_state[_sk(context, "data")] or {}

    st.markdown("---")
    st.markdown("### 📸 Screenshot — Controleer & Bevestig")

    # ── Visuele indicators: geef aan welke velden herkend zijn ──────────────
    bm     = data.get("bookmaker", "unknown")
    bt_raw = data.get("bet_type", "single")
    bm_lbl = {"bet365": "🟢 Bet365", "betcity": "🔵 BetCity"}.get(bm, "❓ Onbekend")
    bt_lbl = {"single": "Single", "parlay": "Parlay", "bet_builder": "Bet Builder"}.get(bt_raw, bt_raw or "—")

    _has_match  = bool(data.get("match"))
    _has_odds   = bool(data.get("total_odds"))
    _has_stake  = bool(data.get("stake"))

    _missing = []
    if not _has_match:  _missing.append("Wedstrijd / Event")
    if not _has_odds:   _missing.append("Odds")
    if not _has_stake:  _missing.append("Inzet")

    if _missing:
        st.warning(
            f"⚠️ Niet herkend uit screenshot — controleer: **{', '.join(_missing)}**"
        )
    else:
        st.success("✅ Alle velden uitgelezen uit screenshot — controleer kort voor je opslaat.")

    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Bookmaker:** {bm_lbl}")
    c2.markdown(f"**Type:** {bt_lbl}")
    c3.markdown(f"**Sport:** {data.get('sport', '—')}")

    _match_label = "✅ Wedstrijd / Event" if _has_match else "⚠️ Wedstrijd / Event (niet herkend)"
    _match = st.text_input(
        _match_label,
        value=data.get("match") or "",
        key=_sk(context, "match"),
        placeholder="bijv. NY Mets @ LA Dodgers",
    )

    # Odds / Inzet / Uitbetaling
    col1, col2, col3 = st.columns(3)
    _odds_label  = "✅ Odds (totaal)"  if _has_odds  else "⚠️ Odds (niet herkend)"
    _stake_label = "✅ Inzet (€)"      if _has_stake else "⚠️ Inzet (niet herkend)"
    _odds = col1.number_input(
        _odds_label, min_value=1.01, max_value=10_000.0,
        value=float(data.get("total_odds") or 2.0),
        step=0.05, format="%.2f", key=_sk(context, "odds"),
    )
    _stake = col2.number_input(
        _stake_label, min_value=0.01, max_value=100_000.0,
        value=float(data.get("stake") or 10.0),
        step=1.0, format="%.2f", key=_sk(context, "stake"),
    )
    col3.metric("Potentiële uitbetaling", f"€ {_odds * _stake:.2f}")

    col4, col5, col6 = st.columns(3)
    _status = col4.selectbox(
        "Status", ["open", "gewonnen", "verloren"],
        key=_sk(context, "status"),
    )
    _ref = col5.text_input(
        "Referentie / ID",
        value=data.get("reference") or "",
        key=_sk(context, "ref"),
    )

    _sport_opts = ["MLB", "NBA", "NHL", "Soccer", "NFL", "Tennis", "Overig"]
    _sport_raw  = data.get("sport") or "Overig"
    if _sport_raw == "Other":
        _sport_raw = "Overig"
    _sport_idx = _sport_opts.index(_sport_raw) if _sport_raw in _sport_opts else len(_sport_opts) - 1
    _sport = col6.selectbox("Sport", _sport_opts, index=_sport_idx, key=_sk(context, "sport"))

    _date_auto = False
    try:
        _gd_default = datetime.date.fromisoformat(data.get("game_date") or "")
        if _gd_default.year < datetime.date.today().year:
            # Model heeft verkeerd jaar ingevuld → gebruik vandaag als fallback
            _gd_default = datetime.date.today()
        else:
            _date_auto = True
    except (ValueError, TypeError):
        _gd_default = datetime.date.today()
    _date_label = "✅ Wedstrijddatum (uit screenshot)" if _date_auto else "⚠️ Wedstrijddatum (niet herkend — controleer)"
    _game_date = st.date_input(_date_label, value=_gd_default, key=_sk(context, "game_date"))

    # ── Legs (altijd tonen voor parlay/bet_builder) ───────────────────────────
    legs_raw    = data.get("legs") or []
    edited_legs = []

    if legs_raw:
        n_legs = len(legs_raw)
        st.markdown(f"**{'Leg' if n_legs == 1 else 'Legs'} ({n_legs}):**")
        for i, leg in enumerate(legs_raw):
            label = leg.get("description") or f"Leg {i + 1}"
            with st.expander(f"Leg {i + 1}: {label}", expanded=(n_legs == 1)):
                _ld = st.text_input("Beschrijving", value=leg.get("description") or "",
                                    key=_sk(context, f"leg_desc_{i}"))
                lc1, lc2 = st.columns(2)
                _lm = lc1.text_input("Markt", value=leg.get("market") or "",
                                     key=_sk(context, f"leg_market_{i}"))
                _ls = lc2.text_input("Selectie", value=leg.get("selection") or "",
                                     key=_sk(context, f"leg_sel_{i}"))
                lc3, lc4, lc5, lc6 = st.columns(4)
                _lo = lc3.number_input("Odds", min_value=0.0, max_value=1000.0,
                                       value=float(leg.get("odds") or 0), step=0.05, format="%.2f",
                                       key=_sk(context, f"leg_odds_{i}"))
                _ll = lc4.number_input("Line", value=float(leg.get("line") or 0), step=0.5,
                                       key=_sk(context, f"leg_line_{i}"))
                _lp = lc5.text_input("Speler", value=leg.get("player") or "",
                                     key=_sk(context, f"leg_player_{i}"))
                _lt = lc6.text_input("Team", value=leg.get("team") or "",
                                     key=_sk(context, f"leg_team_{i}"))
                edited_legs.append({
                    "description": _ld, "market": _lm, "selection": _ls,
                    "odds":        _lo if _lo > 0 else None,
                    "line":        _ll if _ll != 0 else None,
                    "player":      _lp or None,
                    "team":        _lt or None,
                })
    else:
        st.info("Geen legs herkend — wordt als single bet opgeslagen.")

    # ── Odds Boost ────────────────────────────────────────────────────────────
    st.markdown("---")
    boost_col1, boost_col2 = st.columns([1, 3])
    _boost = boost_col1.number_input(
        "🚀 Odds Boost (%)",
        min_value=0, max_value=200, value=0, step=5,
        key=_sk(context, "boost"),
        help="Vul in als Bet365 een odds boost heeft toegepast (bijv. 30 = +30%). Laat 0 als er geen boost is.",
    )
    if _boost > 0:
        _boosted_odds = round(_odds * (1 + _boost / 100), 2)
        boost_col2.markdown(
            f"<div style='padding-top:28px'>Originele odds: <b>{_odds:.2f}</b> → "
            f"Na boost (+{_boost}%): <b style='color:#22c55e'>{_boosted_odds:.2f}</b> "
            f"· Uitbetaling: <b>€{_boosted_odds * _stake:.2f}</b></div>",
            unsafe_allow_html=True,
        )
    else:
        _boosted_odds = _odds

    st.markdown("---")

    # Blokkeer dubbele opslag: zodra saving=True de knop niet meer tonen
    if st.session_state.get(_sk(context, "saving")):
        with st.spinner("Opslaan… even geduld."):
            st.stop()

    btn1, btn2, btn3 = st.columns([3, 2, 1])
    confirm = btn1.button("✅ Opslaan", type="primary", key=_sk(context, "confirm"))
    _       = btn2.button("✏️ Annuleren",              key=_sk(context, "cancel"))
    cancel  = _ or btn3.button("✖",                   key=_sk(context, "cancel2"))

    if confirm:
        st.session_state[_sk(context, "saving")] = True
        with st.spinner("Opslaan… even geduld."):
            _do_save(context, db, data, edited_legs,
                     _boosted_odds, _stake, _status, _sport, _game_date, _match, _ref,
                     odds_boost_pct=_boost)
        st.session_state[_sk(context, "saving")] = False

    if cancel:
        st.session_state[_sk(context, "state")] = "idle"
        st.session_state[_sk(context, "data")]  = None
        st.rerun()


# ─── Opslaan ─────────────────────────────────────────────────────────────────

def _do_save(context, db, data, edited_legs, odds, stake, status,
             sport, game_date, match, ref, odds_boost_pct: int = 0):
    bm      = data.get("bookmaker", "unknown")
    bt_raw  = data.get("bet_type", "single")
    is_multi = bt_raw in ("parlay", "bet_builder") or len(edited_legs) > 1

    # Beschrijving en spelernaam
    if edited_legs:
        if len(edited_legs) == 1:
            desc   = edited_legs[0].get("description") or match or "Screenshot import"
            player = edited_legs[0].get("player") or match or "—"
        else:
            desc   = f"Parlay ({len(edited_legs)} legs)"
            player = match or "Parlay"
    else:
        desc   = match or "Screenshot import"
        player = match or "—"

    # Team voor bet_obj: gebruik wat Claude Vision uit de screenshot haalt.
    # Geen live API-lookup — die is traag en niet kritiek voor opslaan.
    if edited_legs and len(edited_legs) == 1:
        _bet_team = (edited_legs[0].get("team") or "").strip()
    else:
        _bet_team = ""

    # Gedeeld bet-object (voor add_favoriet / upsert_resultaat)
    bet_obj = {
        "player":          player,
        "bet_type":        desc,
        "sport":           sport,
        "odds":            round(odds, 2),
        "ev":              0.0,
        "team":            _bet_team,
        "bet365":          {},
        "import_method":   "screenshot",
        "bookmaker":       bm,
        "speler":          player,
        "bet":             desc,
        "datum":           game_date.isoformat(),
        "game_date":       game_date.isoformat(),
        "ev_score":        0.0,
        "rating":          "",
        "composite":       0.0,
        "odds_boost_pct":  odds_boost_pct,
    }

    # ── Geplaatste Bets: direct opslaan in resultaten ────────────────────────
    if context == "geplaatste_bets":
        if is_multi and edited_legs:
            ok = _save_as_parlay(db, edited_legs, odds, stake, status, sport,
                                 game_date, match, bm, bet_obj)
        else:
            ok = _save_as_single(db, player, desc, bet_obj, status, stake)
        if ok:
            _reset(context)
        return

    # ── Shortlist: opslaan als favoriet + resultaat ──────────────────────────
    if context == "shortlist":
        fav_id = db.make_fav_id(player, desc)
        try:
            db.add_favoriet(fav_id, bet_obj, game_date=game_date.isoformat())
            db.upsert_resultaat(fav_id, bet_obj, status, stake)
            st.toast("✅ Weddenschap opgeslagen in Shortlist!", icon="✅")
        except Exception as exc:
            st.error(f"Fout bij opslaan: {exc}")
            return
        _reset(context)
        return

    # ── Parlay Builder: direct opslaan als geplaatste weddenschap ────────────
    if context == "parlay":
        if is_multi and edited_legs:
            ok = _save_as_parlay(db, edited_legs, odds, stake, status, sport,
                                 game_date, match, bm, bet_obj)
        else:
            ok = _save_as_single(db, player, desc, bet_obj, status, stake)
        if ok:
            _reset(context)
        return


def _save_as_single(db, player, desc, bet_obj, status, stake):
    fav_id = db.make_fav_id(player, desc)
    try:
        db.upsert_resultaat(fav_id, bet_obj, status, float(stake))
        st.toast("✅ Weddenschap opgeslagen in Geplaatste Bets!", icon="✅")
        return True
    except Exception as exc:
        st.error(f"Fout bij opslaan: {exc}")
        return False


def _save_as_parlay(db, edited_legs, odds, stake, status, sport,
                    game_date, match, bm, bet_obj):
    parlay_id = uuid.uuid4().hex[:8]

    props_json = []
    legs_json  = {}
    for i, leg in enumerate(edited_legs):
        _player   = (leg.get("player") or "").strip()
        _team     = (leg.get("team") or "").strip()
        _market   = leg.get("market") or "Player Prop"
        _sel      = leg.get("selection") or ""
        # Voorkom dubbele teamnaam bij match-niveau bets: als de selection
        # gelijk is aan de teamnaam/speler, gebruik alleen de markt.
        _sel_clean = (_sel or "").strip().lower()
        _pl_clean  = (_player or "").strip().lower()
        if _sel_clean and _pl_clean and _sel_clean == _pl_clean:
            _bet_type = _market
        else:
            _bet_type = f"{_market} — {_sel}" if _sel else _market
        _raw_odds = leg.get("odds")
        _leg_odds = float(_raw_odds) if _raw_odds is not None else None

        props_json.append({
            "player":   _player,
            "team":     _team,
            "sport":    sport,
            "bet_type": _bet_type,
            "odds":     _leg_odds,
            "hit_rate": None,
        })
        legs_json[f"{i}_{_player}_{_bet_type}"] = "open"

    if status == "gewonnen":
        wl = round(float(stake) * (odds - 1), 2)
    elif status == "verloren":
        wl = round(-float(stake), 2)
    else:
        wl = 0.0

    parlay_dict = {
        "id":                 parlay_id,
        "datum":              game_date.isoformat(),
        "props_json":         props_json,
        "gecombineerde_odds": round(odds, 4),
        "hit_kans":           None,
        "ev_score":           None,
        "inzet":              float(stake),
        "uitkomst":           status,
        "winst_verlies":      wl,
        "legs_json":          legs_json,
    }

    try:
        db.save_parlay(parlay_dict)
        # Sla ook op in resultaten voor P&L tracking
        prl_fav = {
            **bet_obj,
            "speler":    f"🎰 Parlay ({len(edited_legs)} legs)",
            "bet":       ", ".join(l.get("player") or "" for l in edited_legs[:3]) or "Parlay",
            "sport":     "Parlay",
        }
        db.upsert_resultaat(f"parlay_{parlay_id}", prl_fav, status, float(stake))
        st.toast("✅ Parlay opgeslagen in Geplaatste Bets!", icon="✅")
        return True
    except Exception as exc:
        st.error(f"Fout bij opslaan: {exc}")
        return False


def _reset(context: str) -> None:
    st.session_state[_sk(context, "state")]  = "idle"
    st.session_state[_sk(context, "data")]   = None
    st.session_state[_sk(context, "saving")] = False
    st.rerun()
