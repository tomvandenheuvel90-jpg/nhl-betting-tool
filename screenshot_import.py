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
- status is always "open" for a just-placed bet"""

# ── BetCity prompt ────────────────────────────────────────────────────────────

_BETCITY_PROMPT = """This is a BetCity NL screenshot. Extract the bet data.

BetCity confirmation screen layout:
- "Sessie XX:XX" top-left, "Vergunninghouder Kansspelautoriteit" top-right
  → these confirm it is BetCity Netherlands
- Green banner: "Weddenschap geplaatst" with green checkmark
- Reference: "Ref. XXXXXX" in green text below the banner (e.g. Ref. YL8270949631W)
- Bet type in bold caps: "BET BUILDER" (multi-leg) or "ENKEL" (single)
- Match line: "Team A @ Team B  [total_odds]  €[stake]"  (e.g. "NY Mets @ LA Dodgers  3.25  €37,78")
- "Uitbetaling €X" on its own line = potential payout
- Legs in GREEN BOLD text, Dutch market label in gray below each leg:
    Leg text examples (green bold):
      "Andy Pages - Meer dan 0.5 slagen"
      "Justin Wrobleski - Minder dan 15.5 outs"
      "Santiago Espinal - Minder dan 1.5 honken"
    Market label examples (gray, below each leg):
      "Slagen +/-"         → market = "Player Hits O/U"
      "Werper - Outs +/-"  → market = "Pitcher Outs O/U"
      "Totaal aantal honken +/-" → market = "Total Bases O/U"
- Bottom: "Inzet €X" (left) and "Uitbetaling €X" (right) = stake and payout
- Parse player name and line from leg text:
    "Andy Pages - Meer dan 0.5 slagen" → player="Andy Pages", selection="Over 0.5", line=0.5
    "Justin Wrobleski - Minder dan 15.5 outs" → player="Justin Wrobleski", selection="Under 15.5", line=15.5
    "Santiago Espinal - Minder dan 1.5 honken" → player="Santiago Espinal", selection="Under 1.5", line=1.5
- Note: individual leg odds are NOT shown in BET BUILDER — set legs[].odds = null
- Comma in amounts is decimal separator in Dutch: "37,78" = 37.78

""" + _JSON_SPEC

# ── Bet365 NL prompt ──────────────────────────────────────────────────────────

_BET365_PROMPT = """This is a Bet365 NL screenshot. Extract the bet data.

Bet365 NL confirmation screen layout (two possible screens):

SCREEN TYPE 1 — "HOERA!" confirmation (shown immediately after placing a bet):
- Big "HOERA!" heading with green checkmark circle
- "Je weddenschap is geplaatst." subtitle
- Bet type in cyan/teal caps: "ENKELVOUDIG" (single) or "DUBBEL" (2-leg parlay)
  or "TREBLE" (3-leg), etc.
- Match: two team names stacked (e.g. "HC Pustertal / HK Olimpija Ljubljana")
- Clock icon + time: "Vandaag 19:45" or "Za 01:15" (date/time of the match)
  → "Vandaag" = today
- Market description on its own line: e.g. "Match Odds - Regular Time"
- Selection + odds in cyan badge: e.g. "HC Pustertal  2.20"
- Gray summary box: "Odds X.XX  |  Inzet €X  |  Potentiële uitbetaling €X"
- "Datum DD mmm JJJJ, HH:MM" = date bet was placed, "ID XXXXXXXXXX" = reference
- Buttons: cyan "Doorgaan" and dark "Bewaar op betslip"

SCREEN TYPE 2 — "MIJN BETS" overview page:
- "MIJN BETS" heading
- Multiple bet blocks: each shows match, market, selection with cyan odds badge
- "DUBBEL" in bold cyan = 2-leg parlay section header
- Each parlay shows: all legs, then summary "Odds X.XX | Inzet €X | Potentiële uitbetaling €X"
- "Cash-out nu €X" button may appear
- ID shown at bottom of each bet
- If multiple bets visible, extract the most complete one (prefer parlay over single)

Dutch → English mappings:
- "ENKELVOUDIG" → bet_type = "single"
- "DUBBEL"      → bet_type = "parlay" (2 legs)
- "TREBLE"      → bet_type = "parlay" (3 legs)
- "Meer dan X"  → selection = "Over X"
- "Minder dan X" → selection = "Under X"
- "Match Odds - Regular Time" → market = "Match Result (Regular Time)"
- "Reguliere Speeltijd" → market = "Match Result (Regular Time)"
- "Strikeouts Geworden door Speler - Inclusief Extra Innings" → market = "Pitcher Strikeouts O/U"
- "Inzet" = stake, "Potentiële uitbetaling" = potential payout
- Comma in amounts is decimal separator: "39,98" = 39.98

Reference: use the "ID" number as reference string.
For HOERA! screen: game_date = the match time shown ("Vandaag" → today's date).

""" + _JSON_SPEC

_PROMPTS = {
    "betcity": _BETCITY_PROMPT,
    "bet365":  _BET365_PROMPT,
}


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
    mime   = _detect_mime(image_bytes)
    b64    = base64.standard_b64encode(image_bytes).decode()
    prompt = _PROMPTS.get(bookmaker, _BETCITY_PROMPT)

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

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Ongeldige JSON van Vision API: {exc}\n\nRaw:\n{raw[:400]}"
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
                st.error("Kon screenshot niet uitlezen. Probeer opnieuw of voer handmatig in.")


# ─── Bevestigingsscherm ───────────────────────────────────────────────────────

def _render_confirmation(context: str, db) -> None:
    data = st.session_state[_sk(context, "data")] or {}

    st.markdown("---")
    st.markdown("### 📸 Screenshot — Controleer & Bevestig")

    if not data.get("stake") or not data.get("total_odds"):
        st.warning(
            "⚠️ Sommige gegevens konden niet worden uitgelezen. "
            "Controleer de velden hieronder."
        )

    # Badges
    bm     = data.get("bookmaker", "unknown")
    bt_raw = data.get("bet_type", "single")
    bm_lbl = {"bet365": "🟢 Bet365", "betcity": "🔵 BetCity"}.get(bm, "❓ Onbekend")
    bt_lbl = {"single": "Single", "parlay": "Parlay", "bet_builder": "Bet Builder"}.get(bt_raw, bt_raw or "—")

    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Bookmaker:** {bm_lbl}")
    c2.markdown(f"**Type:** {bt_lbl}")
    c3.markdown(f"**Sport:** {data.get('sport', '—')}")

    _match = st.text_input(
        "Wedstrijd / Event",
        value=data.get("match") or "",
        key=_sk(context, "match"),
        placeholder="bijv. NY Mets @ LA Dodgers",
    )

    # Odds / Inzet / Uitbetaling
    col1, col2, col3 = st.columns(3)
    _odds = col1.number_input(
        "Odds (totaal)", min_value=1.01, max_value=10_000.0,
        value=float(data.get("total_odds") or 2.0),
        step=0.05, format="%.2f", key=_sk(context, "odds"),
    )
    _stake = col2.number_input(
        "Inzet (€)", min_value=0.01, max_value=100_000.0,
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

    try:
        _gd_default = datetime.date.fromisoformat(data.get("game_date") or "")
    except (ValueError, TypeError):
        _gd_default = datetime.date.today()
    _game_date = st.date_input("Wedstrijddatum", value=_gd_default, key=_sk(context, "game_date"))

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
            _save_as_parlay(db, edited_legs, odds, stake, status, sport,
                            game_date, match, bm, bet_obj)
        else:
            _save_as_single(db, player, desc, bet_obj, status, stake)
        _reset(context)
        return

    # ── Shortlist: opslaan als favoriet + resultaat ──────────────────────────
    if context == "shortlist":
        fav_id = db.make_fav_id(player, desc)
        try:
            db.add_favoriet(fav_id, bet_obj, game_date=game_date.isoformat())
            db.upsert_resultaat(fav_id, bet_obj, status, stake)
            st.success("✅ Weddenschap opgeslagen in Shortlist!")
        except Exception as exc:
            st.error(f"Fout bij opslaan: {exc}")
            return
        _reset(context)
        return

    # ── Parlay Builder: direct opslaan als geplaatste weddenschap ────────────
    if context == "parlay":
        if is_multi and edited_legs:
            _save_as_parlay(db, edited_legs, odds, stake, status, sport,
                            game_date, match, bm, bet_obj)
        else:
            _save_as_single(db, player, desc, bet_obj, status, stake)
        _reset(context)
        return


def _save_as_single(db, player, desc, bet_obj, status, stake):
    fav_id = db.make_fav_id(player, desc)
    try:
        db.upsert_resultaat(fav_id, bet_obj, status, float(stake))
        st.success("✅ Weddenschap opgeslagen in Geplaatste Bets!")
    except Exception as exc:
        st.error(f"Fout bij opslaan: {exc}")
        return False
    return True


def _save_as_parlay(db, edited_legs, odds, stake, status, sport,
                    game_date, match, bm, bet_obj):
    parlay_id = uuid.uuid4().hex[:8]

    props_json = []
    legs_json  = {}
    for leg in edited_legs:
        _player   = leg.get("player") or ""
        _market   = leg.get("market") or "Player Prop"
        _sel      = leg.get("selection") or ""
        _bet_type = f"{_market} — {_sel}" if _sel else _market
        _raw_odds = leg.get("odds")
        _leg_odds = float(_raw_odds) if _raw_odds is not None else None

        props_json.append({
            "player":   _player,
            "sport":    sport,
            "bet_type": _bet_type,
            "odds":     _leg_odds,
            "hit_rate": None,
        })
        legs_json[f"{_player}_{_bet_type}"] = "open"

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
        st.success("✅ Parlay opgeslagen in Geplaatste Bets!")
    except Exception as exc:
        st.error(f"Fout bij opslaan: {exc}")
        return


def _reset(context: str) -> None:
    st.session_state[_sk(context, "state")] = "idle"
    st.session_state[_sk(context, "data")]  = None
    st.rerun()
