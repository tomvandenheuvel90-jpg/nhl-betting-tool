"""
Screenshot Bet Import — Vision API extractie en bevestigingsscherm.

Gebruik:
    from screenshot_import import render_screenshot_import
    render_screenshot_import("shortlist", client=anthropic_client)
    render_screenshot_import("parlay",    client=anthropic_client)
"""

import base64
import json
import re
import datetime

import streamlit as st

# Model voor screenshot-extractie (Sonnet voor betere accuracy dan Haiku)
IMPORT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = (
    "You are a sports betting data extraction assistant. "
    "Extract all bet data from the screenshot and return ONLY valid JSON. "
    "No explanation, no markdown, no code blocks. Raw JSON only."
)

# ── Basis JSON-instructie (beide bookmakers) ───────────────────────────────────

_BASE_PROMPT = """Return this exact JSON structure — fill every field you can read:
{
  "bookmaker": "<betcity or bet365>",
  "bet_type": "<single | parlay | bet_builder>",
  "sport": "<MLB | NBA | NHL | NFL | Soccer | Tennis | Other>",
  "stake": <number>,
  "total_odds": <number>,
  "potential_payout": <number>,
  "currency": "EUR",
  "status": "pending",
  "reference": "<reference code or null>",
  "legs": [
    {
      "description": "<full description as shown on screen>",
      "market": "<market type in English>",
      "selection": "<Over X | Under X | Yes | team name | etc.>",
      "line": <number or null>,
      "odds": <decimal odds or null>,
      "player": "<player name or null>",
      "team": "<team name or null>"
    }
  ],
  "match": "<Team A vs Team B or null>",
  "game_date": "<YYYY-MM-DD or null>",
  "raw_notes": "<any other useful text or null>"
}

Rules:
- All odds must be DECIMAL (European format, e.g. 1.85 not -118)
- Use null for any field not visible
- stake and potential_payout are plain numbers, no currency symbol
- "Meer dan X" = Over X  |  "Minder dan X" = Under X
- If multiple separate bets appear on screen, extract the most complete/prominent one
  (prefer a parlay/multi-leg bet over individual single bets)"""

# ── BetCity-specifieke instructie ─────────────────────────────────────────────

_BETCITY_PROMPT = """This is a BetCity (Dutch bookmaker) screenshot.

BetCity layout hints:
- Green "Weddenschap geplaatst" banner = bet confirmation
- Reference code shown as "Ref. XXXX" (e.g. Ref. YL8270949631W)
- "BET BUILDER" label = multi-leg same-game parlay
- Legs shown in green bold text, market label in gray below each leg
- "Inzet" = stake amount (bottom of screen)
- "Uitbetaling" = potential payout
- "Sessie" timer and "Vergunninghouder Kansspelautoriteit" confirm BetCity NL
- Dutch market names to translate:
    "Slagen +/-"              → Hits O/U
    "Werper - Outs +/-"       → Pitcher Outs O/U
    "Totaal aantal honken +/-" → Total Bases O/U
    "Meer dan X slagen"       → Player Hits Over X
    "Minder dan X outs"       → Pitcher Outs Under X
    "Minder dan X honken"     → Total Bases Under X
    "Reguliere Speeltijd"     → Match Result (Regular Time)

""" + _BASE_PROMPT

# ── Bet365-specifieke instructie ──────────────────────────────────────────────

_BET365_PROMPT = """This is a Bet365 NL screenshot.

Bet365 NL layout hints:
- "MIJN BETS" heading = bet slip overview page showing placed bets
- "DUBBEL" = 2-leg parlay, "TREBLE" = 3-leg parlay (otherwise called by leg count)
- Reference/ID shown as "ID XXXXXXXXXX" at the bottom of each bet
- Date format: "DD mmm JJJJ, HH:MM" (e.g. "10 apr 2026, 15:55")
- "Cash-out nu" button may appear — ignore it
- "Reguliere Speeltijd" = Regular Time (soccer market)
- Each bet block shows: match name, long market description, player/selection + odds chip
- Parlay (DUBBEL etc.): all legs shown above the summary row with total odds + inzet + uitbetaling
- Dutch market translations:
    "Strikeouts Geworden door Speler - Inclusief Extra Innings" → Pitcher Strikeouts O/U
    "Meer dan X"   → Over X
    "Minder dan X" → Under X
    "Reguliere Speeltijd" → Match Result (Regular Time)

If the screen shows multiple separate bets, extract the one with the most complete information
(prefer a parlay/DUBBEL with a clear inzet + uitbetaling summary).

""" + _BASE_PROMPT

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
    bookmaker: "betcity" of "bet365" — bepaalt welke prompt gebruikt wordt.
    Gooit ValueError als de JSON ongeldig is.
    """
    mime    = _detect_mime(image_bytes)
    b64     = base64.standard_b64encode(image_bytes).decode()
    prompt  = _PROMPTS.get(bookmaker, _BETCITY_PROMPT)

    resp = client.messages.create(
        model=IMPORT_MODEL,
        max_tokens=4096,
        temperature=0,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
    )

    raw = resp.content[0].text.strip()
    # Verwijder eventuele markdown-fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Ongeldige JSON van Vision API: {exc}\n\nRaw response:\n{raw[:400]}"
        ) from exc


# ─── Session-state helpers ────────────────────────────────────────────────────

def _sk(context: str, suffix: str) -> str:
    return f"sc_{context}_{suffix}"


def _init_state(context: str) -> None:
    for k, default in [("state", "idle"), ("data", None), ("error", None), ("bookmaker", "betcity")]:
        key = _sk(context, k)
        if key not in st.session_state:
            st.session_state[key] = default


# ─── Publieke functie ─────────────────────────────────────────────────────────

def render_screenshot_import(context: str, client=None) -> None:
    """
    Render de '📸 Of importeer via screenshot' sectie.

    context : "shortlist" of "parlay"
    client  : anthropic.Anthropic instantie (of None als niet beschikbaar)
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
    with st.expander("📸 Of importeer via screenshot", expanded=False):
        st.caption(
            "Upload een bevestigingsscherm van Bet365 of BetCity. "
            "De gegevens worden automatisch herkend — je kunt ze daarna nog aanpassen."
        )

        # ── Bookmaker selector ────────────────────────────────────────────────
        st.markdown("**Welke bookmaker?**")
        bm_col1, bm_col2 = st.columns(2)

        # Huidige selectie ophalen uit session state
        current_bm = st.session_state[_sk(context, "bookmaker")]

        betcity_selected = current_bm == "betcity"
        bet365_selected  = current_bm == "bet365"

        # Visuele stijl: geselecteerde knop heeft andere kleur via CSS-class trick
        betcity_label = "🔵 BetCity ✓" if betcity_selected else "🔵 BetCity"
        bet365_label  = "🟢 Bet365 ✓"  if bet365_selected  else "🟢 Bet365"

        if bm_col1.button(betcity_label, key=_sk(context, "btn_betcity"),
                          use_container_width=True,
                          type="primary" if betcity_selected else "secondary"):
            st.session_state[_sk(context, "bookmaker")] = "betcity"
            st.rerun()

        if bm_col2.button(bet365_label, key=_sk(context, "btn_bet365"),
                          use_container_width=True,
                          type="primary" if bet365_selected else "secondary"):
            st.session_state[_sk(context, "bookmaker")] = "bet365"
            st.rerun()

        selected_bm = st.session_state[_sk(context, "bookmaker")]
        bm_display  = "🔵 BetCity" if selected_bm == "betcity" else "🟢 Bet365"
        st.caption(f"Geselecteerd: **{bm_display}**")

        st.markdown("---")

        # ── File uploader ─────────────────────────────────────────────────────
        uploaded = st.file_uploader(
            "Kies een PNG of JPG screenshot (max 10 MB)",
            type=["png", "jpg", "jpeg"],
            key=_sk(context, "uploader"),
        )

        if uploaded is None:
            return

        mb = uploaded.size / 1_048_576
        if mb > 10:
            st.error("⛔ Bestand is groter dan 10 MB. Kies een kleinere screenshot.")
            return
        if mb > 5:
            st.warning(f"⚠️ Groot bestand ({mb:.1f} MB). Verwerking kan iets langer duren.")

        if st.button(
            f"📸 Analyseer {bm_display} screenshot",
            type="primary",
            key=_sk(context, "analyze_btn"),
        ):
            if client is None:
                st.error(
                    "Anthropic API niet beschikbaar. "
                    "Controleer je API-sleutel in de Streamlit Secrets."
                )
                return

            img_bytes = uploaded.read()
            with st.spinner("Screenshot analyseren… even geduld."):
                last_exc = None
                for attempt in range(2):
                    try:
                        data = extract_bet_from_screenshot(client, img_bytes, bookmaker=selected_bm)
                        # Zet bookmaker expliciet op basis van gebruikersselectie
                        data["bookmaker"] = selected_bm
                        st.session_state[_sk(context, "data")]  = data
                        st.session_state[_sk(context, "state")] = "confirm"
                        st.session_state[_sk(context, "error")] = None
                        st.rerun()
                        return
                    except Exception as exc:
                        last_exc = exc
                        if attempt == 0:
                            continue
                st.session_state[_sk(context, "error")] = str(last_exc)
                st.error(
                    "Kon screenshot niet uitlezen. Vul de weddenschap handmatig in."
                )


# ─── Bevestigingsscherm ───────────────────────────────────────────────────────

def _render_confirmation(context: str, db) -> None:
    data = st.session_state[_sk(context, "data")] or {}

    st.markdown("---")
    st.markdown("### 📸 Screenshot Import — Controleer & Bevestig")

    # Waarschuwing bij ontbrekende verplichte velden
    if not data.get("stake") or not data.get("total_odds"):
        st.warning(
            "⚠️ Sommige gegevens konden niet worden uitgelezen. "
            "Controleer de velden hieronder."
        )

    # Badges
    bm = data.get("bookmaker", "unknown")
    bm_label = {"bet365": "🟢 Bet365", "betcity": "🔵 BetCity"}.get(bm, "❓ Onbekend")
    bt_raw   = data.get("bet_type", "")
    bt_label = {"single": "Single Bet", "parlay": "Parlay", "bet_builder": "Bet Builder"}.get(
        bt_raw, bt_raw or "—"
    )
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Bookmaker:** {bm_label}")
    c2.markdown(f"**Type:** {bt_label}")
    c3.markdown(f"**Sport:** {data.get('sport', '—')}")

    # Wedstrijd
    _match = st.text_input(
        "Wedstrijd / Event",
        value=data.get("match") or "",
        key=_sk(context, "match"),
        placeholder="bijv. NY Mets @ LA Dodgers",
    )

    # Odds / Inzet / Uitbetaling
    col1, col2, col3 = st.columns(3)
    _odds = col1.number_input(
        "Odds (totaal)",
        min_value=1.01, max_value=10_000.0,
        value=float(data.get("total_odds") or 2.0),
        step=0.05, format="%.2f",
        key=_sk(context, "odds"),
    )
    _stake = col2.number_input(
        "Inzet (€)",
        min_value=0.01, max_value=100_000.0,
        value=float(data.get("stake") or 10.0),
        step=1.0, format="%.2f",
        key=_sk(context, "stake"),
    )
    col3.metric("Potentiële uitbetaling", f"€ {_odds * _stake:.2f}")

    # Status / Referentie / Sport
    col4, col5, col6 = st.columns(3)
    _status = col4.selectbox(
        "Status",
        ["open", "gewonnen", "verloren"],
        key=_sk(context, "status"),
    )
    _ref = col5.text_input(
        "Referentiecode",
        value=data.get("reference") or "",
        key=_sk(context, "ref"),
    )

    _sport_opts = ["MLB", "NBA", "NHL", "Soccer", "NFL", "Tennis", "Overig"]
    _sport_raw  = data.get("sport") or "Overig"
    if _sport_raw == "Other":
        _sport_raw = "Overig"
    _sport_idx = _sport_opts.index(_sport_raw) if _sport_raw in _sport_opts else len(_sport_opts) - 1
    _sport = col6.selectbox(
        "Sport", _sport_opts, index=_sport_idx, key=_sk(context, "sport")
    )

    # Wedstrijddatum
    try:
        _gd_default = datetime.date.fromisoformat(data.get("game_date") or "")
    except (ValueError, TypeError):
        _gd_default = datetime.date.today()
    _game_date = st.date_input(
        "Wedstrijddatum", value=_gd_default, key=_sk(context, "game_date")
    )

    # ── Legs ──────────────────────────────────────────────────────────────────
    legs_raw    = data.get("legs") or []
    edited_legs = []

    if legs_raw:
        st.markdown(f"**Legs ({len(legs_raw)}):**")
        for i, leg in enumerate(legs_raw):
            label = leg.get("description") or f"Leg {i + 1}"
            with st.expander(f"Leg {i + 1}: {label}", expanded=(len(legs_raw) == 1)):
                _ld = st.text_input(
                    "Beschrijving", value=leg.get("description") or "",
                    key=_sk(context, f"leg_desc_{i}"),
                )
                lc1, lc2 = st.columns(2)
                _lm = lc1.text_input(
                    "Markt", value=leg.get("market") or "",
                    key=_sk(context, f"leg_market_{i}"),
                )
                _ls = lc2.text_input(
                    "Selectie", value=leg.get("selection") or "",
                    key=_sk(context, f"leg_sel_{i}"),
                )
                lc3, lc4, lc5, lc6 = st.columns(4)
                _lo = lc3.number_input(
                    "Odds", min_value=0.0, max_value=1000.0,
                    value=float(leg.get("odds") or 0), step=0.05, format="%.2f",
                    key=_sk(context, f"leg_odds_{i}"),
                )
                _ll = lc4.number_input(
                    "Line", value=float(leg.get("line") or 0), step=0.5,
                    key=_sk(context, f"leg_line_{i}"),
                )
                _lp = lc5.text_input(
                    "Speler", value=leg.get("player") or "",
                    key=_sk(context, f"leg_player_{i}"),
                )
                _lt = lc6.text_input(
                    "Team", value=leg.get("team") or "",
                    key=_sk(context, f"leg_team_{i}"),
                )
                edited_legs.append({
                    "description": _ld,
                    "market":      _lm,
                    "selection":   _ls,
                    "odds":        _lo if _lo > 0 else None,
                    "line":        _ll if _ll != 0 else None,
                    "player":      _lp or None,
                    "team":        _lt or None,
                })
    else:
        st.info("Geen legs herkend in de screenshot — wordt als single bet opgeslagen.")

    st.markdown("---")

    # ── Actieknoppen ──────────────────────────────────────────────────────────
    btn1, btn2, btn3 = st.columns([3, 3, 1])
    confirm = btn1.button(
        "✅ Bevestig & Sla op", type="primary", key=_sk(context, "confirm")
    )
    manual  = btn2.button("✏️ Handmatig invullen", key=_sk(context, "manual"))
    cancel  = btn3.button("✖ Annuleer", key=_sk(context, "cancel"))

    if confirm:
        _do_save(
            context, db, data, edited_legs,
            _odds, _stake, _status, _sport, _game_date, _match, _ref,
        )

    if manual or cancel:
        st.session_state[_sk(context, "state")] = "idle"
        st.session_state[_sk(context, "data")]  = None
        st.rerun()


# ─── Opslaan ─────────────────────────────────────────────────────────────────

def _do_save(context, db, data, edited_legs, odds, stake, status,
             sport, game_date, match, ref):
    """Sla de geëxtraheerde weddenschap op in de database."""
    bm = data.get("bookmaker", "unknown")

    # Beschrijving samenstellen
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

    fav_id = db.make_fav_id(player, desc)

    bet_obj = {
        # voor add_favoriet
        "player":        player,
        "bet_type":      desc,
        "sport":         sport,
        "odds":          round(odds, 2),
        "ev":            0.0,
        "team":          match or "",
        "bet365":        {},
        "import_method": "screenshot",
        "bookmaker":     bm,
        # voor upsert_resultaat
        "speler":        player,
        "bet":           desc,
        "datum":         game_date.isoformat(),
        "game_date":     game_date.isoformat(),
        "ev_score":      0.0,
        "rating":        "",
        "composite":     0.0,
    }

    if context == "shortlist":
        try:
            db.add_favoriet(fav_id, bet_obj, game_date=game_date.isoformat())
            db.upsert_resultaat(fav_id, bet_obj, status, stake)
            st.success("✅ Weddenschap opgeslagen in Shortlist!")
        except Exception as exc:
            st.error(f"Fout bij opslaan: {exc}")
            return

    elif context == "parlay":
        if "parlay_legs" not in st.session_state:
            st.session_state.parlay_legs = []

        legs_to_add = edited_legs if edited_legs else [{
            "description": desc,
            "player":      player,
            "market":      desc,
            "selection":   "",
            "odds":        odds,
            "line":        None,
            "team":        match or None,
        }]

        for leg in legs_to_add:
            _leg_odds = float(leg.get("odds") or odds)
            _player   = leg.get("player") or player
            _market   = leg.get("market") or desc
            if leg.get("selection"):
                _bet_type = f"{_market} — {leg['selection']}"
            else:
                _bet_type = _market

            st.session_state.parlay_legs.append({
                "player":   _player,
                "sport":    sport,
                "bet_type": _bet_type,
                "odds":     _leg_odds,
                "hit_rate": None,
            })

        n = len(legs_to_add)
        st.success(f"✅ {n} leg{'s' if n > 1 else ''} toegevoegd aan Parlay Builder!")

    # Reset importeer-status
    st.session_state[_sk(context, "state")] = "idle"
    st.session_state[_sk(context, "data")]  = None
    st.rerun()
