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

# ── BetCity prompt ────────────────────────────────────────────────────────────

_BETCITY_PROMPT = """This is a BetCity NL screenshot. Extract the bet data.

BetCity confirmation screen layout:
- "Sessie XX:XX" top-left, "Vergunninghouder Kansspelautoriteit" top-right
  → these confirm it is BetCity Netherlands
- Green banner: "Weddenschap geplaatst" with green checkmark
- Reference: "Ref. XXXXXX" in green text below the banner (e.g. Ref. YL8270949631W)
- Bet type in bold caps: "BET BUILDER" (multi-leg) or "ENKEL" (single) or "COMBI" (parlay)
- Match line: "Team A @ Team B  [total_odds]  €[stake]"  (e.g. "NY Mets @ LA Dodgers  3.25  €37,78")
  OR for football/soccer: "Team A - Team B  [total_odds]  €[stake]"
- "Uitbetaling €X" on its own line = potential payout
- For ENKEL (single): legs[].odds is the same as total_odds
- Legs in GREEN BOLD text, Dutch market label in gray below each leg:

  MLB market labels (gray, below leg):
    "Slagen +/-"                     → market = "Player Hits O/U"
    "Werper - Outs +/-"              → market = "Pitcher Outs O/U"
    "Totaal aantal honken +/-"       → market = "Total Bases O/U"
    "Homerun"                        → market = "Home Run"
    "Geslagen honkslag"              → market = "Player Hits"
    "Runs gescoord"                  → market = "Runs Scored"
    "Totaal runs +/-"                → market = "Total Runs O/U"
    "Strikeouts +/-"                 → market = "Pitcher Strikeouts O/U"

  Soccer / Football market labels:
    "Wedstrijduitslag"               → market = "Match Result (1X2)"
    "Beide teams scoren"             → market = "Both Teams to Score"
    "Totaal doelpunten +/-"          → market = "Total Goals O/U"
    "Aantal hoekschoppen +/-"        → market = "Total Corners O/U"
    "Eerste doelpuntenmaker"         → market = "First Goalscorer"
    "Doelpuntenmaker op enig moment" → market = "Anytime Goalscorer"
    "Dubbele kans"                   → market = "Double Chance"
    "Gelijkspel geen weddenschap"    → market = "Draw No Bet"
    "Handicap"                       → market = "Asian Handicap"
    "Totaal gele kaarten +/-"        → market = "Total Bookings O/U"

  Ice Hockey / NHL market labels:
    "Totaal doelpunten +/-"          → market = "Total Goals O/U"
    "Puck lijn"                      → market = "Puck Line"
    "Wedstrijduitslag (incl. OT)"    → market = "Match Result (incl. OT)"
    "Wedstrijduitslag (reguliere tijd)" → market = "Match Result (Regular Time)"
    "Eerste doelpuntenmaker"         → market = "First Goalscorer"
    "Doelpuntenmaker op enig moment" → market = "Anytime Goalscorer"
    "Schoten op doel +/-"            → market = "Player Shots on Goal O/U"

  Basketball / NBA market labels:
    "Punten +/-"                     → market = "Player Points O/U"
    "Rebounds +/-"                   → market = "Player Rebounds O/U"
    "Assists +/-"                    → market = "Player Assists O/U"
    "Totaal punten +/-"              → market = "Total Points O/U"
    "Puntenlijn"                     → market = "Point Spread"

- Bottom: "Inzet €X" (left) and "Uitbetaling €X" (right) = stake and payout
- Parse player name and line from leg text (green bold):
    "Andy Pages - Meer dan 0.5 slagen"       → player="Andy Pages", selection="Over 0.5", line=0.5
    "Justin Wrobleski - Minder dan 15.5 outs" → player="Justin Wrobleski", selection="Under 15.5", line=15.5
    "Santiago Espinal - Minder dan 1.5 honken" → player="Santiago Espinal", selection="Under 1.5", line=1.5
    "Ajax"                                    → player=null, team="Ajax", selection="Ajax" (match result)
- Note: individual leg odds are NOT shown in BET BUILDER — set legs[].odds = null
- For ENKEL bets: set legs[0].odds = total_odds
- Comma in amounts is decimal separator in Dutch: "37,78" = 37.78
- If sport is unclear, infer from team names and market labels

""" + _JSON_SPEC

# ── Bet365 NL prompt ──────────────────────────────────────────────────────────

_BET365_PROMPT_TEMPLATE = """This is a Bet365 NL screenshot. Extract the bet data.
Today's date is {today} (year {year}). Use this year for any date interpretation.

Bet365 NL screen layout (three possible screens):

SCREEN TYPE 1 — "HOERA!" confirmation (single bet, ENKELVOUDIG):
- Big "HOERA!" heading with green checkmark circle
- "Je weddenschap is geplaatst." subtitle
- "ENKELVOUDIG" in cyan caps
- Match: two team names (e.g. "HC Pustertal / HK Olimpija Ljubljana")
- Clock icon + time: "Vandaag 19:45" or "Za 01:15" → use today's date for "Vandaag", ALWAYS year {year}
- Market description: e.g. "Match Odds - Regular Time"
- Selection + odds in cyan badge: e.g. "HC Pustertal  2.20" → total_odds = this number
- Gray summary box: "Odds X.XX  |  Inzet €X  |  Potentiële uitbetaling €X"
  → total_odds = X.XX, stake = X, potential_payout = X
- "Datum DD mmm JJJJ, HH:MM" = date bet was placed (NOT game date), "ID XXXXXXXXXX" = reference
- match = the two team names shown
- legs: one leg with market, selection, odds = total_odds

SCREEN TYPE 2 — "HOERA!" confirmation (PARLAY: DUBBEL, TREBLE, VIERVOUDIG, etc.):
- Big "HOERA!" heading with green checkmark circle
- Bet type in cyan caps: "DUBBEL" (2 legs) / "TREBLE" (3 legs) / "VIERVOUDIG" (4 legs)
- Each leg displayed sequentially:
    Match name: "Team A / Team B"  (or player names for tennis/other)
    Clock + time: "Vandaag 19:45" or "Za 12:00" (use year {year} for all dates)
    Market: e.g. "Match Odds - Regular Time"
    Selection + INDIVIDUAL leg odds in cyan badge: "Team A  2.20"
- ⚠️ CRITICAL — Gray summary box at the BOTTOM after all legs:
    "Odds X.XX  |  Inzet €X  |  Potentiële uitbetaling €X"
    → total_odds = X.XX from this gray box = COMBINED/PRODUCT of all legs
    → stake = the Inzet value from this gray box
    → potential_payout = the Potentiële uitbetaling from this gray box
    → Do NOT use individual leg odds as total_odds
- "Datum DD mmm JJJJ, HH:MM" and "ID XXXXXXXXXX" at bottom
- match = combine first 2 leg match names, e.g. "Team A vs Team B / Team C vs Team D"
- game_date = date of the FIRST leg (if "Vandaag" → {today}, always year {year})

SCREEN TYPE 3 — "MIJN BETS" overview page:
- "MIJN BETS" heading
- Multiple bet blocks visible
- "DUBBEL" in bold cyan = parlay section header
- Each parlay: all legs then summary "Odds X.XX | Inzet €X | Potentiële uitbetaling €X"
- "Cash-out nu €X" button may appear
- ID shown at bottom of each bet
- If multiple bets visible, extract the FIRST/topmost bet only
- status = "open" (unless "GEWONNEN" or "VERLOREN" badge visible)

SCREEN TYPE 4 — Settled bet (won/lost):
- "GEWONNEN" green banner → status = "gewonnen"
- "VERLOREN" red banner → status = "verloren"
- All other fields extracted the same way

Dutch → English mappings for bet_type:
- "ENKELVOUDIG" → "single"
- "DUBBEL"      → "parlay"  (2 legs)
- "TREBLE"      → "parlay"  (3 legs)
- "VIERVOUDIG"  → "parlay"  (4 legs)

Dutch → English mappings for selections:
- "Meer dan X"   → "Over X"
- "Minder dan X" → "Under X"
- "Thuis"        → "Home"
- "Uit"          → "Away"
- "Gelijkspel"   → "Draw"

Dutch → English mappings for market names:
- "Match Odds - Regular Time" / "Reguliere Speeltijd"   → "Match Result (Regular Time)"
- "Match Odds - Incl. Overtime" / "Incl. Verlenging"    → "Match Result (incl. OT)"
- "Wedstrijduitslag"                                     → "Match Result (1X2)"
- "Beide Teams Scoren"                                   → "Both Teams to Score"
- "Totaal Doelpunten" / "Over/Under Doelpunten"          → "Total Goals O/U"
- "Asian Handicap" / "Handicap"                          → "Asian Handicap"
- "Dubbele Kans"                                         → "Double Chance"
- "Gelijkspel Geen Weddenschap"                          → "Draw No Bet"
- "Hoekschoppen Over/Under"                              → "Total Corners O/U"
- "Strikeouts Geworden door Speler - Inclusief Extra Innings" → "Pitcher Strikeouts O/U"
- "Slagen" / "Slagen Over/Under"                         → "Player Hits O/U"
- "Totaal Honken"                                        → "Total Bases O/U"
- "Werper Outs"                                          → "Pitcher Outs O/U"
- "Punten" / "Punten Over/Under"                         → "Player Points O/U"
- "Rebounds Over/Under"                                  → "Player Rebounds O/U"
- "Assists Over/Under"                                   → "Player Assists O/U"
- "Totaal Punten"                                        → "Total Points O/U"
- "Schoten Op Doel"                                      → "Player Shots on Goal O/U"
- "Eerste Doelpuntenmaker"                               → "First Goalscorer"
- "Doelpuntenmaker Op Enig Moment"                       → "Anytime Goalscorer"

Other rules:
- "Inzet" = stake, "Potentiële uitbetaling" = potential_payout
- Comma is decimal separator: "39,98" = 39.98
- Reference: use the "ID" number as reference string
- ALWAYS use year {year} for all dates. Never use a year earlier than {year}.
- For parlay legs: each leg has its own odds in cyan badge → set in legs[].odds

"""

# ─── Vision API ───────────────────────────────────────────────────────────────

def _detect_mime(data: bytes) -> str:
    if data[:4] == b'\x89PNG':
        return "image/png"
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    return "image/jpeg"


def extract_bet_from_screenshot(client, image_bytes: bytes, bookmaker: str = "betcity") -> dict:
    """
    Stuur screenshot naar Vision API en retourneer parsed JSON dict.
    bookmaker : "betcity" of "bet365"
    """
    mime  = _detect_mime(image_bytes)
    b64   = base64.standard_b64encode(image_bytes).decode()

    # Injecteer huidige datum in Bet365-prompt zodat jaar altijd correct is
    today = datetime.date.today()
    if bookmaker == "bet365":
        prompt = _BET365_PROMPT_TEMPLATE.format(today=today.isoformat(), year=today.year) + _JSON_SPEC
    else:
        prompt = _BETCITY_PROMPT

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
    defaults = [("state", "idle"), ("data", None), ("bookmaker", "betcity")]
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
        if context in ("shortlist", "parlay"):
            st.caption(
                "Upload een bevestigingsscherm van Bet365 of BetCity. "
                "De weddenschap wordt direct geregistreerd als geplaatste weddenschap — "
                "je kunt de gegevens nog aanpassen voor je opslaat."
            )
        else:
            st.caption(
                "Upload een bevestigingsscherm van Bet365 of BetCity. "
                "De gegevens worden automatisch ingevuld — je kunt ze daarna nog aanpassen."
            )

        # Bookmaker knoppen
        st.markdown("**Welke bookmaker?**")
        bm_col1, bm_col2 = st.columns(2)
        current_bm = st.session_state[_sk(context, "bookmaker")]

        if bm_col1.button(
            "🔵 BetCity ✓" if current_bm == "betcity" else "🔵 BetCity",
            key=_sk(context, "btn_betcity"), use_container_width=True,
            type="primary" if current_bm == "betcity" else "secondary",
        ):
            st.session_state[_sk(context, "bookmaker")] = "betcity"
            st.rerun()

        if bm_col2.button(
            "🟢 Bet365 ✓" if current_bm == "bet365" else "🟢 Bet365",
            key=_sk(context, "btn_bet365"), use_container_width=True,
            type="primary" if current_bm == "bet365" else "secondary",
        ):
            st.session_state[_sk(context, "bookmaker")] = "bet365"
            st.rerun()

        selected_bm  = st.session_state[_sk(context, "bookmaker")]
        bm_display   = "🔵 BetCity" if selected_bm == "betcity" else "🟢 Bet365"
        st.caption(f"Geselecteerd: **{bm_display}**")
        st.markdown("---")

        # File uploader
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
            f"📸 Analyseer {bm_display} screenshot",
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
                        data = extract_bet_from_screenshot(client, img_bytes, bookmaker=selected_bm)
                        data["bookmaker"] = selected_bm   # expliciet zetten op basis van keuze
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

    st.markdown("---")

    btn1, btn2, btn3 = st.columns([3, 2, 1])
    confirm = btn1.button("✅ Opslaan", type="primary", key=_sk(context, "confirm"))
    _       = btn2.button("✏️ Annuleren",              key=_sk(context, "cancel"))
    cancel  = _ or btn3.button("✖",                   key=_sk(context, "cancel2"))

    if confirm:
        _do_save(context, db, data, edited_legs,
                 _odds, _stake, _status, _sport, _game_date, _match, _ref)

    if cancel:
        st.session_state[_sk(context, "state")] = "idle"
        st.session_state[_sk(context, "data")]  = None
        st.rerun()


# ─── Opslaan ─────────────────────────────────────────────────────────────────

def _do_save(context, db, data, edited_legs, odds, stake, status,
             sport, game_date, match, ref):
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

    # Gedeeld bet-object (voor add_favoriet / upsert_resultaat)
    bet_obj = {
        "player":        player,
        "bet_type":      desc,
        "sport":         sport,
        "odds":          round(odds, 2),
        "ev":            0.0,
        "team":          match or "",
        "bet365":        {},
        "import_method": "screenshot",
        "bookmaker":     bm,
        "speler":        player,
        "bet":           desc,
        "datum":         game_date.isoformat(),
        "game_date":     game_date.isoformat(),
        "ev_score":      0.0,
        "rating":        "",
        "composite":     0.0,
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
        _player   = leg.get("player") or ""
        _team     = leg.get("team") or ""
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
    st.session_state[_sk(context, "state")] = "idle"
    st.session_state[_sk(context, "data")]  = None
    st.rerun()
