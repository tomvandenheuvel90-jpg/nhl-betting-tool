#!/usr/bin/env python3
"""
Bet Analyzer — Streamlit Cloud versie
Ondersteunt: NHL · NBA · MLB · Voetbal (EPL/La Liga/Bundesliga/Serie A/Ligue 1)

Architectuur (na refactor):
  styles.py        — CSS Dark Pro Theme
  prompts.py       — Constanten, prompts, scenario-configuratie
  analysis.py      — Pure Python: extractie, verrijking, scoring (geen Streamlit)
  match_analysis.py — Pure Python: wedstrijd-kansberekening per sport
  ui_components.py  — Streamlit render-functies voor wedstrijdkaarten en prop-kaarten
  streamlit_app.py  — UI-shell: tabs, session state, analyse-flow (dit bestand)
"""

import streamlit as st
import os
import json
import uuid
import datetime
import traceback
import logging as _logging
import tempfile
from pathlib import Path

# ─── Logging ──────────────────────────────────────────────────────────────────

_logging.basicConfig(
    level=_logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
_log = _logging.getLogger(__name__)

# ─── Page config (moet vóór alle andere st-calls) ─────────────────────────────

st.set_page_config(
    page_title="Bet Analyzer",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── Secrets injecteren vóór import van sports modules ────────────────────────

try:
    os.environ.setdefault(
        "FOOTBALL_DATA_API_KEY",
        st.secrets.get("FOOTBALL_DATA_TOKEN", ""),
    )
except Exception:
    pass

# ─── Imports ──────────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, str(Path(__file__).parent))

from styles  import APP_CSS
from prompts import SCENARIO_LABELS, SCENARIO_WEIGHTS

from analysis import (
    extract_bets,
    detect_scenario,
    enrich_bet,
    filter_and_rank_props,
    generate_auto_props,
    generate_parlay_suggestions,
    analyze_flashscore,
    enrich_soccer_matches_form,
    is_nhl_match,
    is_nba_match,
    is_mlb_match,
)
from match_analysis import (
    analyze_nhl_matches,
    analyze_nba_matches,
    analyze_mlb_matches,
    analyze_soccer_matches,
)
from ui_components import (
    SPORT_ICONS,
    render_nhl_match_cards,
    render_soccer_match_cards,
    render_nba_match_cards,
    render_mlb_match_cards,
    render_flashscore,
    render_top3,
    render_bet_card,
)

from scorer import ev, rating
import db

try:
    from sports import odds_api
except Exception:
    odds_api = None

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# ─── CSS injecteren ───────────────────────────────────────────────────────────

st.markdown(APP_CSS, unsafe_allow_html=True)

# ─── Secrets ophalen ──────────────────────────────────────────────────────────

def _get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default) or default
    except Exception:
        return os.environ.get(key, default)

api_key    = _get_secret("ANTHROPIC_API_KEY")
_sb_url    = _get_secret("SUPABASE_URL")
_sb_key    = _get_secret("SUPABASE_KEY")
_odds_key  = _get_secret("ODDS_API_KEY")

# ─── DB initialiseren ─────────────────────────────────────────────────────────

_db_cloud = db.init(_sb_url, _sb_key)
if not _db_cloud:
    st.warning(
        "⚠️ **Lokale opslag** — Favorieten en geschiedenis verdwijnen na Streamlit herstart. "
        "Voeg `SUPABASE_URL` en `SUPABASE_KEY` toe aan je secrets voor persistente opslag."
    )

# ─── Odds API initialiseren ───────────────────────────────────────────────────

if odds_api:
    odds_api.set_api_key(_odds_key)

    try:
        from sports import soccer
        soccer.API_KEY = _get_secret("FOOTBALL_DATA_TOKEN")
    except Exception:
        pass

# ─── MoneyPuck GDrive (optioneel) ─────────────────────────────────────────────

try:
    from sports.moneypuck_local import set_gdrive_credentials, RAW_DIR, FILTERED_DIR
    _gdrive_ok = False
    try:
        _gdrive_dict = dict(st.secrets.get("gcp_service_account", {}))
        if _gdrive_dict.get("type") == "service_account":
            set_gdrive_credentials(_gdrive_dict)
            _gdrive_ok = True
    except Exception:
        pass
    if not (RAW_DIR.exists() or FILTERED_DIR.exists()):
        _fids = Path(__file__).parent / "gdrive_file_ids.json"
        if _gdrive_ok and _fids.exists():
            st.success("☁️ **Cloud modus** — MoneyPuck data via Google Drive.")
        elif not _gdrive_ok:
            st.info("ℹ️ **Cloud versie** — Historische MoneyPuck data niet beschikbaar.")
except Exception:
    pass

# ─── Guards ───────────────────────────────────────────────────────────────────

if not api_key:
    st.error("❌ Geen `ANTHROPIC_API_KEY` gevonden in st.secrets.")
    st.stop()

if not ANTHROPIC_AVAILABLE:
    st.error("❌ `anthropic` pakket niet geïnstalleerd.")
    st.stop()

# ─── Session state ────────────────────────────────────────────────────────────

for _k, _v in [
    ("uploader_key",   0),
    ("last_analysis",  None),
    ("parlay_legs",    []),
    ("just_analyzed",  False),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ─── Header ───────────────────────────────────────────────────────────────────

st.markdown("""
<div style="background:linear-gradient(135deg,#12103a 0%,#1e1860 100%);
  border:1px solid #3a2a70;border-radius:14px;padding:1.2rem 2rem;
  margin-bottom:1rem;display:flex;align-items:center;gap:1rem;
  box-shadow:0 4px 24px rgba(124,58,237,0.18);">
  <span style="font-size:2rem;">🎯</span>
  <div>
    <div style="font-size:1.5rem;font-weight:800;color:#c4b5fd;letter-spacing:-0.3px;">Bet Analyzer</div>
    <div style="color:#8888b8;font-size:0.82rem;margin-top:2px;">
      Linemate + Flashscore &nbsp;·&nbsp; NHL · NBA · MLB · Voetbal
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# Banner (optioneel SVG-asset)
try:
    with open("assets/banner.svg", "r") as _f:
        st.markdown(_f.read(), unsafe_allow_html=True)
except FileNotFoundError:
    pass

# ─── Tabs ─────────────────────────────────────────────────────────────────────

tab_dashboard, tab_analyse, tab_favorieten, tab_parlay, tab_geplaatst, tab_bankroll, tab_history = st.tabs(
    ["🏠 Dashboard", "🔍 Analyse", "⭐ Shortlist", "🎯 Parlay Builder", "📋 Geplaatste Bets", "📊 Bankroll", "🗂️ Analyse Geschiedenis"]
)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

with tab_dashboard:
    _dsh_resultaten  = db.load_resultaten()
    _dsh_favorieten  = db.load_favorieten()
    _dsh_history     = db.load_history()
    _dsh_open        = [r for r in _dsh_resultaten if r.get("uitkomst") == "open"]
    _dsh_gedaan      = [r for r in _dsh_resultaten if r.get("uitkomst") in ("gewonnen","verloren")]
    _dsh_start_bk    = float(db.get_setting("start_bankroll") or 0.0)

    # ── KPI berekeningen ──────────────────────────────────────────────────────
    _dsh_won         = sum(1 for r in _dsh_gedaan if r.get("uitkomst") == "gewonnen")
    _dsh_total_inzet = sum(r.get("inzet", 0) for r in _dsh_gedaan)
    _dsh_total_wl    = sum(r.get("winst_verlies", 0) for r in _dsh_gedaan)
    _dsh_roi         = (_dsh_total_wl / _dsh_total_inzet * 100) if _dsh_total_inzet > 0 else 0.0
    _dsh_wr          = (_dsh_won / len(_dsh_gedaan) * 100) if _dsh_gedaan else 0.0
    _dsh_huidig_saldo = (_dsh_start_bk + _dsh_total_wl) if _dsh_start_bk > 0 else None

    # Streak berekenen
    def _dsh_streak(results: list) -> tuple:
        if not results:
            return 0, "—"
        s = sorted(results, key=lambda r: r.get("datum",""))
        last = s[-1].get("uitkomst","")
        cnt = 0
        for r in reversed(s):
            if r.get("uitkomst") == last:
                cnt += 1
            else:
                break
        return cnt, last

    _dsh_streak_cnt, _dsh_streak_type = _dsh_streak(_dsh_gedaan)
    _dsh_streak_icon = "🔥" if _dsh_streak_type == "gewonnen" else ("❄️" if _dsh_streak_type == "verloren" else "—")

    # Laatste analyse
    _dsh_last_analyse = _dsh_history[0] if _dsh_history else None
    _dsh_last_analyse_str = (
        f"{_dsh_last_analyse.get('datum','')} om {_dsh_last_analyse.get('tijd','')}"
        if _dsh_last_analyse else "Nog geen analyse"
    )

    # Week P&L (laatste 7 dagen)
    _dsh_week_cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    _dsh_week_gedaan = [r for r in _dsh_gedaan if r.get("datum","") >= _dsh_week_cutoff]
    _dsh_week_wl     = sum(r.get("winst_verlies",0) for r in _dsh_week_gedaan)

    # ── Welkomstregel ─────────────────────────────────────────────────────────
    st.markdown("### 🏠 Dashboard")
    _dsh_datum_str = datetime.date.today().strftime("%-d %B %Y")
    st.caption(f"📅 {_dsh_datum_str}  ·  {len(_dsh_open)} open {'bet' if len(_dsh_open) == 1 else 'bets'}  ·  {len(_dsh_favorieten)} in Shortlist")

    # ── KPI-rij ───────────────────────────────────────────────────────────────
    _k1, _k2, _k3, _k4, _k5 = st.columns(5)

    if _dsh_huidig_saldo is not None:
        _k1.metric("🏦 Bankroll", f"€{_dsh_huidig_saldo:.0f}",
                   delta=f"€{_dsh_total_wl:+.0f}" if _dsh_total_wl else None)
    else:
        _k1.metric("🏦 Bankroll", "—", help="Stel je startbankroll in via de Bankroll tab")

    _k2.metric("💰 P&L (week)",
               f"€{_dsh_week_wl:+.0f}" if _dsh_week_gedaan else "—",
               delta=f"{len(_dsh_week_gedaan)} bets" if _dsh_week_gedaan else None)
    _k3.metric("📈 ROI",    f"{_dsh_roi:+.1f}%" if _dsh_gedaan else "—")
    _k4.metric("🎯 Win%",   f"{_dsh_wr:.0f}%"   if _dsh_gedaan else "—",
               delta=f"{_dsh_won}/{len(_dsh_gedaan)}" if _dsh_gedaan else None)
    _streak_soort = "W" if _dsh_streak_type == "gewonnen" else ("L" if _dsh_streak_type == "verloren" else "")
    _streak_label = f"{_dsh_streak_cnt}× {_streak_soort}" if _dsh_gedaan else "—"
    _k5.metric(f"{_dsh_streak_icon} Streak", _streak_label)

    st.markdown("---")

    # ── Open bets (meest actioneerbaar) ───────────────────────────────────────
    if _dsh_open:
        _dsh_open_sorted = sorted(_dsh_open, key=lambda r: r.get("datum",""))
        _days_open_max   = 0
        try:
            _days_open_max = (datetime.date.today() -
                              datetime.date.fromisoformat(_dsh_open_sorted[0].get("datum","")[:10])).days
        except Exception:
            pass

        _open_header = f"⏳ {len(_dsh_open)} open {'weddenschap' if len(_dsh_open) == 1 else 'weddenschappen'}"
        if _days_open_max >= 3:
            _open_header += f"  ⚠️ oudste al {_days_open_max} dagen open"
        st.markdown(f"#### {_open_header}")
        st.caption("Markeer de uitkomst zodra de wedstrijd gespeeld is.")

        for _dop in _dsh_open_sorted:
            _dop_id  = _dop.get("id","")
            _dop_dag = ""
            try:
                _dop_dag = f"{(datetime.date.today() - datetime.date.fromisoformat(_dop.get('datum','')[:10])).days}d geleden"
            except Exception:
                _dop_dag = _dop.get("datum","")[:10]
            _dopa, _dopb, _dopc, _dopd, _dope = st.columns([3, 1, 1, 1, 1])
            _dopa.write(f"**{_dop.get('speler','')}** — {_dop.get('bet','')}")
            _dopa.caption(f"{_dop.get('sport','')} · @ {_dop.get('odds','—')} · €{_dop.get('inzet',0):.2f} · {_dop_dag}")
            if _dopb.button("✅ Won",     key=f"dsh_won_{_dop_id}",  use_container_width=True):
                db.upsert_resultaat(_dop_id, _dop, "gewonnen", float(_dop.get("inzet",10)))
                st.rerun()
            if _dopc.button("❌ Verloor", key=f"dsh_lost_{_dop_id}", use_container_width=True):
                db.upsert_resultaat(_dop_id, _dop, "verloren", float(_dop.get("inzet",10)))
                st.rerun()
    else:
        st.success("✅ Geen open weddenschappen — alles is up-to-date.")

    st.markdown("---")

    # ── Twee kolommen: recente resultaten + shortlist/analyse ─────────────────
    _dcol_l, _dcol_r = st.columns([3, 2])

    with _dcol_l:
        st.markdown("#### 📊 Laatste resultaten")
        _dsh_recent = sorted(_dsh_gedaan, key=lambda r: r.get("datum",""), reverse=True)[:7]
        if not _dsh_recent:
            st.info("Nog geen afgeronde weddenschappen.")
        else:
            for _dr in _dsh_recent:
                _dr_icon = "✅" if _dr.get("uitkomst") == "gewonnen" else "❌"
                _dr_wl   = _dr.get("winst_verlies", 0)
                _dr_wl_s = f"€{_dr_wl:+.2f}"
                _dr_kleur = "#4ade80" if _dr_wl >= 0 else "#f87171"
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;align-items:center;"
                    f"padding:6px 0;border-bottom:1px solid #1e1e3a;'>"
                    f"<span>{_dr_icon} <b>{_dr.get('speler','')}</b> — {_dr.get('bet','')}"
                    f"<br><small style='color:#888;'>{_dr.get('sport','')} · {_dr.get('datum','')[:10]}</small></span>"
                    f"<span style='color:{_dr_kleur};font-weight:700;font-size:1.1rem;'>{_dr_wl_s}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    with _dcol_r:
        # Shortlist preview
        st.markdown("#### ⭐ Shortlist")
        if not _dsh_favorieten:
            st.info("Shortlist is leeg — voeg props toe via Analyse of Analyse Geschiedenis.")
        else:
            for _df in _dsh_favorieten[:5]:
                _df_ev = float(_df.get("ev_score", 0))
                _ev_kleur = "#4ade80" if _df_ev >= 0.05 else "#facc15"
                st.markdown(
                    f"<div style='padding:5px 0;border-bottom:1px solid #1e1e3a;'>"
                    f"<b>{_df.get('speler','')}</b> — {_df.get('bet','')}<br>"
                    f"<small style='color:#888;'>@ {_df.get('odds','—')} · "
                    f"<span style='color:{_ev_kleur};'>EV {_df_ev:+.3f}</span></small>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            if len(_dsh_favorieten) > 5:
                st.caption(f"+ {len(_dsh_favorieten) - 5} meer → ga naar Shortlist tab")

        st.markdown("---")

        # Laatste analyse info
        st.markdown("#### 🔍 Laatste analyse")
        if _dsh_last_analyse:
            _la_props = _dsh_last_analyse.get("alle_props_json") or _dsh_last_analyse.get("top5") or []
            _la_sport_set = {p.get("sport","") for p in _la_props if p.get("sport")}
            st.markdown(
                f"<div style='padding:5px 0;'>"
                f"📅 <b>{_dsh_last_analyse_str}</b><br>"
                f"<small style='color:#888;'>{len(_la_props)} props · {', '.join(sorted(_la_sport_set)) or '—'}</small>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.info("Nog geen analyse gedaan. Ga naar de Analyse tab om te beginnen.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — ANALYSE
# ══════════════════════════════════════════════════════════════════════════════

with tab_analyse:

    if st.session_state.just_analyzed:
        st.success("✅ Analyse klaar — bestanden gewist. Upload nieuwe screenshots voor een nieuwe analyse.")
        st.session_state.just_analyzed = False

    uploaded_files = st.file_uploader(
        "Upload Linemate en/of Flashscore screenshots",
        type=["png", "jpg", "jpeg", "webp", "heic", "heif"],
        accept_multiple_files=True,
        help="Je kunt meerdere screenshots tegelijk uploaden (ook iPhone HEIC foto's)",
        key=f"uploader_{st.session_state.uploader_key}",
    )

    if uploaded_files:
        cols = st.columns(min(len(uploaded_files), 4))
        for i, f in enumerate(uploaded_files):
            cols[i % 4].image(f, use_container_width=True)

    # Odds API gebruik indicator
    if odds_api and _odds_key:
        _usage   = odds_api.get_usage()
        _calls   = _usage.get("calls", 0)
        _limiet  = _usage.get("limiet", 500)
        _today   = datetime.date.today()
        _nxt_str = (_today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1).strftime("%-d %B %Y")
        if _calls >= _limiet:
            st.warning(f"ℹ️ Bet365 verificatie tijdelijk uitgeschakeld (maandlimiet). Reset op {_nxt_str}.")
        elif _calls > 400:
            st.warning(f"⚠️ Bijna op Odds API limiet ({_calls}/{_limiet} calls deze maand)")
        else:
            st.caption(f"🎯 Odds API: {_calls}/{_limiet} calls gebruikt deze maand")

    analyze_btn = st.button(
        "🔍 Analyseer",
        use_container_width=True,
        disabled=not uploaded_files,
        type="primary",
    )

    # ── Analyse uitvoeren ──────────────────────────────────────────────────────

    if analyze_btn and uploaded_files:
        tmp_paths = []
        _aborted  = False
        _reason   = ""
        try:
            for f in uploaded_files:
                suffix = Path(f.name).suffix.lower() or ".png"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp.write(f.read())
                tmp.flush()
                tmp.close()
                tmp_paths.append(tmp.name)

            client = anthropic.Anthropic(api_key=api_key)

            with st.status("⏳ Analyseren...", expanded=True) as status:
                st.write("📸 Screenshots herkennen...")

                # extract_bets geeft (bets, matches, debug_info) terug
                bets, matches, dbg = extract_bets(client, tmp_paths)

                if not bets and not matches:
                    _aborted = True
                    if dbg.get("traceback"):
                        _reason = "Exception tijdens extract_bets()"
                    elif dbg.get("parse_error"):
                        _reason = "JSON parsing mislukt"
                    elif not dbg.get("raw"):
                        _reason = "Claude gaf geen response terug (API-fout?)"
                    else:
                        _reason = f"Claude gaf response maar geen data ({len(dbg.get('raw',''))} tekens)"

                    st.error(f"❌ Analyse mislukt: **{_reason}**")
                    with st.expander("🔧 Debug — volledige diagnostiek", expanded=True):
                        st.caption(f"Model: `{dbg.get('model','?')}` · response: {len(dbg.get('raw',''))} tekens")
                        if dbg.get("steps"):
                            st.markdown("**Stap-log:**")
                            st.code("\n".join(dbg["steps"]), language="text")
                        if dbg.get("parse_error"):
                            st.error(dbg["parse_error"])
                        if dbg.get("traceback"):
                            st.code(dbg["traceback"], language="python")
                        st.markdown("**Claude's ruwe response:**")
                        st.code(dbg.get("raw", "(leeg)")[:4000], language="text")

                    # Auto-test: laat Claude de afbeelding beschrijven
                    st.write("🔍 **Auto-test:** Claude beschrijft de afbeelding...")
                    try:
                        from analysis import _image_content_block
                        _tr = client.messages.create(
                            model=dbg.get("model", "claude-haiku-4-5"),
                            max_tokens=512,
                            messages=[{"role": "user", "content": [
                                _image_content_block(tmp_paths[0]),
                                {"type": "text", "text": "Beschrijf wat je ziet in deze afbeelding."},
                            ]}],
                        )
                        st.info(f"**Claude ziet:** {_tr.content[0].text}")
                    except Exception as _te:
                        st.error(f"Beschrijvingstest mislukt: {type(_te).__name__}: {_te}")

                if _aborted:
                    status.update(label=f"⚠️ Analyse mislukt: {_reason}", state="error")
                else:
                    scenario = detect_scenario(bets, matches)
                    lm_w, s_w = SCENARIO_WEIGHTS[scenario]
                    st.write(f"✅ Gevonden: {len(bets)} props, {len(matches)} wedstrijden")
                    st.write(SCENARIO_LABELS[scenario])

                    # Scenario 1: automatische props genereren uit wedstrijdschema
                    if scenario == 1:
                        st.write("📅 Wedstrijdschema ophalen en props genereren...")
                        auto_bets = generate_auto_props(
                            matches,
                            progress_cb=lambda msg: st.write(f"  · {msg}"),
                        )
                        if auto_bets:
                            bets = auto_bets
                            st.write(f"✅ {len(auto_bets)} automatische props gegenereerd")
                        else:
                            st.warning("⚠️ Geen automatische props beschikbaar (schema of API niet bereikbaar)")

                    # Props verrijken en EV berekenen
                    enriched = []
                    if bets:
                        st.write("🔎 Spelersdata ophalen en EV berekenen...")
                        cache: dict = {}
                        prog = st.progress(0)
                        for i, bet in enumerate(bets):
                            enriched.append(enrich_bet(bet, cache, linemate_weight=lm_w, season_weight=s_w))
                            prog.progress((i + 1) / len(bets))
                        enriched.sort(key=lambda x: x["ev"], reverse=True)
                        st.write(f"✅ {len(enriched)} props gescoord")

                        # Bet365 verificatie (optioneel)
                        if odds_api and odds_api._API_KEY and not odds_api.is_limit_reached():
                            _to_check = [b for b in enriched if b["ev"] > 0]
                            if _to_check:
                                st.write(f"💰 Bet365 verificatie voor {len(_to_check)} props...")
                                odds_api.prefetch_event_props_for_bets(_to_check)
                                b365_prog = st.progress(0)
                                for _i, _bet in enumerate(_to_check):
                                    b365 = odds_api.check_bet365_availability(
                                        player_name=_bet["player"],
                                        bet_type=_bet["bet_type"],
                                        sport=_bet["sport"],
                                        team=_bet.get("team", ""),
                                    )
                                    _bet["bet365"] = b365
                                    if b365["status"] == "available" and b365.get("bet365_odds"):
                                        _bet["odds"]   = b365["bet365_odds"]
                                        _bet["ev"]     = ev(_bet["composite"], b365["bet365_odds"])
                                        _bet["rating"] = rating(_bet["ev"], _bet["composite"])
                                    b365_prog.progress((_i + 1) / len(_to_check))
                                _u = odds_api.get_usage()
                                st.write(f"✅ Bet365 klaar ({_u['calls']}/{_u['limiet']} calls deze maand)")

                                def _ev_rank(b):
                                    s = b.get("bet365", {}).get("status", "unknown")
                                    if s == "unavailable":   return -999.0
                                    if s == "different_line": return b["ev"] * 0.85
                                    return b["ev"]
                                enriched.sort(key=_ev_rank, reverse=True)

                        elif odds_api and odds_api._API_KEY and odds_api.is_limit_reached():
                            st.write("ℹ️ Bet365 verificatie overgeslagen (maandlimiet bereikt)")

                    # Wedstrijd-analyses per sport
                    flashscore_text       = ""
                    nhl_match_analyses    = []
                    soccer_match_analyses = []
                    nba_match_analyses    = []
                    mlb_match_analyses    = []

                    if matches:
                        nhl_matches    = [m for m in matches if is_nhl_match(m)]
                        nba_matches    = [m for m in matches if is_nba_match(m)]
                        mlb_matches    = [m for m in matches if is_mlb_match(m)]
                        soccer_matches = [
                            m for m in matches
                            if m not in nhl_matches and m not in nba_matches and m not in mlb_matches
                        ]

                        if nhl_matches:
                            st.write(f"🏒 {len(nhl_matches)} NHL-wedstrijd(en) analyseren...")
                            nhl_match_analyses = analyze_nhl_matches(nhl_matches)
                            st.write("✅ NHL wedstrijd-analyse klaar")

                        if nba_matches:
                            st.write(f"🏀 {len(nba_matches)} NBA-wedstrijd(en) analyseren...")
                            nba_match_analyses = analyze_nba_matches(nba_matches)
                            st.write("✅ NBA wedstrijd-analyse klaar")

                        if mlb_matches:
                            st.write(f"⚾ {len(mlb_matches)} MLB-wedstrijd(en) analyseren...")
                            mlb_match_analyses = analyze_mlb_matches(mlb_matches)
                            st.write("✅ MLB wedstrijd-analyse klaar")

                        if soccer_matches:
                            st.write(f"⚽ {len(soccer_matches)} voetbalwedstrijd(en) analyseren...")
                            soccer_match_analyses = analyze_soccer_matches(soccer_matches)
                            st.write("📺 Teamvorm ophalen via Football-Data API...")
                            soccer_matches = enrich_soccer_matches_form(soccer_matches)
                            st.write("📺 Flashscore analyseren via Claude...")
                            flashscore_text = analyze_flashscore(client, soccer_matches, enriched)
                            st.write("✅ Voetbal analyse klaar")

                    status.update(label="✅ Analyse compleet!", state="complete")

            # ── Resultaten opslaan ─────────────────────────────────────────────

            if not _aborted:
                enriched_ranked = filter_and_rank_props(enriched)
                _auto_parlays   = generate_parlay_suggestions(enriched_ranked)

                # Top 3: geef voorkeur aan sterke props
                top3 = [b for b in enriched_ranked if b["rating"].startswith("✅")][:3]
                if not top3:
                    top3 = enriched_ranked[:3]
                if not top3:
                    top3 = [b for b in enriched if float(b.get("ev") or -1) > 0][:3]
                top3_out = [
                    {"player": b["player"], "bet_type": b["bet_type"],
                     "odds": b["odds"], "ev": b["ev"]}
                    for b in top3
                ]

                if enriched:
                    _sid = db.save_to_history(enriched, alle_props=enriched_ranked, parlay_suggesties=_auto_parlays)
                    st.session_state["current_session_id"] = _sid

                st.session_state.last_analysis = {
                    "enriched":              enriched,
                    "enriched_ranked":       enriched_ranked,
                    "top3":                  top3_out,
                    "flashscore":            flashscore_text,
                    "nhl_match_analyses":    nhl_match_analyses,
                    "soccer_match_analyses": soccer_match_analyses,
                    "nba_match_analyses":    nba_match_analyses,
                    "mlb_match_analyses":    mlb_match_analyses,
                    "scenario":              scenario,
                    "auto_parlays":          _auto_parlays,
                    "debug_hit_rates":       dbg.get("hit_rates", []),
                }
                st.session_state.uploader_key += 1
                st.session_state.just_analyzed = True
                st.rerun()

        except Exception as e:
            _full_tb = traceback.format_exc()
            _log.error(f"[analyse] Onverwachte fout:\n{_full_tb}")
            st.error(f"❌ **{type(e).__name__}**: {e}")
            with st.expander("🔧 Volledige traceback", expanded=True):
                st.code(_full_tb, language="python")
        finally:
            for p in tmp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    # ── Vorige analyseresultaten tonen ────────────────────────────────────────

    if st.session_state.last_analysis:
        res = st.session_state.last_analysis

        enriched              = res["enriched"]
        top3_out              = res["top3"]
        flashscore_text       = res["flashscore"]
        nhl_match_analyses    = res.get("nhl_match_analyses", [])
        soccer_match_analyses = res.get("soccer_match_analyses", [])
        nba_match_analyses    = res.get("nba_match_analyses", [])
        mlb_match_analyses    = res.get("mlb_match_analyses", [])
        scenario              = res.get("scenario", 3)

        st.info(SCENARIO_LABELS.get(scenario, ""))
        if scenario == 3:
            st.info("⚠️ Tip: upload ook een Flashscore screenshot voor wedstrijdcontext en automatische prop-suggesties.")

        # Wedstrijd-kaarten
        if nhl_match_analyses:    render_nhl_match_cards(nhl_match_analyses)
        if nba_match_analyses:    render_nba_match_cards(nba_match_analyses)
        if mlb_match_analyses:    render_mlb_match_cards(mlb_match_analyses)
        if soccer_match_analyses: render_soccer_match_cards(soccer_match_analyses)
        if flashscore_text:       render_flashscore(flashscore_text)

        if enriched:
            # Debug hit rates
            _hr_dbg = res.get("debug_hit_rates", [])
            if _hr_dbg:
                with st.expander("🔧 Debug — Hit rates per prop (Claude Haiku extractie)", expanded=False):
                    _found   = [r for r in _hr_dbg if r.get("hit_rate") is not None]
                    _missing = [r for r in _hr_dbg if r.get("hit_rate") is None]
                    st.caption(f"{len(_found)}/{len(_hr_dbg)} props met hit_rate · {len(_missing)} ontbrekend")
                    for r in _hr_dbg:
                        hr = r.get("hit_rate")
                        hr_str = f"{hr*100:.1f}%" if hr is not None else "⚠️ Niet gevonden"
                        st.write(f"- **{r['player']}** · {r['bet_type']} @ {r['odds']} → HR: `{hr_str}`")

            st.markdown("---")
            render_top3(top3_out)

            # Automatische parlay suggesties
            _aps = res.get("auto_parlays", [])
            if _aps:
                st.markdown("---")
                st.markdown("### 🎯 Automatische Parlay Suggesties")
                st.caption("Top combinaties op basis van beschikbare props")
                for _api, _apc in enumerate(_aps, 1):
                    _ev_s = f"+{_apc['parlay_ev']:.3f}" if _apc["parlay_ev"] >= 0 else f"{_apc['parlay_ev']:.3f}"
                    _legs = " + ".join(f"{b.get('player','')} ({b.get('bet_type','')})" for b in _apc["props"])
                    _c1, _c2, _c3, _c4, _c5 = st.columns([4, 1, 1, 1, 1])
                    _c1.write(f"**{_api}.** {_legs}")
                    _c2.write(f"Odds: {_apc.get('gecombineerde_odds', 0):.2f}")
                    _c3.write(f"Hit: {_apc.get('hit_kans', 0)*100:.1f}%")
                    _c4.write(f"EV: {_ev_s}")
                    if _c5.button("⭐ Sla op", key=f"autopar_{_api}"):
                        db.save_parlay({
                            "id":                 str(uuid.uuid4())[:8],
                            "datum":              datetime.datetime.now().isoformat(),
                            "props_json":         _apc.get("props", []),
                            "gecombineerde_odds": _apc.get("gecombineerde_odds", 1.0),
                            "hit_kans":           _apc.get("hit_kans", 0.0),
                            "ev_score":           _apc["parlay_ev"],
                            "inzet":              10.0,
                            "uitkomst":           "open",
                            "winst_verlies":      0.0,
                            "legs_json":          {
                                b.get("player","")+"_"+b.get("bet_type",""): "open"
                                for b in _apc.get("props", [])
                            },
                        })
                        st.success(f"✅ Parlay {_api} opgeslagen!")
                        st.rerun()

            # Alle props
            st.markdown("---")
            st.markdown("### 📊 Alle props")
            _fav_ids_set = {f["id"] for f in db.load_favorieten()}
            _cur_sid     = st.session_state.get("current_session_id", "")
            for i, bet in enumerate(enriched, 1):
                _is_fav = db.make_fav_id(bet["player"], bet["bet_type"]) in _fav_ids_set
                render_bet_card(bet, i, len(enriched), is_fav=_is_fav, session_id=_cur_sid)

        st.caption("⚠️ Statistische analyse ter ondersteuning. Wedden brengt financiële risico's. Speel verantwoord.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — FAVORIETEN
# ══════════════════════════════════════════════════════════════════════════════

with tab_favorieten:
    st.markdown("### ⭐ Shortlist — Bets in overweging")

    # ── Handmatig weddenschap toevoegen ───────────────────────────────────────
    with st.expander("➕ Handmatig weddenschap toevoegen", expanded=False):
        st.caption("Voeg een weddenschap toe die niet via de app geanalyseerd is.")
        _mf1, _mf2 = st.columns(2)
        _m_speler  = _mf1.text_input("Speler / Team", key="m_speler",
                                      placeholder="bijv. Connor McDavid of Yankees")
        _m_sport   = _mf2.selectbox("Sport", ["NHL","NBA","MLB","Voetbal","Overig"],
                                     key="m_sport")
        _mf3, _mf4 = st.columns(2)
        _m_bet     = _mf3.text_input("Bet type", key="m_bet",
                                      placeholder="bijv. Anytime Goal Scorer")
        _m_odds    = _mf4.number_input("Odds", min_value=1.01, max_value=50.0,
                                        value=2.00, step=0.05, format="%.2f",
                                        key="m_odds")
        _mf5, _mf6, _mf7 = st.columns(3)
        _m_inzet   = _mf5.number_input("Inzet (€)", min_value=0.10, value=10.0,
                                         step=1.0, format="%.2f", key="m_inzet")
        _m_uitkomst = _mf6.selectbox("Uitkomst", ["open","gewonnen","verloren"],
                                      key="m_uitkomst")
        _m_ev      = _mf7.number_input("EV (optioneel)", min_value=-1.0, max_value=5.0,
                                        value=0.0, step=0.01, format="%.3f",
                                        key="m_ev")
        if st.button("➕ Toevoegen aan favorieten", key="m_add_fav",
                     disabled=not (_m_speler and _m_bet)):
            _m_bet_obj = {
                "player":   _m_speler,
                "bet_type": _m_bet,
                "sport":    _m_sport,
                "odds":     _m_odds,
                "ev":       _m_ev,
                "team":     "",
                "bet365":   {},
                "source":   "handmatig",
            }
            _m_fid = db.make_fav_id(_m_speler, _m_bet)
            db.add_favoriet(_m_fid, _m_bet_obj)
            if _m_uitkomst != "open":
                db.upsert_resultaat(_m_fid, _m_bet_obj, _m_uitkomst, _m_inzet)
            st.success(f"✅ '{_m_speler} — {_m_bet}' toegevoegd!")
            st.rerun()

    st.markdown("---")
    _favs    = db.load_favorieten()
    _res_map = {r["id"]: r for r in db.load_resultaten()}

    if not _favs:
        st.info("Nog geen favorieten. Klik op ⭐ in een prop-kaart om te bewaren of voeg er handmatig een toe.")
    else:
        # Samenvatting als er afgeronde resultaten zijn
        _done = [r for r in db.load_resultaten() if r.get("uitkomst") in ("gewonnen", "verloren")]
        if _done:
            _fn_won = sum(1 for r in _done if r.get("uitkomst") == "gewonnen")
            _ft_inzet = sum(r.get("inzet", 0) for r in _done)
            _ft_wl    = sum(r.get("winst_verlies", 0) for r in _done)
            _froi     = (_ft_wl / _ft_inzet * 100) if _ft_inzet > 0 else 0.0
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("✅ Gewonnen",  _fn_won)
            c2.metric("❌ Verloren",  len(_done) - _fn_won)
            c3.metric("💰 P&L",       f"€{_ft_wl:+.2f}")
            c4.metric("📈 ROI",       f"{_froi:+.1f}%")
            st.markdown("---")

        for _idx, _fav in enumerate(_favs):
            _fid      = _fav.get("id", "")
            _res      = _res_map.get(_fid, {})
            _uitkomst = _res.get("uitkomst", "")
            _icon     = "✅" if _uitkomst == "gewonnen" else ("❌" if _uitkomst == "verloren" else "⏳")
            _ev_disp  = f"{float(_fav.get('ev_score', 0)):+.3f}"

            with st.expander(
                f"{_icon} {_fav.get('speler','')} · {_fav.get('bet','')} "
                f"@ {_fav.get('odds','')}  |  EV {_ev_disp}  |  {_fav.get('datum','')}",
                expanded=(_uitkomst == ""),
            ):
                _ci, _cd = st.columns([4, 1])
                with _ci:
                    _cap = f"Sport: {_fav.get('sport','')} · Bet365: {_fav.get('bet365_status','')}"
                    if _res:
                        _cap += f"  ·  Inzet: €{_res.get('inzet',0):.2f}  ·  P&L: €{_res.get('winst_verlies',0):+.2f}"
                    st.caption(_cap)
                with _cd:
                    if st.button("🗑️", key=f"delfav_{_fid}_{_idx}", help="Verwijder favoriet"):
                        db.remove_favoriet(_fid)
                        db.remove_resultaat(_fid)
                        st.rerun()

                _inzet_default = float(_res.get("inzet", 10.0))
                _inzet = st.number_input(
                    "💰 Inzet (€)", min_value=0.10, value=_inzet_default,
                    step=1.0, format="%.2f", key=f"inzet_{_fid}_{_idx}",
                )
                _cpl, _cw, _cl, _cp = st.columns(4)
                if _cpl.button("📋 Geplaatst", key=f"placed_{_fid}_{_idx}", use_container_width=True,
                               help="Markeer als geplaatst (uitkomst nog onbekend)"):
                    db.upsert_resultaat(_fid, _fav, "open", _inzet)
                    st.rerun()
                if _cw.button("✅ Gewonnen", key=f"won_{_fid}_{_idx}",  use_container_width=True):
                    db.upsert_resultaat(_fid, _fav, "gewonnen", _inzet)
                    st.rerun()
                if _cl.button("❌ Verloren", key=f"lost_{_fid}_{_idx}", use_container_width=True):
                    db.upsert_resultaat(_fid, _fav, "verloren", _inzet)
                    st.rerun()
                if _cp.button("⏳ Reset",    key=f"reset_{_fid}_{_idx}", use_container_width=True):
                    db.remove_resultaat(_fid)
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — BANKROLL
# ══════════════════════════════════════════════════════════════════════════════

with tab_bankroll:
    st.markdown("### 📊 Bankroll Tracker")

    import pandas as pd

    # ── Weddenschap toevoegen ────────────────────────────────────────────────
    with st.expander("➕ Weddenschap toevoegen", expanded=True):
        st.caption("Voeg een weddenschap toe — zowel al gezette bets (open) als afgeronde bets (gewonnen/verloren).")
        _db1, _db2, _db3 = st.columns(3)
        _db4, _db5, _db6 = st.columns(3)
        _db_speler   = _db1.text_input("Speler / Team", key="db_speler",
                                        placeholder="bijv. Connor McDavid")
        _db_sport    = _db2.selectbox("Sport", ["NHL","NBA","MLB","Voetbal","Overig"], key="db_sport")
        _db_bet      = _db3.text_input("Bet type", key="db_bet",
                                        placeholder="bijv. Anytime Goal Scorer")
        _db_odds     = _db4.number_input("Odds", min_value=1.01, max_value=50.0,
                                          value=1.90, step=0.05, format="%.2f", key="db_odds")
        _db_inzet    = _db5.number_input("Inzet (€)", min_value=0.10, max_value=5000.0,
                                          value=10.0, step=1.0, format="%.2f", key="db_inzet")
        _db_uitkomst = _db6.selectbox(
            "Uitkomst",
            ["open (nog niet gespeeld)", "gewonnen", "verloren"],
            key="db_uitkomst",
            help="Kies 'open' als je de bet al geplaatst hebt maar de uitkomst nog onbekend is.",
        )
        _db_datum    = st.date_input("Datum", value=datetime.date.today(), key="db_datum")
        if st.button("💾 Opslaan", key="btn_db_save", use_container_width=True, type="primary"):
            if _db_speler and _db_bet:
                _db_uitkomst_clean = "open" if _db_uitkomst.startswith("open") else _db_uitkomst
                db.add_direct_bet(
                    speler=_db_speler, sport=_db_sport, bet_type=_db_bet,
                    odds=float(_db_odds), inzet=float(_db_inzet),
                    uitkomst=_db_uitkomst_clean, datum=_db_datum.isoformat(),
                )
                st.success("✅ Weddenschap opgeslagen!")
                st.rerun()
            else:
                st.warning("Vul minimaal speler/team en bet type in.")

    st.markdown("---")

    # ── Openstaande weddenschappen ───────────────────────────────────────────
    _openstaand = [r for r in db.load_resultaten() if r.get("uitkomst") == "open"]
    if _openstaand:
        st.markdown("#### ⏳ Openstaande weddenschappen")
        st.caption(f"{len(_openstaand)} bet(s) nog niet afgerond — klik op gewonnen of verloren om te registreren.")
        for _op in sorted(_openstaand, key=lambda r: r.get("datum",""), reverse=True):
            _op_id = _op.get("id","")
            _oc1, _oc2, _oc3, _oc4, _oc5 = st.columns([3, 1, 1, 1, 1])
            _oc1.write(f"**{_op.get('speler','')}** — {_op.get('bet','')}  @ {_op.get('odds','—')} | €{_op.get('inzet',0):.2f}")
            _oc2.caption(_op.get("datum","")[:10])
            if _oc3.button("✅ Won", key=f"opwon_{_op_id}"):
                _op_upd = dict(_op)
                _op_upd["uitkomst"] = "gewonnen"
                db.upsert_resultaat(_op_id, _op_upd, "gewonnen", float(_op.get("inzet", 10)))
                st.rerun()
            if _oc4.button("❌ Verloor", key=f"oplost_{_op_id}"):
                _op_upd = dict(_op)
                _op_upd["uitkomst"] = "verloren"
                db.upsert_resultaat(_op_id, _op_upd, "verloren", float(_op.get("inzet", 10)))
                st.rerun()
            if _oc5.button("🗑️", key=f"opdel_{_op_id}", help="Verwijder"):
                db.remove_resultaat(_op_id)
                st.rerun()
        st.markdown("---")

    # ── Startbankroll instelling ─────────────────────────────────────────────
    _start_bk_saved = float(db.get_setting("start_bankroll") or 0.0)
    with st.expander("⚙️ Bankroll instellingen", expanded=(_start_bk_saved == 0)):
        _start_bk_input = st.number_input(
            "Startbankroll (€)", min_value=0.0, max_value=100000.0,
            value=_start_bk_saved, step=10.0, format="%.2f", key="start_bk_input",
            help="Stel je beginkapitaal in. De app berekent dan je huidig saldo en groei%."
        )
        if st.button("💾 Opslaan", key="btn_start_bk"):
            db.set_setting("start_bankroll", float(_start_bk_input))
            _start_bk_saved = float(_start_bk_input)
            st.success("Startbankroll opgeslagen!")
            st.rerun()

    st.markdown("---")

    # ── Filters ──────────────────────────────────────────────────────────────
    st.markdown("#### 🔎 Filters")
    _bkf1, _bkf2, _bkf3, _bkf4 = st.columns(4)
    _bk_sport  = _bkf1.selectbox("Sport",    ["Alles","NHL","NBA","MLB","Voetbal"], key="bk_sport")
    _bk_btype  = _bkf2.selectbox("Bet type", ["Alles","Goals","Assists","Shots on Goal",
                                               "Blocked Shots","Hits","Points","Home Runs",
                                               "Strikeouts","Over/Under"], key="bk_btype")
    _bk_period = _bkf3.selectbox("Periode",  ["Alles","Laatste 7 dagen","Laatste 30 dagen"], key="bk_period")
    _bk_kind   = _bkf4.selectbox("Type",     ["Alles","Singles","Parlays"], key="bk_kind")
    st.markdown("---")

    _today_bk = datetime.date.today()

    def _bk_filter(r: dict) -> bool:
        if _bk_sport != "Alles" and _bk_sport.lower() not in (r.get("sport","") or "").lower():
            return False
        if _bk_btype != "Alles" and _bk_btype.lower() not in (r.get("bet_type","") or "").lower():
            return False
        if _bk_period != "Alles":
            try:
                rd   = datetime.date.fromisoformat((r.get("datum") or "")[:10])
                days = 7 if "7" in _bk_period else 30
                if (_today_bk - rd).days > days:
                    return False
            except Exception:
                pass
        if _bk_kind == "Singles" and r.get("is_parlay"): return False
        if _bk_kind == "Parlays" and not r.get("is_parlay"): return False
        return True

    _alle_res = [r for r in db.load_resultaten() if _bk_filter(r)]
    _gedaan   = [r for r in _alle_res if r.get("uitkomst") in ("gewonnen", "verloren")]

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _calc_streak(results: list) -> tuple:
        """Bereken huidige streak en langste streak. Returns (huidig, langste, soort)."""
        if not results:
            return 0, 0, "—"
        sorted_r = sorted(results, key=lambda r: r.get("datum",""))
        current, best, last = 1, 1, sorted_r[-1].get("uitkomst","")
        for i in range(len(sorted_r) - 2, -1, -1):
            if sorted_r[i].get("uitkomst","") == last:
                current += 1
            else:
                break
        for i in range(len(sorted_r) - 1):
            run = 1
            while i + run < len(sorted_r) and sorted_r[i+run].get("uitkomst") == sorted_r[i].get("uitkomst"):
                run += 1
            best = max(best, run)
        return current, best, last

    def _calc_drawdown(results: list) -> float:
        """Bereken maximale drawdown (grootste piek-naar-dal verlies)."""
        if len(results) < 2:
            return 0.0
        sorted_r = sorted(results, key=lambda r: r.get("datum",""))
        peak, max_dd, cum = 0.0, 0.0, 0.0
        for r in sorted_r:
            cum  += r.get("winst_verlies", 0)
            peak  = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        return round(max_dd, 2)

    if not _gedaan:
        st.info("Nog geen afgeronde weddenschappen. Voeg ze toe via ➕ hierboven of markeer props in ⭐ Favorieten.")
    else:
        # ── Overzicht metrics ────────────────────────────────────────────────
        st.markdown("#### 🎯 Overzicht")
        _bn_won   = sum(1 for r in _gedaan if r.get("uitkomst") == "gewonnen")
        _bt_inzet = sum(r.get("inzet", 0) for r in _gedaan)
        _bt_wl    = sum(r.get("winst_verlies", 0) for r in _gedaan)
        _broi     = (_bt_wl / _bt_inzet * 100) if _bt_inzet > 0 else 0.0
        _bwin_pct = (_bn_won / len(_gedaan) * 100) if _gedaan else 0.0

        # Huidig saldo (alleen als startbankroll ingesteld)
        if _start_bk_saved > 0:
            _huidig_saldo = _start_bk_saved + _bt_wl
            _groei_pct    = (_bt_wl / _start_bk_saved * 100) if _start_bk_saved > 0 else 0.0
            _bmc1, _bmc2, _bmc3, _bmc4 = st.columns(4)
            _bmc1.metric("🏦 Startbankroll",  f"€{_start_bk_saved:.2f}")
            _bmc2.metric("💰 Huidig saldo",   f"€{_huidig_saldo:.2f}", delta=f"€{_bt_wl:+.2f}")
            _bmc3.metric("📈 Groei",           f"{_groei_pct:+.1f}%")
            _bmc4.metric("🎯 Win %",           f"{_bwin_pct:.1f}%")
            st.markdown("")

        _bc1, _bc2, _bc3, _bc4 = st.columns(4)
        _bc1.metric("💰 Totaal P&L",  f"€{_bt_wl:+.2f}")
        _bc2.metric("📈 ROI",          f"{_broi:+.1f}%")
        _bc3.metric("📊 W / L",        f"{_bn_won} / {len(_gedaan) - _bn_won}")
        _bc4.metric("🎰 Bets gespeeld", len(_gedaan))

        # Streak + drawdown
        _cur_streak, _best_streak, _streak_type = _calc_streak(_gedaan)
        _max_dd = _calc_drawdown(_gedaan)
        _streak_icon = "🔥" if _streak_type == "gewonnen" else "❄️"
        _bx1, _bx2, _bx3 = st.columns(3)
        _bx1.metric(f"{_streak_icon} Huidige streak", f"{_cur_streak}× {_streak_type}")
        _bx2.metric("🏆 Langste streak",              f"{_best_streak}")
        _bx3.metric("📉 Max drawdown",                f"€{_max_dd:.2f}",
                    help="Grootste verlies van piek naar dal in je P&L curve")

        # ── P&L grafiek (met bankrollniveau) ────────────────────────────────
        st.markdown("---")
        st.markdown("#### 📈 P&L over tijd")
        _sorted_res = sorted(_gedaan, key=lambda r: r.get("datum", ""))
        if len(_sorted_res) >= 2:
            _cum_wl, _rows = 0.0, []
            for _r in _sorted_res:
                _cum_wl += _r.get("winst_verlies", 0)
                _row = {"Datum": _r.get("datum",""), "Cumulatief P&L (€)": round(_cum_wl, 2)}
                if _start_bk_saved > 0:
                    _row["Bankroll (€)"] = round(_start_bk_saved + _cum_wl, 2)
                _rows.append(_row)
            _chart_df = pd.DataFrame(_rows).set_index("Datum")
            st.line_chart(_chart_df)
        else:
            st.caption("Minimaal 2 afgeronde weddenschappen nodig voor een grafiek.")

        # ── Per sport ────────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 🏟️ Per sport")
        for _bsport in sorted({r.get("sport","?") for r in _gedaan}):
            _sr  = [r for r in _gedaan if r.get("sport","") == _bsport]
            _sw  = sum(1 for r in _sr if r.get("uitkomst") == "gewonnen")
            _si  = sum(r.get("inzet", 0) for r in _sr)
            _swl = sum(r.get("winst_verlies", 0) for r in _sr)
            _sroi = (_swl / _si * 100) if _si > 0 else 0.0
            _icon = SPORT_ICONS.get(_bsport.upper(), "⚽")
            with st.expander(f"{_icon} {_bsport}  —  P&L: €{_swl:+.2f}  |  ROI: {_sroi:+.1f}%", expanded=True):
                _sc1, _sc2, _sc3 = st.columns(3)
                _sc1.metric("W / L",        f"{_sw} / {len(_sr) - _sw}")
                _sc2.metric("Totale inzet", f"€{_si:.2f}")
                _sc3.metric("P&L",          f"€{_swl:+.2f}")
                _btype_wl = {}
                for _r in _sr:
                    _bt = _r.get("bet","?")
                    _btype_wl[_bt] = _btype_wl.get(_bt, 0.0) + _r.get("winst_verlies", 0)
                if _btype_wl:
                    _best_bt = max(_btype_wl, key=lambda k: _btype_wl[k])
                    if _btype_wl[_best_bt] > 0:
                        st.caption(f"✨ Meest winstgevend: **{_best_bt}** (€{_btype_wl[_best_bt]:+.2f})")

        # ── Winst per odds-range ─────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 🎰 Winst per odds-range")
        _odds_buckets = [
            ("1.01–1.49", 1.01, 1.49), ("1.50–1.74", 1.50, 1.74),
            ("1.75–1.99", 1.75, 1.99), ("2.00–2.49", 2.00, 2.49),
            ("2.50–2.99", 2.50, 2.99), ("3.00+",     3.00, 99.0),
        ]
        _odds_rows = []
        for _label, _lo, _hi in _odds_buckets:
            _br = [r for r in _gedaan if _lo <= float(r.get("odds",0) or 0) <= _hi]
            if not _br: continue
            _bw  = sum(1 for r in _br if r.get("uitkomst") == "gewonnen")
            _bwv = sum(r.get("winst_verlies",0) for r in _br)
            _bi  = sum(r.get("inzet",0) for r in _br)
            _odds_rows.append({
                "Odds-range": _label, "N": len(_br),
                "Win %":  f"{_bw/len(_br)*100:.0f}%",
                "P&L":    f"€{_bwv:+.2f}",
                "ROI":    f"{(_bwv/_bi*100) if _bi else 0:+.1f}%",
            })
        if _odds_rows:
            st.dataframe(pd.DataFrame(_odds_rows), hide_index=True, use_container_width=True)
        else:
            st.caption("Nog niet genoeg data voor odds-range analyse.")

        # ── EV analyse ───────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 🔬 EV Analyse (voorspeld vs werkelijk)")
        _ev_rows = []
        for _bsport in sorted({r.get("sport","?") for r in _gedaan}):
            _sr = [r for r in _gedaan if r.get("sport","") == _bsport]
            if len(_sr) < 3: continue
            _pred_hrs = [
                (float(_r.get("ev_score",0)) + 1) / float(_r.get("odds",2.0))
                for _r in _sr if float(_r.get("odds",2.0)) > 1.0
            ]
            if not _pred_hrs: continue
            _pred_hr   = sum(_pred_hrs) / len(_pred_hrs)
            _actual_hr = sum(1 for r in _sr if r.get("uitkomst") == "gewonnen") / len(_sr)
            _diff      = _actual_hr - _pred_hr
            _ev_rows.append({
                "Sport": _bsport, "Voorspeld HR": f"{_pred_hr*100:.1f}%",
                "Werkelijk HR": f"{_actual_hr*100:.1f}%", "Verschil": f"{_diff*100:+.1f}%", "N": len(_sr),
            })
            if _pred_hr > 0 and _actual_hr < _pred_hr * 0.80:
                st.warning(f"⚠️ {_bsport} props presteren {abs(_diff)*100:.0f}% onder verwachte hit rate")
        if _ev_rows:
            st.dataframe(pd.DataFrame(_ev_rows), hide_index=True, use_container_width=True)

        # ── Per bet type ─────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 📊 Per bet type")
        _bt_agg: dict = {}
        for _r in _gedaan:
            _bt = (_r.get("bet") or _r.get("bet_type") or "Onbekend").split(" ")[0]
            _bt_agg.setdefault(_bt, {"n":0,"won":0,"ev":0.0,"wv":0.0,"inzet":0.0})
            _bt_agg[_bt]["n"] += 1
            if (_r.get("uitkomst") or "") == "gewonnen": _bt_agg[_bt]["won"] += 1
            _bt_agg[_bt]["ev"]    += float(_r.get("ev_score") or 0)
            _bt_agg[_bt]["wv"]    += float(_r.get("winst_verlies") or 0)
            _bt_agg[_bt]["inzet"] += float(_r.get("inzet") or 0)
        _bt_rows = [
            {"Bet Type": _bt, "N": _s["n"],
             "Win %":  f"{_s['won']/_s['n']*100:.0f}%"  if _s["n"] else "0%",
             "ROI":    f"{(_s['wv']/_s['inzet']*100) if _s['inzet'] else 0:+.1f}%",
             "P&L":    f"€{_s['wv']:+.2f}",
             "Gem. EV": f"{_s['ev']/_s['n']:.3f}" if _s["n"] else "0.000"}
            for _bt, _s in sorted(_bt_agg.items(), key=lambda x: x[1]["wv"], reverse=True)
        ]
        if _bt_rows:
            st.dataframe(pd.DataFrame(_bt_rows), use_container_width=True, hide_index=True)

        # ── Parlay ROI ───────────────────────────────────────────────────────
        _all_parlays_bk = db.load_parlays()
        if _all_parlays_bk:
            st.markdown("---")
            st.markdown("#### 🎯 Parlay ROI")
            _p_n    = len(_all_parlays_bk)
            _p_won  = sum(1 for p in _all_parlays_bk if (p.get("uitkomst") or "") == "gewonnen")
            _p_inzet = sum(float(p.get("inzet") or 10) for p in _all_parlays_bk)
            _p_wv   = sum(float(p.get("winst_verlies") or 0) for p in _all_parlays_bk)
            _p_roi  = _p_wv / _p_inzet * 100 if _p_inzet else 0
            _pc1, _pc2, _pc3, _pc4 = st.columns(4)
            _pc1.metric("Parlays gespeeld", _p_n)
            _pc2.metric("Gewonnen",          f"{_p_won}/{_p_n}")
            _pc3.metric("Totaal W/V",        f"€{_p_wv:.2f}")
            _pc4.metric("Parlay ROI",        f"{_p_roi:.1f}%")

    # ── Kelly Criterion Calculator ───────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🧮 Kelly Criterion Calculator")
    st.caption("Bereken de optimale inzet als percentage van je bankroll op basis van je geschatte hit rate.")
    _kc1, _kc2, _kc3 = st.columns(3)
    _k_odds    = _kc1.number_input("Odds",        min_value=1.01, max_value=50.0, value=1.90,
                                    step=0.05, format="%.2f", key="kelly_odds")
    _k_hitrate = _kc2.number_input("Hit rate (%)", min_value=1.0, max_value=99.0, value=55.0,
                                    step=1.0, format="%.1f", key="kelly_hr") / 100.0
    _k_fraction = _kc3.selectbox("Kelly fractie",
                                  ["Volle Kelly (agressief)", "Halve Kelly (gematigd)", "Kwart Kelly (conservatief)"],
                                  index=1, key="kelly_frac")
    _k_frac_val = 1.0 if "Volle" in _k_fraction else (0.5 if "Halve" in _k_fraction else 0.25)
    _k_b        = _k_odds - 1.0          # winst per eenheid inzet
    _k_q        = 1.0 - _k_hitrate
    _k_full     = (_k_b * _k_hitrate - _k_q) / _k_b if _k_b > 0 else 0.0
    _k_advised  = max(0.0, _k_full * _k_frac_val)
    _kr1, _kr2, _kr3, _kr4 = st.columns(4)
    _kr1.metric("Full Kelly %",    f"{_k_full*100:.1f}%")
    _kr2.metric("Geadviseerde %",  f"{_k_advised*100:.1f}%")
    if _start_bk_saved > 0:
        _k_euro = _start_bk_saved * _k_advised + sum(r.get("winst_verlies",0) for r in db.load_resultaten() if r.get("uitkomst") in ("gewonnen","verloren"))
        _k_euro = max(0.0, _k_euro * _k_advised)
        _kr3.metric("Inzet bij huidig saldo", f"€{_k_euro:.2f}")
    if _k_full <= 0:
        _kr4.metric("EV", "❌ Negatief", help="Geen positieve verwachte waarde bij deze odds + hit rate.")
    else:
        _k_ev = _k_hitrate * _k_b - _k_q
        _kr4.metric("EV per €1 inzet", f"€{_k_ev:+.3f}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — PARLAY BUILDER
# ══════════════════════════════════════════════════════════════════════════════

with tab_parlay:
    st.markdown("### 🎯 Parlay Builder")
    st.caption("Combineer props tot een parlay en bereken de gecombineerde EV")

    _la = st.session_state.get("last_analysis") or {}
    # Gebruik de volledige enriched lijst — ook props met negatieve EV zijn selecteerbaar
    all_props_parlay = _la.get("enriched") or []

    # ── Handmatig prop toevoegen (altijd zichtbaar, bovenaan) ─────────────────
    st.markdown("#### ➕ Prop toevoegen aan parlay")
    _ph1, _ph2 = st.columns(2)
    _p_speler  = _ph1.text_input("Speler / Team", key="p_speler",
                                  placeholder="bijv. Auston Matthews")
    _p_sport   = _ph2.selectbox("Sport", ["NHL","NBA","MLB","Voetbal","Overig"],
                                 key="p_sport")
    _ph3, _ph4, _ph5 = st.columns(3)
    _p_bet     = _ph3.text_input("Bet type", key="p_bet",
                                  placeholder="bijv. Anytime Goal Scorer")
    _p_odds    = _ph4.number_input("Odds", min_value=1.01, max_value=50.0,
                                    value=2.00, step=0.05, format="%.2f",
                                    key="p_odds")
    _p_hr      = _ph5.number_input("Hit rate % (optioneel)", min_value=0, max_value=100,
                                    value=50, step=1, key="p_hr",
                                    help="Schatting van de kans dat de prop slaagt. Gebruik 50% als je het niet weet.")
    if st.button("➕ Voeg toe aan parlay", key="p_add_leg",
                 type="primary", use_container_width=True,
                 disabled=not (_p_speler and _p_bet)):
        st.session_state.parlay_legs.append({
            "player":   _p_speler,
            "sport":    _p_sport,
            "bet_type": _p_bet,
            "odds":     float(_p_odds),
            "hit_rate": float(_p_hr) / 100,
        })
        st.rerun()

    # ── Props uit analyse (optioneel, ingeklapt) ──────────────────────────────
    if all_props_parlay:
        with st.expander(f"📊 Props uit laatste analyse toevoegen ({len(all_props_parlay)} beschikbaar)", expanded=False):
            zoek   = st.text_input("🔍 Zoek speler of sport", key="parlay_search",
                                    placeholder="Bijv. McDavid, NHL, Goals...")
            zoek_l = zoek.lower() if zoek else ""
            filtered_p = [
                b for b in all_props_parlay
                if not zoek_l
                or zoek_l in (b.get("player") or "").lower()
                or zoek_l in (b.get("sport") or "").lower()
                or zoek_l in (b.get("bet_type") or "").lower()
            ]
            if filtered_p:
                for b in filtered_p:
                    _ev_b   = float(b.get("ev") or 0)
                    _ev_clr = "🟢" if _ev_b >= 0.05 else ("🟡" if _ev_b >= 0 else "🔴")
                    _already = any(
                        l.get("player") == b.get("player") and l.get("bet_type") == b.get("bet_type")
                        for l in st.session_state.parlay_legs
                    )
                    _pc1, _pc2, _pc3, _pc4 = st.columns([3, 1, 1, 1])
                    _pc1.write(f"{b.get('player','?')} — {b.get('bet_type','?')}")
                    _pc2.write(f"Odds: {b.get('odds','—')}")
                    _pc3.write(f"{_ev_clr} EV {_ev_b:+.3f}")
                    if not _already:
                        if _pc4.button("+ Voeg toe",
                                       key=f"addleg_{b.get('player','')}_{b.get('bet_type','')}"):
                            st.session_state.parlay_legs.append({
                                "player":   b.get("player",""),
                                "sport":    b.get("sport",""),
                                "bet_type": b.get("bet_type",""),
                                "odds":     float(b.get("odds") or 1.5),
                                "hit_rate": float(b.get("composite") or b.get("linemate_hr") or 0.5),
                            })
                            st.rerun()
                    else:
                        _pc4.caption("✅ Toegevoegd")

    st.markdown("---")

    if st.session_state.parlay_legs:
        st.markdown("#### 🧩 Jouw Parlay")
        legs_to_remove = []
        for _li, _leg in enumerate(st.session_state.parlay_legs):
            _lc1, _lc2, _lc3, _lc4 = st.columns([3, 1, 1, 0.5])
            _lc1.write(f"**{_leg.get('player','')}** — {_leg.get('bet_type','')}")
            _new_odds = _lc2.number_input(
                "Odds", min_value=1.01, max_value=50.0,
                value=float(_leg.get("odds",1.5)), step=0.05, format="%.2f",
                key=f"pleg_odds_{_li}",
            )
            st.session_state.parlay_legs[_li]["odds"] = _new_odds
            _lc3.caption(f"HR: {_leg.get('hit_rate',0)*100:.0f}%")
            if _lc4.button("🗑️", key=f"rmleg_{_li}"):
                legs_to_remove.append(_li)
        for _idx in sorted(legs_to_remove, reverse=True):
            st.session_state.parlay_legs.pop(_idx)
        if legs_to_remove:
            st.rerun()

        _legs      = st.session_state.parlay_legs
        _comb_odds = 1.0
        _hit_ch    = 1.0
        for _leg in _legs:
            _comb_odds *= float(_leg.get("odds", 1.5))
            _hit_ch    *= float(_leg.get("hit_rate", 0.5))
        _p_ev  = _hit_ch * (_comb_odds - 1) - (1 - _hit_ch)
        _inzet = st.number_input("💰 Inzet (€)", min_value=1.0, max_value=10000.0,
                                  value=10.0, step=1.0, key="parlay_inzet")
        _winst = _inzet * _comb_odds - _inzet
        _ev_s2 = f"+{_p_ev:.3f}" if _p_ev >= 0 else f"{_p_ev:.3f}"

        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _mc1.metric("Gecombineerde Odds", f"{_comb_odds:.2f}")
        _mc2.metric("Hit Kans",           f"{_hit_ch*100:.1f}%")
        _mc3.metric("Parlay EV",          _ev_s2)
        _mc4.metric(f"Winst bij €{_inzet:.0f}", f"€{_winst:.2f}")

        if _p_ev < 0:
            st.warning(f"⚠️ Negatieve EV ({_ev_s2}) — verliesgevend op lange termijn.")
        else:
            st.success(f"✅ Positieve EV ({_ev_s2})")

        _pb1, _pb2 = st.columns(2)
        if _pb1.button("⭐ Sla parlay op", use_container_width=True, type="primary"):
            db.save_parlay({
                "id":                 str(uuid.uuid4())[:8],
                "datum":              datetime.datetime.now().isoformat(),
                "props_json":         list(_legs),
                "gecombineerde_odds": round(_comb_odds, 4),
                "hit_kans":           round(_hit_ch, 6),
                "ev_score":           round(_p_ev, 6),
                "inzet":              float(_inzet),
                "uitkomst":           "open",
                "winst_verlies":      0.0,
                "legs_json":          {l.get("player","")+"_"+l.get("bet_type",""): "open" for l in _legs},
            })
            st.session_state.parlay_legs = []
            st.success("✅ Parlay opgeslagen!")
            st.rerun()
        if _pb2.button("🗑️ Wis parlay", use_container_width=True):
            st.session_state.parlay_legs = []
            st.rerun()
    else:
        st.info("Voeg props toe om een parlay te bouwen.")

    # Opgeslagen parlays
    _saved_parlays = db.load_parlays()
    if _saved_parlays:
        st.markdown("---")
        st.markdown("#### 📋 Opgeslagen Parlays")
        for _prl in _saved_parlays:
            _prl_legs = _prl.get("props_json") or []
            _prl_lj   = _prl.get("legs_json") or {}
            if isinstance(_prl_lj, str):
                try: _prl_lj = json.loads(_prl_lj)
                except Exception: _prl_lj = {}
            _prl_ev_s = f"+{_prl.get('ev_score',0):.3f}" if (_prl.get('ev_score') or 0) >= 0 else f"{_prl.get('ev_score',0):.3f}"
            with st.expander(
                f"🎯 {len(_prl_legs)} legs · Odds {_prl.get('gecombineerde_odds',0):.2f}"
                f" · EV {_prl_ev_s} · {(_prl.get('uitkomst') or 'open').upper()}"
            ):
                _upd_legs = dict(_prl_lj)
                _changed  = False
                for _pleg in _prl_legs:
                    _lk  = str(_pleg.get("player","")) + "_" + str(_pleg.get("bet_type",""))
                    _lst = _upd_legs.get(_lk, "open")
                    _plc1, _plc2 = st.columns([3, 2])
                    _plc1.write(f"**{_pleg.get('player','')}** — {_pleg.get('bet_type','')} @ {_pleg.get('odds','—')}")
                    _nst = _plc2.selectbox(
                        "Status", options=["open","geraakt","gemist"],
                        index=["open","geraakt","gemist"].index(_lst) if _lst in ["open","geraakt","gemist"] else 0,
                        key=f"legst_{_prl.get('id','')}_{_lk}", label_visibility="collapsed",
                    )
                    if _nst != _lst:
                        _upd_legs[_lk] = _nst
                        _changed = True
                if _changed:
                    db.update_parlay(_prl.get("id",""), {"legs_json": _upd_legs})
                    st.rerun()

                _oc1, _oc2, _oc3 = st.columns(3)
                if (_prl.get("uitkomst") or "open") == "open":
                    if _oc1.button("✅ Gewonnen", key=f"pwon_{_prl.get('id','')}"):
                        _pw = _prl.get("inzet",10) * _prl.get("gecombineerde_odds",1) - _prl.get("inzet",10)
                        db.update_parlay(_prl.get("id",""), {"uitkomst":"gewonnen","winst_verlies":round(_pw,2)})
                        st.rerun()
                    if _oc2.button("❌ Verloren", key=f"plost_{_prl.get('id','')}"):
                        db.update_parlay(_prl.get("id",""), {"uitkomst":"verloren","winst_verlies":-_prl.get("inzet",10)})
                        st.rerun()
                else:
                    _wv = _prl.get("winst_verlies", 0) or 0
                    _kl = "#4ade80" if _wv >= 0 else "#f87171"
                    st.markdown(f"<span style='color:{_kl};font-weight:700'>Uitkomst: {(_prl.get('uitkomst') or '').upper()} · W/V: €{_wv:.2f}</span>", unsafe_allow_html=True)
                if _oc3.button("🗑️ Verwijder", key=f"pdel_{_prl.get('id','')}"):
                    db.delete_parlay(_prl.get("id",""))
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — GEPLAATSTE BETS
# ══════════════════════════════════════════════════════════════════════════════

with tab_geplaatst:
    st.markdown("### 📋 Geplaatste Weddenschappen")
    st.caption("Alle weddenschappen die je hebt ingezet, gerangschikt per maand en week.")

    from datetime import date as _date

    _alle_res_gp = db.load_resultaten()

    if not _alle_res_gp:
        st.info("Nog geen weddenschappen geregistreerd. Voeg bets toe via de Shortlist of Bankroll tab.")
    else:
        # ── Filters ──────────────────────────────────────────────────────────
        _gp_c1, _gp_c2, _gp_c3 = st.columns(3)
        _gp_sport    = _gp_c1.selectbox("Sport", ["Alles","NHL","NBA","MLB","Voetbal","Overig"], key="gp_sport")
        _gp_uitkomst = _gp_c2.selectbox("Uitkomst", ["Alles","open","gewonnen","verloren"], key="gp_uitkomst")
        _gp_zoek     = _gp_c3.text_input("🔍 Zoek speler", key="gp_zoek", placeholder="naam...")

        _gp_data = _alle_res_gp
        if _gp_sport != "Alles":
            _gp_data = [r for r in _gp_data if _gp_sport.lower() in (r.get("sport") or "").lower()]
        if _gp_uitkomst != "Alles":
            _gp_data = [r for r in _gp_data if r.get("uitkomst","") == _gp_uitkomst]
        if _gp_zoek:
            _gp_data = [r for r in _gp_data if _gp_zoek.lower() in (r.get("speler") or "").lower()]

        if not _gp_data:
            st.info("Geen weddenschappen gevonden met deze filters.")
        else:
            # ── Totaalsamenvatting ────────────────────────────────────────────
            _gp_afgerond = [r for r in _gp_data if r.get("uitkomst") in ("gewonnen","verloren")]
            if _gp_afgerond:
                _gp_won   = sum(1 for r in _gp_afgerond if r.get("uitkomst") == "gewonnen")
                _gp_inzet = sum(r.get("inzet", 0) for r in _gp_afgerond)
                _gp_wl    = sum(r.get("winst_verlies", 0) for r in _gp_afgerond)
                _gp_roi   = (_gp_wl / _gp_inzet * 100) if _gp_inzet > 0 else 0.0
                _gp_wr    = (_gp_won / len(_gp_afgerond) * 100) if _gp_afgerond else 0.0
                sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                sc1.metric("Totaal bets",   len(_gp_data))
                sc2.metric("Win rate",      f"{_gp_wr:.0f}%")
                sc3.metric("Totale inzet",  f"€{_gp_inzet:.2f}")
                sc4.metric("P&L",           f"€{_gp_wl:+.2f}")
                sc5.metric("ROI",           f"{_gp_roi:+.1f}%")
            st.markdown("---")

            # ── Groepeer op maand → week ──────────────────────────────────────
            def _week_label(datum_str: str) -> str:
                try:
                    d  = _date.fromisoformat(datum_str)
                    wn = d.isocalendar()[1]
                    # Maandag en zondag van die week
                    maandag = d - datetime.timedelta(days=d.weekday())
                    zondag  = maandag + datetime.timedelta(days=6)
                    return f"Week {wn} · {maandag.strftime('%-d %b')} – {zondag.strftime('%-d %b')}"
                except Exception:
                    return "Onbekend"

            def _maand_label(datum_str: str) -> str:
                try:
                    d = _date.fromisoformat(datum_str)
                    return d.strftime("%B %Y").capitalize()
                except Exception:
                    return "Onbekend"

            # Sorteren nieuwste eerst
            _gp_data_sorted = sorted(_gp_data, key=lambda r: r.get("datum",""), reverse=True)

            # Groeperen op maand
            from collections import OrderedDict
            _maand_dict: dict = OrderedDict()
            for _r in _gp_data_sorted:
                _ml = _maand_label(_r.get("datum",""))
                _wl = _week_label(_r.get("datum",""))
                if _ml not in _maand_dict:
                    _maand_dict[_ml] = OrderedDict()
                if _wl not in _maand_dict[_ml]:
                    _maand_dict[_ml][_wl] = []
                _maand_dict[_ml][_wl].append(_r)

            for _maand, _weken in _maand_dict.items():
                # Maand samenvatting
                _m_rijen    = [r for wk in _weken.values() for r in wk]
                _m_afgerond = [r for r in _m_rijen if r.get("uitkomst") in ("gewonnen","verloren")]
                _m_won      = sum(1 for r in _m_afgerond if r.get("uitkomst") == "gewonnen")
                _m_inzet    = sum(r.get("inzet",0) for r in _m_afgerond)
                _m_wl       = sum(r.get("winst_verlies",0) for r in _m_afgerond)
                _m_wr_str   = f"{_m_won}/{len(_m_afgerond)}" if _m_afgerond else "—"
                _m_wl_str   = f"€{_m_wl:+.2f}" if _m_afgerond else "—"

                with st.expander(
                    f"📅 **{_maand}**  ·  {len(_m_rijen)} bets  ·  W/L {_m_wr_str}  ·  P&L {_m_wl_str}",
                    expanded=True,
                ):
                    for _week, _bets in _weken.items():
                        # Week samenvatting
                        _w_afgerond = [r for r in _bets if r.get("uitkomst") in ("gewonnen","verloren")]
                        _w_won      = sum(1 for r in _w_afgerond if r.get("uitkomst") == "gewonnen")
                        _w_inzet    = sum(r.get("inzet",0) for r in _w_afgerond)
                        _w_wl       = sum(r.get("winst_verlies",0) for r in _w_afgerond)
                        _w_wr_str   = f"{_w_won}/{len(_w_afgerond)}" if _w_afgerond else "—"
                        _w_wl_str   = f"€{_w_wl:+.2f}" if _w_afgerond else "—"

                        st.markdown(
                            f"<div style='background:#0e0e24;border-left:3px solid #4361ee;"
                            f"padding:6px 12px;margin:8px 0 4px 0;border-radius:4px;'>"
                            f"<strong>{_week}</strong> &nbsp;·&nbsp; {len(_bets)} bets "
                            f"&nbsp;·&nbsp; W/L {_w_wr_str} "
                            f"&nbsp;·&nbsp; P&L {_w_wl_str}</div>",
                            unsafe_allow_html=True,
                        )

                        for _b in _bets:
                            _b_uit  = _b.get("uitkomst","")
                            _b_icon = "✅" if _b_uit == "gewonnen" else ("❌" if _b_uit == "verloren" else "⏳")
                            _b_wl   = _b.get("winst_verlies",0)
                            _b_wl_s = f"€{_b_wl:+.2f}" if _b_uit != "open" else "—"
                            _bc1, _bc2, _bc3, _bc4, _bc5 = st.columns([3, 1, 1, 1, 1])
                            _bc1.write(f"{_b_icon} **{_b.get('speler','')}** — {_b.get('bet','')}")
                            _bc2.write(f"@ {_b.get('odds','—')}")
                            _bc3.write(f"€{_b.get('inzet',0):.2f}")
                            _bc4.write(_b_wl_s)
                            _bc5.caption(_b.get("datum",""))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — ANALYSE GESCHIEDENIS
# ══════════════════════════════════════════════════════════════════════════════

with tab_history:
    st.markdown("### 🗂️ Analyse Geschiedenis")
    st.caption("Analyses blijven bewaard zolang ze recent zijn (< 7 dagen) of gekoppeld zijn aan een geplaatste weddenschap.")

    # ── Bet-type categorieën ──────────────────────────────────────────────────
    _BET_CATEGORIEEN = {
        "Player Prop": ["goals","assists","shots","points","hits","home runs","strikeouts",
                        "rebounds","steals","blocks","prop"],
        "Moneyline":   ["moneyline","ml","win","to win"],
        "Spread":      ["spread","handicap","puck line","run line","point spread"],
        "Total":       ["over","under","total","o/u"],
        "Parlay":      ["parlay","combo","combined"],
    }

    def _bet_categorie(bet_type: str) -> str:
        bt = (bet_type or "").lower()
        for cat, keywords in _BET_CATEGORIEEN.items():
            if any(k in bt for k in keywords):
                return cat
        return "Overig"

    _hf1, _hf2, _hf3, _hf4 = st.columns(4)
    _hist_sport = _hf1.selectbox("Sport",
        ["Alles","NHL","NBA","MLB","Voetbal"], key="hist_sport_flt")
    _hist_cat   = _hf2.selectbox("Bet categorie",
        ["Alles","Player Prop","Moneyline","Spread","Total","Parlay","Overig"],
        key="hist_cat_flt")
    _hist_zoek  = _hf3.text_input("🔍 Speler", key="hist_zoek", placeholder="naam...")
    _hist_sort  = _hf4.selectbox("Sortering", ["Nieuwste eerst","Oudste eerst"], key="hist_sort")
    st.markdown("---")

    _all_hist = db.load_history()
    if _hist_sort == "Oudste eerst":
        _all_hist = list(reversed(_all_hist))

    if not _all_hist:
        st.info("Nog geen analyses opgeslagen. Voer een analyse uit om de geschiedenis te vullen.")
    else:
        _used_sids = db._get_used_session_ids()
        _shown = 0
        for _entry_idx, entry in enumerate(_all_hist):
            datum   = entry.get("datum","")
            tijd    = entry.get("tijd","")
            _sid    = entry.get("session_id","")
            _is_used = _sid in _used_sids and bool(_sid)
            _alle_p = entry.get("alle_props_json") or []
            if isinstance(_alle_p, str):
                try:    _alle_p = json.loads(_alle_p)
                except: _alle_p = []

            # Fallback naar top5 voor oudere analyses
            if not _alle_p:
                _alle_p = [
                    {
                        "player":    b.get("speler", b.get("player","")),
                        "sport":     b.get("sport",""),
                        "bet_type":  b.get("bet", b.get("bet_type","")),
                        "odds":      b.get("odds",""),
                        "ev":        float(str(b.get("ev_score","0")).replace("+","")) if b.get("ev_score") else float(b.get("ev") or 0),
                        "composite": 0,
                        "rating":    b.get("rating",""),
                    }
                    for b in (entry.get("top5") or [])
                ]

            # Filters toepassen
            _filt_p = _alle_p
            if _hist_sport != "Alles":
                _filt_p = [p for p in _filt_p if _hist_sport.lower() in (p.get("sport") or "").lower()]
            if _hist_cat != "Alles":
                _filt_p = [p for p in _filt_p if _bet_categorie(p.get("bet_type","")) == _hist_cat]
            if _hist_zoek:
                _filt_p = [p for p in _filt_p if _hist_zoek.lower() in (p.get("player","") + p.get("speler","")).lower()]
            if not _filt_p:
                continue

            _shown += 1
            _bewaard_badge = " 📌 *bewaard*" if _is_used else ""
            _sport_set = {p.get("sport","") for p in _filt_p if p.get("sport")}
            _sport_str = " · ".join(sorted(_sport_set)) if _sport_set else ""

            with st.expander(
                f"📅 {datum} {tijd}  ·  {len(_filt_p)} props  ·  {_sport_str}{_bewaard_badge}",
                expanded=False,
            ):
                for _idx_hp, _hp in enumerate(_filt_p):
                    _player   = _hp.get("player", _hp.get("speler",""))
                    _bet_type = _hp.get("bet_type", _hp.get("bet",""))
                    _cat_tag  = _bet_categorie(_bet_type)
                    _hpc1, _hpc2, _hpc3, _hpc4, _hpc5 = st.columns([3, 1, 1, 1, 1])
                    _hpc1.write(f"**{_player}** — {_bet_type}")
                    _hpc1.caption(f"{_hp.get('sport','')} · {_cat_tag}")
                    _hpc2.write(f"@ {_hp.get('odds','—')}")
                    _hev   = float(_hp.get("ev") or 0)
                    _hev_s = f"+{_hev:.3f}" if _hev >= 0 else f"{_hev:.3f}"
                    _hpc3.write(f"EV: {_hev_s}")
                    # Shortlist knop
                    _hshk = f"hshort_{_entry_idx}_{_idx_hp}"
                    if _hpc4.button("⭐ Shortlist", key=_hshk):
                        _fid_h = db.make_fav_id(_player, _bet_type)
                        db.add_favoriet(_fid_h, {
                            "player":   _player,
                            "bet_type": _bet_type,
                            "odds":     float(_hp.get("odds") or 1.5),
                            "ev":       _hev,
                            "sport":    _hp.get("sport",""),
                            "source_session_id": _sid,
                        }, source_session_id=_sid)
                        st.success(f"✅ {_player} toegevoegd aan Shortlist")
                        st.rerun()
                    # Parlay knop
                    _hpk = f"hpar_{_entry_idx}_{_idx_hp}"
                    if _hpc5.button("🎯 Parlay", key=_hpk):
                        st.session_state.parlay_legs.append({
                            "player":   _player,
                            "sport":    _hp.get("sport",""),
                            "bet_type": _bet_type,
                            "odds":     float(_hp.get("odds") or 1.5),
                            "hit_rate": float(_hp.get("composite") or 0.5),
                        })
                        st.rerun()

        if _shown == 0:
            st.info("Geen analyses gevonden met deze filters.")
