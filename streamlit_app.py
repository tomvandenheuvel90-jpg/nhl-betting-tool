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

# ─── Globale UI-helper: KPI-kaartje (geen truncatie, hover-tooltip) ──────────

def kpi_card(icon: str, label: str, value: str, sub: str = "",
             positive: bool = None, tooltip: str = "") -> str:
    """Geeft HTML terug voor een KPI-kaartje. Gebruik met st.markdown(..., unsafe_allow_html=True)."""
    val_color  = "#4ade80" if positive is True else ("#f87171" if positive is False else "#ffffff")
    sub_html   = f"<div style='font-size:0.78rem;color:#888;margin-top:2px;'>{sub}</div>" if sub else ""
    title_attr = f'title="{tooltip}"' if tooltip else ""
    return (
        f"<div {title_attr} style='background:#11112b;border:1px solid #2a2a58;border-radius:12px;"
        f"padding:16px 20px;text-align:center;cursor:default;margin-bottom:8px;'>"
        f"<div style='font-size:0.82rem;color:#a0a0c0;margin-bottom:6px;'>{icon} {label}</div>"
        f"<div style='font-size:1.6rem;font-weight:800;color:{val_color};line-height:1.2;'>{value}</div>"
        f"{sub_html}"
        f"</div>"
    )

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
import screenshot_import

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

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _team_caption_suffix(bet: dict) -> str:
    """
    Geeft '  ·  Utah Hockey Club' terug als het team-veld van de bet zinvol is om
    te tonen in een caption-regel. Anders een lege string. Zinvol betekent:
      - niet leeg
      - niet al in de spelernaam/bet-tekst te vinden (voorkomt duplicatie)
      - geen match-string (bevat geen ' vs ', ' - ', ' / ', ' @ ')
    """
    _team = str(bet.get("team") or "").strip()
    if not _team:
        return ""
    _tl = _team.lower()
    _sp = str(bet.get("speler") or bet.get("player") or "").lower()
    _bt = str(bet.get("bet") or bet.get("bet_type") or "").lower()
    if _tl in _sp or _tl in _bt:
        return ""
    for _sep in (" vs ", " v ", " - ", " / ", " @ "):
        if _sep in _tl:
            return ""
    return f"  ·  {_team}"


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

# Schema-drift: toon eventuele waarschuwingen over ontbrekende Supabase-kolommen.
# Deze waarschuwingen verschijnen als db.py een upsert-fallback heeft moeten
# gebruiken omdat een kolom nog niet in de database is aangemaakt.
try:
    _drift_notes = db.get_schema_drift_notes()
    for _note in _drift_notes:
        st.warning(_note)
except Exception:
    pass

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
            st.markdown('<small style="color:#a0c4ff;">ℹ️ <b>Cloud versie</b> — Historische MoneyPuck data niet beschikbaar.</small>', unsafe_allow_html=True)
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
    ("uploader_key",      0),
    ("last_analysis",     None),
    ("parlay_legs",       []),
    ("just_analyzed",     False),
    ("parlay_form_ver",   0),
    ("parlay_last_sport", "NHL"),
    ("bk_view",           "7 Dagen"),
    ("bk_selected_day",   None),
    ("injuries_enabled",  False),
    ("gp_editing",        None),
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

# Banner — JPG heeft voorrang boven SVG, max-hoogte beperkt zodat de afbeelding compact blijft
import os as _os
if _os.path.exists("assets/banner.jpg"):
    st.markdown('<div style="max-height:150px;overflow:hidden;border-radius:8px;margin-bottom:0.6rem;">', unsafe_allow_html=True)
    st.image("assets/banner.jpg", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
elif _os.path.exists("assets/banner.svg"):
    try:
        with open("assets/banner.svg", "r") as _f:
            _svg = _f.read()
        st.markdown(f'<div style="max-height:150px;overflow:hidden;border-radius:8px;margin-bottom:0.6rem;">{_svg}</div>', unsafe_allow_html=True)
    except Exception:
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

    # ── Gesettlede parlays samenvoegen ────────────────────────────────────────
    # Parlays worden opgeslagen in de `parlays` tabel. Ze komen alleen in
    # `resultaten` terecht via upsert_resultaat() bij settlement. Als dat (nog)
    # niet is gebeurd — bijv. voor bestaande parlays of na een mislukte write —
    # voegen we ze hier handmatig toe zodat Dashboard altijd volledig is.
    _dsh_bestaande_parlay_ids = {
        r.get("id", "") for r in _dsh_resultaten
        if str(r.get("id", "")).startswith("parlay_")
    }
    for _dp in db.load_parlays():
        # Alle parlays (open én gesettled) samenvoegen zodat open parlays
        # zichtbaar zijn als open bets en gesettlede parlays niet ontbreken.
        _dp_res_id = f"parlay_{_dp['id']}"
        if _dp_res_id in _dsh_bestaande_parlay_ids:
            continue  # al aanwezig in resultaten, niet dubbel tellen
        _dp_legs = _dp.get("props_json") or []
        _dp_inzet = float(_dp.get("inzet") or 0)
        _dp_odds  = float(_dp.get("gecombineerde_odds") or 1.0)
        _dp_wl    = float(_dp.get("winst_verlies") or 0)
        # Gesettlede parlays die niet in resultaten staan krijgen vandaag als datum
        # zodat ze in het recente overzicht verschijnen en niet door een oude aanmaaKdatum wegvallen.
        _dp_uitkomst = _dp.get("uitkomst") or "open"
        _dp_datum = (
            datetime.date.today().isoformat()
            if _dp_uitkomst in ("gewonnen", "verloren", "void")
            else (_dp.get("datum") or datetime.date.today().isoformat())[:10]
        )
        _dsh_resultaten.append({
            "id":            _dp_res_id,
            "datum":         _dp_datum,
            "speler":        f"🎰 Parlay ({len(_dp_legs)} legs)",
            "bet":           ", ".join(str(l.get("player", "")) for l in _dp_legs[:3]) or "Parlay",
            "sport":         "Parlay",
            "odds":          _dp_odds,
            "inzet":         _dp_inzet,
            "uitkomst":      _dp_uitkomst,
            "winst_verlies": _dp_wl,
            "ev_score":      float(_dp.get("ev_score") or 0),
            "is_parlay":     True,
        })

    _dsh_open        = [r for r in _dsh_resultaten if r.get("uitkomst") == "open"]
    _dsh_gedaan      = [r for r in _dsh_resultaten if r.get("uitkomst") in ("gewonnen","verloren","void")]
    _dsh_start_bk    = float(db.get_setting("start_bankroll") or 0.0)

    # ── KPI berekeningen ──────────────────────────────────────────────────────
    _dsh_won         = sum(1 for r in _dsh_gedaan if r.get("uitkomst") == "gewonnen")
    _dsh_total_inzet = sum(r.get("inzet", 0) for r in _dsh_gedaan)
    _dsh_total_wl    = sum(r.get("winst_verlies", 0) for r in _dsh_gedaan)
    _dsh_roi         = (_dsh_total_wl / _dsh_total_inzet * 100) if _dsh_total_inzet > 0 else 0.0
    _dsh_wr          = (_dsh_won / len(_dsh_gedaan) * 100) if _dsh_gedaan else 0.0
    _dsh_mutations_total = db.get_bankroll_mutations_total()
    # Deducteer openstaande inzetten direct van het saldo (stake al gecommitteerd)
    _dsh_open_inzet   = sum(float(r.get("inzet", 0)) for r in _dsh_open)
    _dsh_huidig_saldo = (_dsh_start_bk + _dsh_mutations_total + _dsh_total_wl - _dsh_open_inzet) if _dsh_start_bk > 0 else None

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

    # All-time streak is verhuisd naar het maand-overzicht hieronder.

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
    _dsh_fav_actief_count = sum(1 for f in _dsh_favorieten if str(f.get("game_date") or f.get("datum") or datetime.date.today().isoformat())[:10] >= datetime.date.today().isoformat())
    st.caption(f"📅 {_dsh_datum_str}  ·  {len(_dsh_open)} open {'bet' if len(_dsh_open) == 1 else 'bets'}  ·  {_dsh_fav_actief_count} in Shortlist")

    # ── KPI-kaartjes (algemeen / All Time) ────────────────────────────────────
    # Streak en Open inzet zijn verhuisd naar het maand-overzicht hieronder.
    _bk_val     = f"€{_dsh_huidig_saldo:.0f}" if _dsh_huidig_saldo is not None else "—"
    _bk_sub     = f"start €{_dsh_start_bk:.0f}  ·  P&L {_dsh_total_wl:+.0f}" if _dsh_start_bk > 0 else "Stel startbankroll in via Bankroll tab"
    _bk_pos     = (True if _dsh_total_wl > 0 else False) if _dsh_start_bk > 0 and _dsh_total_wl != 0 else None
    _wk_val     = f"€{_dsh_week_wl:+.0f}" if _dsh_week_gedaan else "—"
    _wk_sub     = f"{len(_dsh_week_gedaan)} bets deze week" if _dsh_week_gedaan else "Geen bets deze week"
    _wk_pos     = (True if _dsh_week_wl > 0 else False) if _dsh_week_gedaan and _dsh_week_wl != 0 else None
    _roi_val    = f"{_dsh_roi:+.0f}%" if _dsh_gedaan else "—"
    _roi_sub    = f"over {len(_dsh_gedaan)} afgeronde bets" if _dsh_gedaan else ""
    _roi_pos    = (True if _dsh_roi > 0 else False) if _dsh_gedaan and _dsh_roi != 0 else None
    _wr_val     = f"{_dsh_wr:.0f}%" if _dsh_gedaan else "—"
    _wr_sub     = f"{_dsh_won} gewonnen / {len(_dsh_gedaan) - _dsh_won} verloren" if _dsh_gedaan else ""
    _wr_pos     = (True if _dsh_wr >= 55 else False) if _dsh_gedaan else None

    _kr1, _kr2, _kr3, _kr4 = st.columns(4)
    _kr1.markdown(kpi_card("🏦", "Bankroll", _bk_val, _bk_sub, _bk_pos, tooltip=_bk_val), unsafe_allow_html=True)
    _kr2.markdown(kpi_card("💰", "P&L deze week", _wk_val, _wk_sub, _wk_pos, tooltip=_wk_val), unsafe_allow_html=True)
    _kr3.markdown(kpi_card("📈", "ROI (totaal)", _roi_val, _roi_sub, _roi_pos, tooltip=_roi_val), unsafe_allow_html=True)
    _kr4.markdown(kpi_card("🎯", "Win rate", _wr_val, _wr_sub, _wr_pos, tooltip=_wr_val), unsafe_allow_html=True)

    # ── Maand-overzicht ───────────────────────────────────────────────────────
    st.markdown("---")
    _NL_MONTHS = ["januari","februari","maart","april","mei","juni",
                  "juli","augustus","september","oktober","november","december"]

    # Verzamel alle (jaar, maand) combinaties met bets, plus de huidige maand
    _dsh_today_d   = datetime.date.today()
    _dsh_months_set = {(_dsh_today_d.year, _dsh_today_d.month)}
    for _r in _dsh_resultaten:
        _dt = (_r.get("datum") or "")[:10]
        try:
            _d_obj = datetime.date.fromisoformat(_dt)
            _dsh_months_set.add((_d_obj.year, _d_obj.month))
        except Exception:
            pass
    _dsh_months_sorted = sorted(_dsh_months_set, reverse=True)
    _dsh_month_labels  = [f"{_NL_MONTHS[m-1].capitalize()} {y}"
                          for y, m in _dsh_months_sorted]

    _mh1, _mh2 = st.columns([2, 2])
    with _mh1:
        st.markdown("#### 📅 Maand-overzicht")
    with _mh2:
        _dsh_sel_lbl = st.selectbox(
            "Selecteer maand", _dsh_month_labels,
            index=0, key="dsh_month_sel",
            label_visibility="collapsed",
        )
    _dsh_sel_idx = _dsh_month_labels.index(_dsh_sel_lbl)
    _dsh_sel_y, _dsh_sel_m = _dsh_months_sorted[_dsh_sel_idx]

    def _in_sel_month(r: dict) -> bool:
        _dt = (r.get("datum") or "")[:10]
        try:
            _o = datetime.date.fromisoformat(_dt)
            return _o.year == _dsh_sel_y and _o.month == _dsh_sel_m
        except Exception:
            return False

    _dsh_m_all     = [r for r in _dsh_resultaten if _in_sel_month(r)]
    _dsh_m_open    = [r for r in _dsh_m_all if r.get("uitkomst") == "open"]
    _dsh_m_settled = [r for r in _dsh_m_all if r.get("uitkomst") in ("gewonnen","verloren","void")]
    _dsh_m_won     = sum(1 for r in _dsh_m_settled if r.get("uitkomst") == "gewonnen")
    _dsh_m_lost    = sum(1 for r in _dsh_m_settled if r.get("uitkomst") == "verloren")
    _dsh_m_stk_set = sum(float(r.get("inzet") or 0) for r in _dsh_m_settled)
    _dsh_m_stk_tot = sum(float(r.get("inzet") or 0) for r in _dsh_m_all)
    _dsh_m_open_st = sum(float(r.get("inzet") or 0) for r in _dsh_m_open)
    _dsh_m_open_pt = sum(float(r.get("inzet") or 0) * float(r.get("odds") or 1.0)
                         for r in _dsh_m_open)
    _dsh_m_wl      = sum(float(r.get("winst_verlies") or 0) for r in _dsh_m_settled)
    _dsh_m_roi     = (_dsh_m_wl / _dsh_m_stk_set * 100) if _dsh_m_stk_set > 0 else 0.0
    _dsh_m_wr      = (_dsh_m_won / len(_dsh_m_settled) * 100) if _dsh_m_settled else 0.0
    _dsh_m_str_cnt, _dsh_m_str_typ = _dsh_streak(_dsh_m_settled)
    _dsh_m_str_ico = ("🔥" if _dsh_m_str_typ == "gewonnen"
                      else "❄️" if _dsh_m_str_typ == "verloren" else "—")

    # Rij 1: Totaal ingezet · P&L · ROI
    _m_pl_val  = f"€{_dsh_m_wl:+.0f}" if _dsh_m_settled else "—"
    _m_pl_pos  = (True if _dsh_m_wl > 0 else False) if (_dsh_m_settled and _dsh_m_wl != 0) else None
    _m_roi_val = f"{_dsh_m_roi:+.0f}%" if _dsh_m_settled else "—"
    _m_roi_pos = (True if _dsh_m_roi > 0 else False) if (_dsh_m_settled and _dsh_m_roi != 0) else None
    _m_stk_sub = (f"{len(_dsh_m_all)} bets ({len(_dsh_m_open)} open)"
                  if _dsh_m_all else "geen bets in deze maand")

    _mk1, _mk2, _mk3 = st.columns(3)
    _mk1.markdown(kpi_card("💶", "Totaal ingezet",
        f"€{_dsh_m_stk_tot:.0f}" if _dsh_m_all else "—",
        _m_stk_sub, tooltip=f"€{_dsh_m_stk_tot:.2f}"), unsafe_allow_html=True)
    _mk2.markdown(kpi_card("💰", "P&L maand", _m_pl_val,
        f"{len(_dsh_m_settled)} afgerond" if _dsh_m_settled else "geen afgeronde bets",
        _m_pl_pos, tooltip=f"€{_dsh_m_wl:+.4f}"), unsafe_allow_html=True)
    _mk3.markdown(kpi_card("📈", "ROI maand", _m_roi_val,
        f"over €{_dsh_m_stk_set:.0f} stake" if _dsh_m_settled else "",
        _m_roi_pos, tooltip=_m_roi_val), unsafe_allow_html=True)

    # Rij 2: Win rate · Streak · Open in maand
    _m_wr_val  = f"{_dsh_m_wr:.0f}%" if _dsh_m_settled else "—"
    _m_wr_sub  = f"{_dsh_m_won} W / {_dsh_m_lost} L" if _dsh_m_settled else ""
    _m_wr_pos  = (True if _dsh_m_wr >= 55 else False) if _dsh_m_settled else None
    _m_str_val = f"{_dsh_m_str_cnt}×" if _dsh_m_settled else "—"
    _m_str_sub = (("gewonnen" if _dsh_m_str_typ == "gewonnen"
                   else "verloren" if _dsh_m_str_typ == "verloren" else "")
                  if _dsh_m_settled else "")
    _m_str_pos = (True if _dsh_m_str_typ == "gewonnen" else False) if _dsh_m_settled else None
    _m_op_val  = f"€{_dsh_m_open_st:.0f}" if _dsh_m_open else "—"
    _m_op_sub  = (f"{len(_dsh_m_open)} bets · pot. uitb. €{_dsh_m_open_pt:.0f}"
                  if _dsh_m_open else "geen open bets")

    _mk4, _mk5, _mk6 = st.columns(3)
    _mk4.markdown(kpi_card("🎯", "Win rate maand", _m_wr_val, _m_wr_sub, _m_wr_pos), unsafe_allow_html=True)
    _mk5.markdown(kpi_card(_dsh_m_str_ico, "Streak maand", _m_str_val, _m_str_sub, _m_str_pos), unsafe_allow_html=True)
    _mk6.markdown(kpi_card("⏳", "Open in maand", _m_op_val, _m_op_sub), unsafe_allow_html=True)

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
            _dop_inzet_tw = _dop.get("inzet")
            _dop_odds_tw  = _dop.get("odds")
            if _dop_inzet_tw is not None and _dop_odds_tw is not None:
                _dop_te_winnen_s = f"€{round(float(_dop_inzet_tw) * (float(_dop_odds_tw) - 1), 2):.2f}"
            else:
                _dop_te_winnen_s = "—"
            _dopa.caption(f"{_dop.get('sport','')}{_team_caption_suffix(_dop)} · @ {_dop.get('odds','—')} · inzet €{_dop.get('inzet',0):.0f} · te winnen {_dop_te_winnen_s} · {_dop_dag}")
            if _dopb.button("✅ Win",     key=f"dsh_won_{_dop_id}",  use_container_width=True):
                _dop_inzet_val = float(_dop.get("inzet", 10))
                _dop_fav = dict(_dop); _dop_fav["datum"] = datetime.date.today().isoformat()
                db.upsert_resultaat(_dop_id, _dop_fav, "gewonnen", _dop_inzet_val)
                if str(_dop_id).startswith("parlay_"):
                    _dop_odds_val = float(_dop.get("odds", 1.0))
                    db.update_parlay(str(_dop_id)[len("parlay_"):], {
                        "uitkomst": "gewonnen",
                        "winst_verlies": round(_dop_inzet_val * (_dop_odds_val - 1), 2),
                    })
                st.rerun()
            if _dopc.button("❌ Loss", key=f"dsh_lost_{_dop_id}", use_container_width=True):
                _dop_inzet_val = float(_dop.get("inzet", 10))
                _dop_fav = dict(_dop); _dop_fav["datum"] = datetime.date.today().isoformat()
                db.upsert_resultaat(_dop_id, _dop_fav, "verloren", _dop_inzet_val)
                if str(_dop_id).startswith("parlay_"):
                    db.update_parlay(str(_dop_id)[len("parlay_"):], {
                        "uitkomst": "verloren",
                        "winst_verlies": round(-_dop_inzet_val, 2),
                    })
                st.rerun()
            if _dopd.button("⚪ Void", key=f"dsh_void_{_dop_id}", use_container_width=True):
                _dop_inzet_val = float(_dop.get("inzet", 10))
                _dop_fav = dict(_dop); _dop_fav["datum"] = datetime.date.today().isoformat()
                db.upsert_resultaat(_dop_id, _dop_fav, "void", _dop_inzet_val)
                if str(_dop_id).startswith("parlay_"):
                    db.update_parlay(str(_dop_id)[len("parlay_"):], {
                        "uitkomst": "void",
                        "winst_verlies": 0.0,
                    })
                st.rerun()
    else:
        st.success("✅ Geen open weddenschappen — alles is up-to-date.")

    st.markdown("---")

    # ── Twee kolommen: recente resultaten + shortlist/analyse ─────────────────
    _dcol_l, _dcol_r = st.columns([3, 2])

    with _dcol_l:
        st.markdown("#### 📊 Laatste resultaten")
        _dsh_recent = sorted(_dsh_gedaan, key=lambda r: r.get("datum",""), reverse=True)[:15]
        if not _dsh_recent:
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen afgeronde weddenschappen.</small>', unsafe_allow_html=True)
        else:
            for _dr in _dsh_recent:
                _dr_icon = "✅" if _dr.get("uitkomst") == "gewonnen" else ("⚪" if _dr.get("uitkomst") == "void" else "❌")
                _dr_wl   = _dr.get("winst_verlies", 0)
                _dr_wl_s = f"+€{abs(_dr_wl):.0f}" if _dr_wl >= 0 else f"-€{abs(_dr_wl):.0f}"
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
        _dsh_today = datetime.date.today().isoformat()
        _dsh_favorieten_actief = [
            f for f in _dsh_favorieten
            if str(f.get("game_date") or f.get("datum") or _dsh_today)[:10] >= _dsh_today
        ]
        if not _dsh_favorieten_actief:
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Shortlist is leeg — voeg props toe via Analyse of Analyse Geschiedenis.</small>', unsafe_allow_html=True)
        else:
            for _df in _dsh_favorieten_actief[:5]:
                _df_ev = float(_df.get("ev_score") or 0)
                _ev_kleur = "#4ade80" if _df_ev >= 0.05 else "#facc15"
                st.markdown(
                    f"<div style='padding:5px 0;border-bottom:1px solid #1e1e3a;'>"
                    f"<b>{_df.get('speler','')}</b> — {_df.get('bet','')}<br>"
                    f"<small style='color:#888;'>@ {_df.get('odds','—')} · "
                    f"<span style='color:{_ev_kleur};'>EV {_df_ev:+.3f}</span></small>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            if len(_dsh_favorieten_actief) > 5:
                st.caption(f"+ {len(_dsh_favorieten_actief) - 5} meer → ga naar Shortlist tab")

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
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen analyse gedaan. Ga naar de Analyse tab om te beginnen.</small>', unsafe_allow_html=True)


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

    st.session_state.injuries_enabled = st.checkbox(
        "🩺 Blessure-check (NHL spelersroster laden)",
        value=st.session_state.injuries_enabled,
        help="Schakel uit om de NHL roster scan over te slaan en de analyse te versnellen.",
    )

    if "bet365_verify_enabled" not in st.session_state:
        st.session_state.bet365_verify_enabled = False
    st.session_state.bet365_verify_enabled = st.checkbox(
        "💰 Bet365 odds verificatie (TheOddsAPI)",
        value=st.session_state.bet365_verify_enabled,
        help="Schakel in om odds te verifiëren via Bet365. Standaard uitgeschakeld — Linemate-odds worden gebruikt.",
    )

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
                    if dbg.get("_dbg_traceback"):
                        _reason = "Exception tijdens extract_bets()"
                    elif dbg.get("_dbg_parse_error"):
                        _reason = "JSON parsing mislukt"
                    elif not dbg.get("_dbg_raw"):
                        _reason = "Claude gaf geen response terug (API-fout?)"
                    else:
                        _reason = f"Claude gaf response maar geen data ({len(dbg.get('_dbg_raw',''))} tekens)"

                    st.error(f"❌ Analyse mislukt: **{_reason}**")
                    with st.expander("🔧 Debug — volledige diagnostiek", expanded=True):
                        st.caption(f"Model: `{dbg.get('_dbg_model','?')}` · response: {len(dbg.get('_dbg_raw',''))} tekens")
                        if dbg.get("_dbg_steps"):
                            st.markdown("**Stap-log:**")
                            st.code("\n".join(dbg["_dbg_steps"]), language="text")
                        if dbg.get("_dbg_parse_error"):
                            st.error(dbg["_dbg_parse_error"])
                        if dbg.get("_dbg_traceback"):
                            st.code(dbg["_dbg_traceback"], language="python")
                        st.markdown("**Claude's ruwe response:**")
                        st.code(dbg.get("_dbg_raw", "(leeg)")[:4000], language="text")

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
                        st.markdown(f'<small style="color:#a0c4ff;">ℹ️ <b>Claude ziet:</b> {_tr.content[0].text}</small>', unsafe_allow_html=True)
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
                            injuries_enabled=st.session_state.injuries_enabled,
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
                        enriched.sort(key=lambda x: (x.get("ev") if x.get("ev") is not None else -999.0), reverse=True)
                        st.write(f"✅ {len(enriched)} props gescoord")

                        # Bet365 verificatie (optioneel — alleen als handmatig ingeschakeld)
                        if st.session_state.get("bet365_verify_enabled") and odds_api and odds_api._API_KEY and not odds_api.is_limit_reached():
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
                                    ev_b = b.get("ev") if b.get("ev") is not None else -998.0
                                    if s == "different_line": return ev_b * 0.85
                                    return ev_b
                                enriched.sort(key=_ev_rank, reverse=True)

                        elif st.session_state.get("bet365_verify_enabled") and odds_api and odds_api._API_KEY and odds_api.is_limit_reached():
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
                _gefilterd_n    = len(enriched) - len(enriched_ranked)
                if _gefilterd_n > 0:
                    st.markdown(
                        f'<small style="color:#a0c4ff;">ℹ️ <b>{len(enriched)} props gescoord</b> — '
                        f'<b>{len(enriched_ranked)} positieve EV</b> · '
                        f'{_gefilterd_n} negatieve EV of klein sample (worden ook getoond)</small>',
                        unsafe_allow_html=True,
                    )
                _auto_parlays   = generate_parlay_suggestions(enriched_ranked)

                # Top 3: geef voorkeur aan sterke props met positieve EV
                top3 = [b for b in enriched_ranked if b["rating"].startswith("✅") and float(b.get("ev") or -1) > 0][:3]
                if not top3:
                    top3 = [b for b in enriched_ranked if float(b.get("ev") or -1) > 0][:3]
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
                    "debug_hit_rates":       dbg.get("_dbg_hit_rates", []),
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
        enriched_ranked       = res.get("enriched_ranked", [])
        top3_out              = res["top3"]
        flashscore_text       = res["flashscore"]
        nhl_match_analyses    = res.get("nhl_match_analyses", [])
        soccer_match_analyses = res.get("soccer_match_analyses", [])
        nba_match_analyses    = res.get("nba_match_analyses", [])
        mlb_match_analyses    = res.get("mlb_match_analyses", [])
        scenario              = res.get("scenario", 3)

        _sc_lbl = SCENARIO_LABELS.get(scenario, "")
        if _sc_lbl:
            st.markdown(f'<small style="color:#a0c4ff;">ℹ️ {_sc_lbl}</small>', unsafe_allow_html=True)
        if scenario == 3:
            st.markdown('<small style="color:#a0c4ff;">⚠️ Tip: upload ook een Flashscore screenshot voor wedstrijdcontext en automatische prop-suggesties.</small>', unsafe_allow_html=True)

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

            # Automatische parlay suggesties — gesplitst per grootte
            _aps = res.get("auto_parlays", [])
            if _aps:
                st.markdown("---")
                st.markdown("### 🎯 Automatische Parlay Suggesties")

                _aps_by_size = {}
                for _apc in _aps:
                    _sz = _apc.get("n_legs", len(_apc.get("props", [])))
                    _aps_by_size.setdefault(_sz, []).append(_apc)

                _par_idx = 0  # globale teller voor unieke widget-keys
                for _sz in sorted(_aps_by_size.keys()):
                    _sz_list = _aps_by_size[_sz]
                    st.caption(f"**{_sz}-leg parlays** — top {len(_sz_list)}")
                    for _apc in _sz_list:
                        _par_idx += 1
                        _ev_s  = f"+{_apc['parlay_ev']:.3f}" if _apc["parlay_ev"] >= 0 else f"{_apc['parlay_ev']:.3f}"
                        _legs  = " + ".join(f"{b.get('player','')} ({b.get('bet_type','')})" for b in _apc["props"])
                        _c1, _c2, _c3, _c4, _c5 = st.columns([4, 1, 1, 1, 1])
                        _leg_label = f"**{_par_idx}.** {_legs}"
                        if _apc.get("same_team_warning"):
                            _leg_label += " ⚠️"
                        _c1.write(_leg_label)
                        _c2.write(f"Odds: {_apc.get('gecombineerde_odds', 0):.2f}")
                        _c3.write(f"Hit: {_apc.get('hit_kans', 0)*100:.1f}%")
                        _c4.write(f"EV: {_ev_s}")
                        if _apc.get("same_team_warning"):
                            _c1.caption("⚠️ Zelfde team — odds al −15% gecorrigeerd (SGP-korting)")
                        if _c5.button("🎯 Naar Builder", key=f"autopar_{_par_idx}",
                                      help="Stuur naar Parlay Builder om inzet in te stellen en op te slaan"):
                            st.session_state.parlay_legs = [
                                {
                                    "player":   b.get("player", ""),
                                    "sport":    b.get("sport", ""),
                                    "bet_type": b.get("bet_type", ""),
                                    "odds":     float(b.get("odds") or 1.5),
                                    "hit_rate": float(b.get("composite") or b.get("linemate_hr") or 0),
                                }
                                for b in _apc.get("props", [])
                            ]
                            st.info(f"✅ Parlay {_par_idx} staat klaar in de 🎯 Parlay Builder — stel daar je inzet in en sla op.")
                            st.rerun()

            # Alle props — gesorteerd van beste naar slechtste EV (alle screenshots gecombineerd)
            # enriched is al gesorteerd op EV (hoog→laag) inclusief negatieve EV props
            _display_props = enriched  # toon alle props, zodat meerdere screenshots correct worden gecombineerd
            st.markdown("---")
            st.markdown("### 📊 Alle props")
            _fav_ids_set = {f["id"] for f in db.load_favorieten()}
            _cur_sid     = st.session_state.get("current_session_id", "")
            _enriched_ids = {(b["player"], b["bet_type"]) for b in enriched_ranked}
            for i, bet in enumerate(_display_props, 1):
                _is_fav   = db.make_fav_id(bet["player"], bet["bet_type"]) in _fav_ids_set
                _in_ranked = (bet["player"], bet["bet_type"]) in _enriched_ids
                render_bet_card(bet, i, len(_display_props), is_fav=_is_fav, session_id=_cur_sid, dimmed=not _in_ranked)

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
        _bet_types  = ["Player Prop", "Match Result", "Odds Boost", "Other"]
        _mf3, _mf4 = st.columns(2)
        _m_bet_cat = _mf3.selectbox("Bet type", _bet_types, key="m_bet_cat")
        _m_odds    = _mf4.number_input("Odds", min_value=1.01, max_value=50.0,
                                        value=2.00, step=0.05, format="%.2f",
                                        key="m_odds")
        _m_bet_det = st.text_input("Details", key="m_bet_det",
                                    placeholder="bijv. Anytime Goal Scorer, Over 2.5 goals, Colorado Avalanche to win")
        _m_bet     = f"{_m_bet_cat} — {_m_bet_det.strip()}" if _m_bet_det.strip() else _m_bet_cat
        _m_team    = st.text_input("Team (optioneel)", key="m_team",
                                    placeholder="bijv. Utah Hockey Club — de ploeg van de speler")
        _mf5, _mf6, _mf7, _mf8 = st.columns(4)
        _m_inzet   = _mf5.number_input("Inzet (€)", min_value=0.10, value=10.0,
                                         step=1.0, format="%.2f", key="m_inzet")
        _m_uitkomst = _mf6.selectbox("Uitkomst", ["open","gewonnen","verloren","void"],
                                      key="m_uitkomst")
        _m_ev      = _mf7.number_input("EV (optioneel)", min_value=-1.0, max_value=5.0,
                                        value=0.0, step=0.01, format="%.3f",
                                        key="m_ev")
        _m_game_date = _mf8.date_input("Wedstrijddatum", value=datetime.date.today(), key="m_game_date")
        _m_direct_inzet = st.checkbox(
            "📋 Direct inzetten als geplaatste weddenschap",
            key="m_direct_inzet", value=False,
            help="Registreert de bet ook direct in Geplaatste Bets (status: open). "
                 "Zelfde als op de 'Geplaatst' knop drukken na het toevoegen.",
        )
        if st.button("➕ Toevoegen aan favorieten", key="m_add_fav",
                     disabled=not _m_speler):
            _m_bet_obj = {
                # add_favoriet leest: player, bet_type, ev, odds, sport
                "player":   _m_speler,
                "bet_type": _m_bet,
                # upsert_resultaat leest: speler, bet, ev_score, datum, odds, sport
                "speler":   _m_speler,
                "bet":      _m_bet,
                "ev_score": _m_ev,
                "datum":    _m_game_date.isoformat(),
                "sport":    _m_sport,
                "odds":     _m_odds,
                "ev":       _m_ev,
                "team":     (_m_team or "").strip(),
                "bet365":   {},
                "source":   "handmatig",
            }
            _m_fid = db.make_fav_id(_m_speler, _m_bet)
            db.add_favoriet(_m_fid, _m_bet_obj, game_date=_m_game_date.isoformat())
            if _m_uitkomst != "open" or _m_direct_inzet:
                db.upsert_resultaat(_m_fid, _m_bet_obj, _m_uitkomst, _m_inzet)
            st.success(f"✅ '{_m_speler} — {_m_bet}' toegevoegd!")
            st.rerun()

    _sc_client = anthropic.Anthropic(api_key=api_key) if ANTHROPIC_AVAILABLE and api_key else None
    screenshot_import.render_screenshot_import("shortlist", client=_sc_client)

    st.markdown("---")
    _favs    = db.load_favorieten()
    _res_map = {r["id"]: r for r in db.load_resultaten()}

    if not _favs:
        st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen favorieten. Klik op ⭐ in een prop-kaart om te bewaren of voeg er handmatig een toe.</small>', unsafe_allow_html=True)
    else:
        # Samenvatting als er afgeronde resultaten zijn
        _done = [r for r in db.load_resultaten() if r.get("uitkomst") in ("gewonnen", "verloren", "void")]
        if _done:
            _fn_won   = sum(1 for r in _done if r.get("uitkomst") == "gewonnen")
            _fn_lost  = len(_done) - _fn_won
            _ft_inzet = sum(r.get("inzet", 0) for r in _done)
            _ft_wl    = sum(r.get("winst_verlies", 0) for r in _done)
            _froi     = (_ft_wl / _ft_inzet * 100) if _ft_inzet > 0 else 0.0
            _sc1, _sc2, _sc3, _sc4 = st.columns(4)
            _sc1.markdown(kpi_card("✅", "Gewonnen", str(_fn_won), f"{_fn_won}/{len(_done)} bets"), unsafe_allow_html=True)
            _sc2.markdown(kpi_card("❌", "Verloren",  str(_fn_lost), positive=(False if _fn_lost > 0 else None)), unsafe_allow_html=True)
            _sc3.markdown(kpi_card("💰", "P&L", (f"+€{abs(_ft_wl):.2f}" if _ft_wl >= 0 else f"-€{abs(_ft_wl):.2f}"), f"inzet €{_ft_inzet:.2f}", positive=(_ft_wl > 0) if _ft_wl != 0 else None, tooltip=(f"+€{abs(_ft_wl):.4f}" if _ft_wl >= 0 else f"-€{abs(_ft_wl):.4f}")), unsafe_allow_html=True)
            _sc4.markdown(kpi_card("📈", "ROI", f"{_froi:+.1f}%", f"over {len(_done)} bets", positive=(_froi > 0) if _froi != 0 else None, tooltip=f"{_froi:+.4f}%"), unsafe_allow_html=True)
            st.markdown("---")

        # ── Splits favorieten in actief vs. verlopen ──────────────────────────
        _today_iso = datetime.date.today().isoformat()

        def _fav_game_date(fav: dict) -> str:
            # Gebruik game_date als aanwezig, anders datum (wanneer toegevoegd)
            # [:10] → strip eventuele timestamp-suffix van Supabase ("2026-04-12 00:00:00" → "2026-04-12")
            gd = fav.get("game_date") or fav.get("datum") or _today_iso
            return str(gd)[:10]

        def _fav_is_expired(fav: dict) -> bool:
            """Verlopen = game_date < vandaag.
            Alle bets met een verstreken datum worden verborgen,
            ook gewonnen/verloren — die staan al in de Geplaatste Bets tab.
            """
            return _fav_game_date(fav) < _today_iso

        _favs_active  = [f for f in _favs if not _fav_is_expired(f)]
        _favs_expired = [f for f in _favs if _fav_is_expired(f)]

        if not _favs_active and not _favs_expired:
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen actieve bets.</small>', unsafe_allow_html=True)

        # Actieve bets
        for _idx, _fav in enumerate(_favs_active):
            _fid      = _fav.get("id", "")
            _res      = _res_map.get(_fid, {})
            _uitkomst = _res.get("uitkomst", "")
            _icon     = "✅" if _uitkomst == "gewonnen" else ("❌" if _uitkomst == "verloren" else ("⚪" if _uitkomst == "void" else "⏳"))
            _ev_disp  = f"{float(_fav.get('ev_score') or 0):+.3f}"

            with st.expander(
                f"{_icon} {_fav.get('speler','')} · {_fav.get('bet','')} "
                f"@ {_fav.get('odds','')}  |  EV {_ev_disp}  |  {_fav.get('datum','')}",
                expanded=(_uitkomst == ""),
            ):
                _ci, _cd = st.columns([4, 1])
                with _ci:
                    _cap = f"Sport: {_fav.get('sport','')}{_team_caption_suffix(_fav)} · Bet365: {_fav.get('bet365_status','')}"
                    if _res:
                        _cap += f"  ·  Inzet: €{_res.get('inzet',0):.2f}"
                        if _uitkomst == "void":
                            _cap += "  ·  P&L: ⚪ Void"
                        elif _uitkomst in ("gewonnen", "verloren"):
                            _r_wl = _res.get('winst_verlies', 0)
                            _cap += f"  ·  P&L: {'+€' if _r_wl >= 0 else '-€'}{abs(_r_wl):.2f}"
                        else:
                            _cap += "  ·  P&L: —"
                    st.caption(_cap)
                with _cd:
                    if st.button("🗑️", key=f"delfav_{_fid}_{_idx}", help="Verwijder favoriet"):
                        db.remove_favoriet(_fid)
                        db.remove_resultaat(_fid)
                        st.rerun()

                _col_inzet, _col_odds = st.columns(2)
                _inzet_default = float(_res.get("inzet", 10.0))
                _inzet = _col_inzet.number_input(
                    "💰 Inzet (€)", min_value=0.10, value=_inzet_default,
                    step=1.0, format="%.2f", key=f"inzet_{_fid}_{_idx}",
                )
                _odds_default = float(_res.get("odds") or _fav.get("odds") or 1.5)
                _odds = _col_odds.number_input(
                    "📊 Odds", min_value=1.01, value=_odds_default,
                    step=0.05, format="%.2f", key=f"odds_{_fid}_{_idx}",
                )
                # Gebruik de (eventueel aangepaste) odds bij opslaan.
                # Overschrijf _fav["datum"] met vandaag — dat veld is de datum
                # waarop de favoriet aan de Shortlist werd toegevoegd (mogelijk
                # weken/maanden geleden) en mag niet dienst doen als placement-
                # datum. upsert_resultaat() behoudt vervolgens automatisch de
                # placement-datum bij een status-update (open → gewonnen).
                _fav_met_odds = {**_fav, "odds": _odds,
                                 "datum": datetime.date.today().isoformat()}
                _cpl, _cw, _cl, _cv, _cp = st.columns(5)
                if _cpl.button("📋 Geplaatst", key=f"placed_{_fid}_{_idx}", use_container_width=True,
                               help="Markeer als geplaatst (uitkomst nog onbekend)"):
                    db.upsert_resultaat(_fid, _fav_met_odds, "open", _inzet)
                    st.rerun()
                if _cw.button("✅ Gewonnen", key=f"won_{_fid}_{_idx}",  use_container_width=True):
                    db.upsert_resultaat(_fid, _fav_met_odds, "gewonnen", _inzet)
                    st.rerun()
                if _cl.button("❌ Verloren", key=f"lost_{_fid}_{_idx}", use_container_width=True):
                    db.upsert_resultaat(_fid, _fav_met_odds, "verloren", _inzet)
                    st.rerun()
                if _cv.button("⚪ Void",     key=f"void_{_fid}_{_idx}",  use_container_width=True,
                               help="Inzet teruggestort (geen P&L)"):
                    db.upsert_resultaat(_fid, _fav_met_odds, "void", _inzet)
                    st.rerun()
                if _cp.button("⏳ Reset",    key=f"reset_{_fid}_{_idx}", use_container_width=True):
                    db.remove_resultaat(_fid)
                    st.rerun()

                # ── Doorsturen naar Parlay Builder ────────────────────────────
                if st.button("🎯 Voeg toe aan Parlay Builder",
                             key=f"fav_to_parlay_{_fid}_{_idx}",
                             use_container_width=True):
                    _already = any(
                        l.get("player") == _fav.get("speler") and
                        l.get("bet_type") == _fav.get("bet")
                        for l in st.session_state.parlay_legs
                    )
                    if _already:
                        st.warning("⚠️ Deze prop zit al in je Parlay Builder.")
                    else:
                        st.session_state.parlay_legs.append({
                            "player":   _fav.get("speler", ""),
                            "sport":    _fav.get("sport", ""),
                            "bet_type": _fav.get("bet", ""),
                            "odds":     float(_fav.get("odds") or 1.5),
                            "hit_rate": None,
                        })
                        st.success(f"✅ Toegevoegd aan Parlay Builder — ga naar 🎯 tab")

        # ── Verlopen bets (ingeklapt) ─────────────────────────────────────────
        if _favs_expired:
            with st.expander(f"🕐 Verlopen bets ({len(_favs_expired)}) — wedstrijd al gespeeld, niet ingezet", expanded=False):
                st.caption("Deze bets staan nog in je database. Je kunt ze handmatig verwijderen.")
                for _idx_e, _fav_e in enumerate(_favs_expired):
                    _fid_e  = _fav_e.get("id", "")
                    _gd_e   = _fav_game_date(_fav_e)
                    _ev_e   = f"{float(_fav_e.get('ev_score') or 0):+.3f}"
                    _ec1, _ec2 = st.columns([5, 1])
                    _ec1.markdown(
                        f"**{_fav_e.get('speler','')}** · {_fav_e.get('bet','')} "
                        f"@ {_fav_e.get('odds','')}  ·  EV {_ev_e}  ·  📅 {_gd_e}"
                    )
                    if _ec2.button("🗑️", key=f"del_exp_{_fid_e}_{_idx_e}", help="Verwijder"):
                        db.remove_favoriet(_fid_e)
                        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — BANKROLL
# ══════════════════════════════════════════════════════════════════════════════

with tab_bankroll:
    import pandas as pd
    from collections import defaultdict as _defaultdict

    _bk_today = datetime.date.today()

    # ── Zet standaard geselecteerde dag ──────────────────────────────────────
    if st.session_state.bk_selected_day is None:
        st.session_state.bk_selected_day = _bk_today.isoformat()

    # ── Laad alle data één keer ───────────────────────────────────────────────
    _bk_all_raw      = db.load_resultaten()
    _all_parlays_bk  = db.load_parlays()
    _parlays_bk_map  = {p["id"]: p for p in _all_parlays_bk}

    # Gesettlede parlays die (nog) niet in resultaten staan toevoegen
    _bk_settled_ids = {str(r.get("id","")) for r in _bk_all_raw
                       if r.get("uitkomst") in ("gewonnen","verloren","void")}
    _bk_all_settled = [r for r in _bk_all_raw
                       if r.get("uitkomst") in ("gewonnen","verloren","void")]
    for _bkp in _all_parlays_bk:
        if (_bkp.get("uitkomst") or "open") not in ("gewonnen","verloren","void"):
            continue
        _bkp_id = f"parlay_{_bkp['id']}"
        if _bkp_id in _bk_settled_ids:
            continue
        _bkp_legs = _bkp.get("props_json") or []
        _bk_all_settled.append({
            "id":            _bkp_id,
            "datum":         (_bkp.get("datum") or "")[:10],
            "speler":        f"🎰 Parlay ({len(_bkp_legs)} legs)",
            "bet":           ", ".join(str(l.get("player","")) for l in _bkp_legs[:3]) or "Parlay",
            "sport":         "Parlay",
            "odds":          float(_bkp.get("gecombineerde_odds") or 1.0),
            "inzet":         float(_bkp.get("inzet") or 0),
            "uitkomst":      _bkp["uitkomst"],
            "winst_verlies": float(_bkp.get("winst_verlies") or 0),
            "ev_score":      float(_bkp.get("ev_score") or 0),
            "is_parlay":     True,
        })

    # Bets gegroepeerd per datum
    _bets_by_date: dict = _defaultdict(list)
    for _bkr in _bk_all_settled:
        _bkd = (_bkr.get("datum") or "")[:10]
        if _bkd:
            _bets_by_date[_bkd].append(_bkr)

    # Globale totalen
    _bk_total_wl      = sum(r.get("winst_verlies",0) for r in _bk_all_settled)
    _start_bk_saved   = float(db.get_setting("start_bankroll") or 0.0)
    _mutations_total  = db.get_bankroll_mutations_total()
    # Deducteer openstaande inzetten (stake al gecommitteerd, nog niet gesettled)
    _bk_all_raw_ids   = {str(r.get("id","")) for r in _bk_all_raw}
    _bk_open_inzet    = sum(float(r.get("inzet",0)) for r in _bk_all_raw if r.get("uitkomst") == "open")
    _bk_open_inzet   += sum(
        float(p.get("inzet") or 0)
        for p in _all_parlays_bk
        if (p.get("uitkomst") or "open") == "open" and f"parlay_{p['id']}" not in _bk_all_raw_ids
    )
    _bk_balance       = _start_bk_saved + _mutations_total + _bk_total_wl - _bk_open_inzet if _start_bk_saved > 0 else None

    # ── HEADER ───────────────────────────────────────────────────────────────
    _7d_cutoff = (_bk_today - datetime.timedelta(days=6)).isoformat()
    _7d_wl     = sum(r.get("winst_verlies",0) for r in _bk_all_settled
                     if (r.get("datum") or "")[:10] >= _7d_cutoff)
    _7d_color  = "#4ade80" if _7d_wl >= 0 else "#f87171"
    _7d_pill   = f"{'+ ' if _7d_wl >= 0 else ''}€{_7d_wl:+.2f}  laatste 7 dagen"

    if _bk_balance is not None:
        _bal_display = f"€{_bk_balance:,.2f}"
    elif _bk_all_settled:
        _bal_display = f"€{_bk_total_wl:+.2f} P&L"
    else:
        _bal_display = "—"

    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#12103a 0%,#1e1860 100%);
      border:1px solid #3a2a70;border-radius:14px;padding:1.3rem 1.6rem;
      margin-bottom:1.1rem;box-shadow:0 4px 24px rgba(124,58,237,0.20);">
      <div style="font-size:0.75rem;color:#8888b8;text-transform:uppercase;
        letter-spacing:1.2px;margin-bottom:4px;">Total Balance</div>
      <div style="font-size:2.4rem;font-weight:800;color:#c4b5fd;letter-spacing:-1px;
        line-height:1.1;">{_bal_display}</div>
      <div style="margin-top:10px;">
        <span style="background:{'rgba(74,222,128,0.13)' if _7d_wl >= 0 else 'rgba(248,113,113,0.13)'};
          border:1px solid {_7d_color};color:{_7d_color};
          padding:4px 14px;border-radius:20px;font-size:0.82rem;font-weight:600;
          letter-spacing:0.2px;">{_7d_pill}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── PERIOD SWITCHER ───────────────────────────────────────────────────────
    _view_options = ["7 Dagen", "Maand", "All Time"]
    _cur_view_idx = _view_options.index(st.session_state.bk_view) \
                    if st.session_state.bk_view in _view_options else 0
    _selected_view = st.radio(
        "Periode",
        _view_options,
        index=_cur_view_idx,
        horizontal=True,
        label_visibility="collapsed",
        key="bk_view_radio",
    )
    st.session_state.bk_view = _selected_view
    st.markdown("---")

    # ════════════════════════════════════════════════════════════
    # VIEW: 7 DAGEN
    # ════════════════════════════════════════════════════════════
    if _selected_view == "7 Dagen":
        # Bouw de 7 dag-objecten
        _week_days = []
        for _di in range(6, -1, -1):
            _wd = _bk_today - datetime.timedelta(days=_di)
            _wd_str  = _wd.isoformat()
            _wd_bets = _bets_by_date.get(_wd_str, [])
            _wd_pnl  = sum(r.get("winst_verlies",0) for r in _wd_bets)
            _week_days.append({
                "date":    _wd_str,
                "dayname": _wd.strftime("%a"),
                "day_num": _wd.day,
                "pnl":     _wd_pnl,
                "bets":    _wd_bets,
            })

        # Zorg dat geselecteerde dag binnen het window valt
        _sel_day = st.session_state.bk_selected_day
        if _sel_day not in [d["date"] for d in _week_days]:
            _sel_day = _bk_today.isoformat()
            st.session_state.bk_selected_day = _sel_day

        # Per-knop CSS injecteren via markeerspan + sibling-selector
        _day_css_parts = ["<style>"]
        for _di2, _wd2 in enumerate(_week_days):
            _wpnl = _wd2["pnl"]
            _wsel = (_wd2["date"] == _sel_day)
            if _wpnl > 0:
                _wbg = "rgba(74,222,128,0.13)"; _wbc = "#4ade80"
            elif _wpnl < 0 and _wd2["bets"]:
                _wbg = "rgba(248,113,113,0.13)"; _wbc = "#f87171"
            else:
                _wbg = "#11112b"; _wbc = "#2e2e56"
            _wborder = "2px solid #7c3aed" if _wsel else f"1px solid {_wbc}"
            _day_css_parts.append(f"""
            [data-testid="stMarkdownContainer"]:has(span.bkdm{_di2})
            ~ [data-testid="stButton"] button {{
                background: {_wbg} !important;
                border: {_wborder} !important;
                min-height: 82px !important;
                white-space: pre-wrap !important;
                line-height: 1.6 !important;
                font-size: 0.78rem !important;
                padding: 6px 2px !important;
                font-weight: 600 !important;
            }}""")
        _day_css_parts.append("</style>")
        st.markdown("".join(_day_css_parts), unsafe_allow_html=True)

        # 7 dag-knoppen in columns
        _dcols = st.columns(7)
        for _di3, (_dc, _wd3) in enumerate(zip(_dcols, _week_days)):
            with _dc:
                _dp = _wd3["pnl"]
                _dp_str = (f"+€{int(abs(_dp))}" if _dp > 0
                           else f"-€{int(abs(_dp))}" if _dp < 0
                           else "—")
                st.markdown(f'<span class="bkdm{_di3}"></span>', unsafe_allow_html=True)
                if st.button(
                    f"{_wd3['dayname']}\n{_wd3['day_num']}\n{_dp_str}",
                    key=f"bk_day_{_di3}",
                    use_container_width=True,
                ):
                    st.session_state.bk_selected_day = _wd3["date"]
                    st.rerun()

        # Stat-kaartjes
        _7d_bets_all = [b for _wd4 in _week_days for b in _wd4["bets"]]
        _7d_won_n    = sum(1 for b in _7d_bets_all if b.get("uitkomst") == "gewonnen")
        _7d_n        = len(_7d_bets_all)
        _7d_wr       = (_7d_won_n / _7d_n * 100) if _7d_n > 0 else 0.0
        _7d_pnl_sum  = sum(b.get("winst_verlies",0) for b in _7d_bets_all)

        st.markdown("")
        _sc1, _sc2, _sc3 = st.columns(3)
        _sc1.markdown(kpi_card("💰", "Week P&L",
            (f"+€{abs(_7d_pnl_sum):.2f}" if _7d_pnl_sum >= 0 else f"-€{abs(_7d_pnl_sum):.2f}"),
            positive=(_7d_pnl_sum > 0) if _7d_pnl_sum != 0 else None,
            tooltip=(f"+€{abs(_7d_pnl_sum):.4f}" if _7d_pnl_sum >= 0 else f"-€{abs(_7d_pnl_sum):.4f}")), unsafe_allow_html=True)
        _sc2.markdown(kpi_card("🎯", "Bets",
            str(_7d_n), "in periode"), unsafe_allow_html=True)
        _sc3.markdown(kpi_card("📊", "Win rate",
            f"{_7d_wr:.0f}%",
            f"{_7d_won_n} gewonnen",
            positive=(_7d_wr >= 50) if _7d_n > 0 else None), unsafe_allow_html=True)

        # Dag-detail
        st.markdown("---")
        _sel_info = next((d for d in _week_days if d["date"] == _sel_day), None)
        if _sel_info:
            try:
                _sel_dt  = datetime.date.fromisoformat(_sel_day)
                _day_lbl = _sel_dt.strftime("%A %-d %b")
            except Exception:
                _day_lbl = _sel_day
            _day_tot   = _sel_info["pnl"]
            _day_color = "#4ade80" if _day_tot > 0 else "#f87171" if _day_tot < 0 else "#a8aace"

            _ddh1, _ddh2 = st.columns([3, 1])
            _ddh1.markdown(f"**{_day_lbl}**")
            _ddh2.markdown(
                f"<div style='text-align:right;color:{_day_color};"
                f"font-weight:700;font-size:1rem;'>{'+€' if _day_tot >= 0 else '-€'}{abs(_day_tot):.2f}</div>",
                unsafe_allow_html=True)

            if _sel_info["bets"]:
                for _db2 in sorted(_sel_info["bets"],
                                   key=lambda x: x.get("datum",""), reverse=True):
                    _dsp  = _db2.get("sport","?")
                    _dico = SPORT_ICONS.get(_dsp.upper(), "🎮") if _dsp != "Parlay" else "🎰"
                    _dpnl = _db2.get("winst_verlies", 0)
                    _dcol = "#4ade80" if _dpnl > 0 else "#f87171"
                    _dbet = _db2.get("bet") or _db2.get("bet_type") or ""
                    _dspl = _db2.get("speler","")
                    _dods = _db2.get("odds","—")
                    st.markdown(f"""
                    <div style="display:flex;justify-content:space-between;align-items:center;
                      padding:9px 14px;margin:4px 0;background:#11112b;border-radius:8px;
                      border:1px solid #2e2e56;">
                      <div>
                        <span style="margin-right:6px;font-size:1rem;">{_dico}</span>
                        <span style="color:#e8eaf6;font-weight:600;">{_dspl}</span>
                        <span style="color:#a8aace;font-size:0.82rem;margin-left:6px;">{_dbet}</span>
                      </div>
                      <div style="text-align:right;flex-shrink:0;margin-left:8px;">
                        <span style="color:{_dcol};font-weight:700;">{'+€' if _dpnl >= 0 else '-€'}{abs(_dpnl):.2f}</span>
                        <span style="color:#6868a0;font-size:0.75rem;margin-left:6px;">@ {_dods}</span>
                      </div>
                    </div>""", unsafe_allow_html=True)
            else:
                st.markdown("""
                <div style="text-align:center;color:#6868a0;padding:28px 16px;
                  background:#11112b;border-radius:8px;border:1px dashed #2e2e56;
                  margin-top:8px;">
                  Geen bets op deze dag
                </div>""", unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════════
    # VIEW: MAAND
    # ════════════════════════════════════════════════════════════
    elif _selected_view == "Maand":
        _m_start  = _bk_today.replace(day=1)
        _m_next   = (_m_start + datetime.timedelta(days=32)).replace(day=1)
        _m_days   = [_m_start + datetime.timedelta(days=_mi)
                     for _mi in range((_m_next - _m_start).days)]

        _m_bets_all = [b for _md in _m_days
                       for b in _bets_by_date.get(_md.isoformat(), [])]
        _m_total_pnl = sum(b.get("winst_verlies",0) for b in _m_bets_all)
        _m_won_n     = sum(1 for b in _m_bets_all if b.get("uitkomst") == "gewonnen")
        _m_n         = len(_m_bets_all)
        _m_wr        = (_m_won_n / _m_n * 100) if _m_n > 0 else 0.0

        st.markdown(f"#### {_bk_today.strftime('%B %Y')}")
        _mc1, _mc2, _mc3 = st.columns(3)
        _mc1.markdown(kpi_card("💰", "Maand P&L",
            f"€{_m_total_pnl:+.2f}",
            positive=(_m_total_pnl > 0) if _m_total_pnl != 0 else None,
            tooltip=f"€{_m_total_pnl:+.4f}"), unsafe_allow_html=True)
        _mc2.markdown(kpi_card("🎯", "Bets", str(_m_n), "in maand"), unsafe_allow_html=True)
        _mc3.markdown(kpi_card("📊", "Win rate",
            f"{_m_wr:.0f}%",
            f"{_m_won_n} gewonnen",
            positive=(_m_wr >= 50) if _m_n > 0 else None), unsafe_allow_html=True)
        st.markdown("---")

        # Groepeer in kalender-weken
        _wk_groups: dict = {}
        for _wmd in _m_days:
            _wk_nr = _wmd.isocalendar()[1]
            if _wk_nr not in _wk_groups:
                _wk_groups[_wk_nr] = []
            _wk_groups[_wk_nr].append(_wmd)

        _wk_counter = 1
        for _wk_nr2, _wk_days2 in sorted(_wk_groups.items()):
            _wk_s  = _wk_days2[0].strftime("%-d %b")
            _wk_e  = _wk_days2[-1].strftime("%-d %b")
            _wk_bs = [b for _wd5 in _wk_days2
                      for b in _bets_by_date.get(_wd5.isoformat(), [])]
            _wk_pnl  = sum(b.get("winst_verlies",0) for b in _wk_bs)
            _wk_won  = sum(1 for b in _wk_bs if b.get("uitkomst") == "gewonnen")
            _wk_n    = len(_wk_bs)
            _wk_col  = "#4ade80" if _wk_pnl > 0 else "#f87171" if _wk_pnl < 0 else "#a8aace"
            _wk_pstr = f"€{_wk_pnl:+.2f}"
            # Expand de huidige week automatisch
            _wk_is_cur = any(_wd5 >= _bk_today for _wd5 in _wk_days2)

            with st.expander(
                f"Week {_wk_counter}  ·  {_wk_s}–{_wk_e}  ·  {_wk_n} bets  ·  P&L: {_wk_pstr}",
                expanded=_wk_is_cur,
            ):
                if not _wk_bs:
                    st.caption("Geen bets in deze week.")
                else:
                    for _wb in sorted(_wk_bs, key=lambda x: x.get("datum",""), reverse=True):
                        _ws   = _wb.get("sport","?")
                        _wico = SPORT_ICONS.get(_ws.upper(), "🎮") if _ws != "Parlay" else "🎰"
                        _wpnl2 = _wb.get("winst_verlies",0)
                        _wcol2 = "#4ade80" if _wpnl2 > 0 else "#f87171"
                        _wdate = (_wb.get("datum",""))[:10]
                        try:
                            _wdate_fmt = datetime.date.fromisoformat(_wdate).strftime("%-d %b")
                        except Exception:
                            _wdate_fmt = _wdate
                        st.markdown(f"""
                        <div style="display:flex;justify-content:space-between;
                          align-items:center;padding:7px 12px;margin:3px 0;
                          background:#13132e;border-radius:7px;border:1px solid #2e2e56;">
                          <div>
                            <span style="color:#6868a0;font-size:0.72rem;margin-right:8px;">{_wdate_fmt}</span>
                            <span style="margin-right:4px;">{_wico}</span>
                            <span style="color:#e8eaf6;font-weight:500;">{_wb.get('speler','')}</span>
                            <span style="color:#a8aace;font-size:0.78rem;margin-left:6px;">{_wb.get('bet','')}</span>
                          </div>
                          <span style="color:{_wcol2};font-weight:700;flex-shrink:0;margin-left:8px;">
                            €{_wpnl2:+.2f}
                          </span>
                        </div>""", unsafe_allow_html=True)
            _wk_counter += 1

    # ════════════════════════════════════════════════════════════
    # VIEW: ALL TIME  (bestaande implementatie ongewijzigd)
    # ════════════════════════════════════════════════════════════
    else:
        st.markdown("### 📊 Bankroll Tracker")

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
                ["open (nog niet gespeeld)", "gewonnen", "verloren", "void"],
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
        # Open parlays toevoegen (staan niet in resultaten tabel)
        _bk_open_prl_existing = {r.get("id", "") for r in _openstaand if str(r.get("id", "")).startswith("parlay_")}
        for _bk_op_prl in db.load_parlays():
            if (_bk_op_prl.get("uitkomst") or "open") != "open":
                continue
            _bk_op_prl_res_id = f"parlay_{_bk_op_prl['id']}"
            if _bk_op_prl_res_id in _bk_open_prl_existing:
                continue
            _bk_op_prl_legs = _bk_op_prl.get("props_json") or []
            _openstaand.append({
                "id":            _bk_op_prl_res_id,
                "datum":         (_bk_op_prl.get("datum") or datetime.date.today().isoformat())[:10],
                "speler":        f"🎰 Parlay ({len(_bk_op_prl_legs)} legs)",
                "bet":           ", ".join(str(l.get("player", "")) for l in _bk_op_prl_legs[:3]) or "Parlay",
                "sport":         "Parlay",
                "odds":          float(_bk_op_prl.get("gecombineerde_odds") or 1.0),
                "inzet":         float(_bk_op_prl.get("inzet") or 0),
                "uitkomst":      "open",
                "winst_verlies": 0.0,
                "ev_score":      float(_bk_op_prl.get("ev_score") or 0),
                "is_parlay":     True,
            })
        if _openstaand:
            st.markdown("#### ⏳ Openstaande weddenschappen")
            st.caption(f"{len(_openstaand)} bet(s) nog niet afgerond — klik op gewonnen of verloren om te registreren.")
            for _op in sorted(_openstaand, key=lambda r: r.get("datum",""), reverse=True):
                _op_id = _op.get("id","")
                _oc1, _oc2, _oc3, _oc4, _oc5, _oc6 = st.columns([3, 0.9, 0.8, 0.8, 0.8, 0.7])
                _oc1.write(f"**{_op.get('speler','')}** — {_op.get('bet','')}  @ {_op.get('odds','—')} | €{_op.get('inzet',0):.2f}")
                _oc2.caption(_op.get("datum","")[:10])
                if _oc3.button("✅ Win", key=f"opwon_{_op_id}"):
                    _op_inzet_val = float(_op.get("inzet", 10))
                    _op_upd = dict(_op)
                    _op_upd["uitkomst"] = "gewonnen"
                    db.upsert_resultaat(_op_id, _op_upd, "gewonnen", _op_inzet_val)
                    if str(_op_id).startswith("parlay_"):
                        _op_odds_val = float(_op.get("odds", 1.0))
                        db.update_parlay(str(_op_id)[len("parlay_"):], {
                            "uitkomst": "gewonnen",
                            "winst_verlies": round(_op_inzet_val * (_op_odds_val - 1), 2),
                        })
                    st.rerun()
                if _oc4.button("❌ Loss", key=f"oplost_{_op_id}"):
                    _op_inzet_val = float(_op.get("inzet", 10))
                    _op_upd = dict(_op)
                    _op_upd["uitkomst"] = "verloren"
                    db.upsert_resultaat(_op_id, _op_upd, "verloren", _op_inzet_val)
                    if str(_op_id).startswith("parlay_"):
                        db.update_parlay(str(_op_id)[len("parlay_"):], {
                            "uitkomst": "verloren",
                            "winst_verlies": round(-float(_op.get("inzet", 10)), 2),
                        })
                    st.rerun()
                if _oc5.button("⚪ Void", key=f"opvoid_{_op_id}"):
                    _op_inzet_val = float(_op.get("inzet", 10))
                    _op_upd = dict(_op)
                    _op_upd["uitkomst"] = "void"
                    db.upsert_resultaat(_op_id, _op_upd, "void", _op_inzet_val)
                    if str(_op_id).startswith("parlay_"):
                        db.update_parlay(str(_op_id)[len("parlay_"):], {
                            "uitkomst": "void",
                            "winst_verlies": 0.0,
                        })
                    st.rerun()
                if _oc6.button("🗑️", key=f"opdel_{_op_id}", help="Verwijder"):
                    if str(_op_id).startswith("parlay_"):
                        # Open parlay staat niet in resultaten → verwijder uit parlays tabel
                        db.delete_parlay(str(_op_id)[len("parlay_"):])
                    else:
                        db.remove_resultaat(_op_id)
                    st.rerun()
            st.markdown("---")
    
        # ── Opname / Storting registreren ────────────────────────────────────────
        with st.expander("💸 Opname of storting registreren", expanded=False):
            st.caption("Registreer geldopnames of stortingen zodat je saldo altijd klopt.")
            _mut_c1, _mut_c2, _mut_c3 = st.columns([1, 2, 1])
            _mut_type    = _mut_c1.selectbox("Type", ["Opname 💸", "Storting 💰"], key="mut_type")
            _mut_bedrag  = _mut_c2.number_input("Bedrag (€)", min_value=0.01, max_value=100000.0,
                                                value=20.0, step=1.0, format="%.2f", key="mut_bedrag")
            _mut_omschr  = _mut_c3.text_input("Omschrijving (optioneel)", key="mut_omschr",
                                               placeholder="bijv. Claude kosten")
            if st.button("✅ Registreren", key="btn_mut_save"):
                _bedrag_signed = -abs(_mut_bedrag) if "Opname" in _mut_type else abs(_mut_bedrag)
                _omschr_final  = _mut_omschr.strip() or ("Storting" if _bedrag_signed > 0 else "Opname")
                db.save_bankroll_mutation(_bedrag_signed, _omschr_final)
                st.success(f"{'Opname' if _bedrag_signed < 0 else 'Storting'} van €{abs(_bedrag_signed):.2f} geregistreerd!")
                st.rerun()

            _mutations = db.load_bankroll_mutations()
            if _mutations:
                st.markdown("**Mutatiehistorie:**")
                for _m in sorted(_mutations, key=lambda x: x.get("datum",""), reverse=True):
                    _m_bedrag = float(_m.get("bedrag", 0))
                    _m_kleur  = "#4ade80" if _m_bedrag >= 0 else "#f87171"
                    _m_icon   = "💰" if _m_bedrag >= 0 else "💸"
                    _mc1, _mc2, _mc3, _mc4 = st.columns([1.5, 1.2, 3, 0.8])
                    _mc1.caption(_m.get("datum","—"))
                    _mc2.markdown(f"<span style='color:{_m_kleur};font-weight:700'>{'+' if _m_bedrag >= 0 else ''}€{_m_bedrag:.2f}</span>", unsafe_allow_html=True)
                    _mc3.caption(f"{_m_icon} {_m.get('omschrijving','')}")
                    if _mc4.button("🗑️", key=f"del_mut_{_m['id']}"):
                        db.delete_bankroll_mutation(_m["id"])
                        st.rerun()

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
            # Detecteer parlay via id-prefix (robuust voor bestaande records zonder is_parlay=True)
            _r_is_parlay = str(r.get("id","")).startswith("parlay_") or bool(r.get("is_parlay"))
            if _bk_kind == "Singles" and _r_is_parlay: return False
            if _bk_kind == "Parlays" and not _r_is_parlay: return False
            return True
    
        _alle_res = [r for r in db.load_resultaten() if _bk_filter(r)]
        _gedaan   = [r for r in _alle_res if r.get("uitkomst") in ("gewonnen", "verloren", "void")]
    
        # ── Parlays laden (één keer, hergebruikt voor per-sport en Parlay ROI) ────
        _all_parlays_bk = db.load_parlays()
        _parlays_bk_map = {p["id"]: p for p in _all_parlays_bk}
    
        # Gesettlede parlays die ontbreken in _gedaan toevoegen (zelfde aanpak als Dashboard).
        # _gedaan komt uit resultaten; als upsert_resultaat ooit niet is uitgevoerd staan
        # de parlays daar niet in en worden ze hier handmatig ingevuld.
        _bk_bestaande_prl_ids = {
            str(r.get("id", "")) for r in _gedaan
            if str(r.get("id", "")).startswith("parlay_")
        }
        for _p in _all_parlays_bk:
            if (_p.get("uitkomst") or "open") not in ("gewonnen", "verloren", "void"):
                continue
            _p_res_id = f"parlay_{_p['id']}"
            if _p_res_id in _bk_bestaande_prl_ids:
                continue  # al aanwezig in resultaten, niet dubbel tellen
            _p_legs  = _p.get("props_json") or []
            _p_entry = {
                "id":            _p_res_id,
                "datum":         (_p.get("datum") or "")[:10],
                "speler":        f"🎰 Parlay ({len(_p_legs)} legs)",
                "bet":           ", ".join(str(l.get("player", "")) for l in _p_legs[:3]) or "Parlay",
                "sport":         "Parlay",
                "odds":          float(_p.get("gecombineerde_odds") or 1.0),
                "inzet":         float(_p.get("inzet") or 0),
                "uitkomst":      _p["uitkomst"],
                "winst_verlies": float(_p.get("winst_verlies") or 0),
                "ev_score":      float(_p.get("ev_score") or 0),
                "is_parlay":     True,
            }
            if _bk_filter(_p_entry):  # respecteer actieve sport/periode/type filters
                _gedaan.append(_p_entry)
    
        # _gedaan_sport: parlay-entries uitgebreid naar losse sport-legs voor per-sport stats
        # Elke leg krijgt inzet/P&L proportioneel toegewezen (inzet / aantal legs).
        def _expand_parlay_legs(gedaan, parlays_map):
            result = []
            for r in gedaan:
                r_id = str(r.get("id", ""))
                if r_id.startswith("parlay_"):
                    prl = parlays_map.get(r_id[len("parlay_"):], {})
                    legs = prl.get("props_json") or []
                    n = max(len(legs), 1)
                    for leg in legs:
                        result.append({
                            "sport":         leg.get("sport", "") or "Parlay",
                            "uitkomst":      r["uitkomst"],
                            "inzet":         round(r.get("inzet", 0) / n, 2),
                            "winst_verlies": round(r.get("winst_verlies", 0) / n, 2),
                            "ev_score":      0.0,
                            "odds":          float(leg.get("odds", 1.0)),
                            "bet":           leg.get("bet_type", ""),
                            "speler":        leg.get("player", ""),
                            "_parlay_id":    r_id,
                        })
                else:
                    result.append(r)
            return result
        _gedaan_sport = _expand_parlay_legs(_gedaan, _parlays_bk_map)
    
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
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen afgeronde weddenschappen. Voeg ze toe via ➕ hierboven of markeer props in ⭐ Favorieten.</small>', unsafe_allow_html=True)
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
                _bk_flt_open_inzet = sum(float(r.get("inzet",0)) for r in _alle_res if r.get("uitkomst") == "open")
                _huidig_saldo = _start_bk_saved + _mutations_total + _bt_wl - _bk_flt_open_inzet
                _groei_pct    = (_bt_wl / _start_bk_saved * 100) if _start_bk_saved > 0 else 0.0
                _bmc1, _bmc2, _bmc3, _bmc4 = st.columns(4)
                _bmc1.markdown(kpi_card("🏦", "Startbankroll", f"€{_start_bk_saved:.2f}"), unsafe_allow_html=True)
                _bmc2.markdown(kpi_card("💰", "Huidig saldo",  f"€{_huidig_saldo:.2f}", f"P&L {_bt_wl:+.2f}", positive=(_bt_wl > 0) if _bt_wl != 0 else None), unsafe_allow_html=True)
                _bmc3.markdown(kpi_card("📈", "Groei",         f"{_groei_pct:+.1f}%", positive=(_groei_pct > 0) if _groei_pct != 0 else None), unsafe_allow_html=True)
                _bmc4.markdown(kpi_card("🎯", "Win %",         f"{_bwin_pct:.1f}%",   positive=(_bwin_pct >= 55) if _gedaan else None), unsafe_allow_html=True)
                st.markdown("")
    
            _bc1, _bc2, _bc3, _bc4 = st.columns(4)
            _bc1.markdown(kpi_card("💰", "Totaal P&L",   f"€{_bt_wl:+.2f}", positive=(_bt_wl > 0) if _bt_wl != 0 else None, tooltip=f"€{_bt_wl:+.4f}"), unsafe_allow_html=True)
            _bc2.markdown(kpi_card("📈", "ROI",           f"{_broi:+.1f}%",  positive=(_broi > 0) if _broi != 0 else None, tooltip=f"{_broi:+.4f}%"), unsafe_allow_html=True)
            _bc3.markdown(kpi_card("📊", "W / L",         f"{_bn_won} / {len(_gedaan) - _bn_won}", f"{len(_gedaan)} bets gespeeld"), unsafe_allow_html=True)
            _bc4.markdown(kpi_card("🎰", "Bets gespeeld", str(len(_gedaan))), unsafe_allow_html=True)
    
            # Streak + drawdown
            _cur_streak, _best_streak, _streak_type = _calc_streak(_gedaan)
            _max_dd = _calc_drawdown(_gedaan)
            _streak_icon = "🔥" if _streak_type == "gewonnen" else "❄️"
            _bx1, _bx2, _bx3 = st.columns(3)
            _bx1.markdown(kpi_card(_streak_icon, "Huidige streak", f"{_cur_streak}× {_streak_type}", positive=(_streak_type == "gewonnen")), unsafe_allow_html=True)
            _bx2.markdown(kpi_card("🏆", "Langste streak", str(_best_streak)), unsafe_allow_html=True)
            _bx3.markdown(kpi_card("📉", "Max drawdown", f"€{_max_dd:.2f}", "piek-naar-dal verlies", positive=(False if _max_dd > 0 else None), tooltip=f"€{_max_dd:.4f}"), unsafe_allow_html=True)
    
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
    
            # ── Per sport ─────────────────────────────────────────────────────────
            # Gebruikt _gedaan_sport: parlay-entries zijn uitgebreid naar losse sport-legs.
            st.markdown("---")
            st.markdown("#### 🏟️ Per sport")
            for _bsport in sorted({r.get("sport","?") for r in _gedaan_sport}):
                _sr   = [r for r in _gedaan_sport if r.get("sport","") == _bsport]
                _sw   = sum(1 for r in _sr if r.get("uitkomst") == "gewonnen")
                _si   = sum(r.get("inzet", 0) for r in _sr)
                _swl  = sum(r.get("winst_verlies", 0) for r in _sr)
                _sroi = (_swl / _si * 100) if _si > 0 else 0.0
                # Onderscheid single bets vs parlay legs in dit sport-bucket
                _sr_singles = [r for r in _sr if not r.get("_parlay_id")]
                _sr_legs    = [r for r in _sr if r.get("_parlay_id")]
                _icon = SPORT_ICONS.get(_bsport.upper(), "⚽") if _bsport != "Parlay" else "🎰"
                with st.expander(f"{_icon} {_bsport}  —  P&L: €{_swl:+.2f}  |  ROI: {_sroi:+.1f}%", expanded=True):
                    _sc1, _sc2, _sc3 = st.columns(3)
                    _sc1.metric("W / L",        f"{_sw} / {len(_sr) - _sw}")
                    _sc2.metric("Totale inzet", f"€{_si:.2f}")
                    _sc3.metric("P&L",          f"€{_swl:+.2f}")
                    if _sr_legs:
                        _pl_w = sum(1 for r in _sr_legs if r.get("uitkomst") == "gewonnen")
                        _pl_l = len(_sr_legs) - _pl_w
                        st.caption(f"🎰 Waarvan parlay legs: {_pl_w}W / {_pl_l}L  ·  {len(_sr_singles)} singles")
                    _btype_wl = {}
                    for _r in _sr_singles:  # alleen singles voor meest winstgevend bet type
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
    
            # ── Model Prestaties — Feedback Loop ─────────────────────────────────
            st.markdown("---")
            st.markdown("#### 🧠 Model Prestaties — Feedback Loop")
            st.caption("Hoe goed presteren de bets die de app aanbeveelt? Vergelijk voorspeld EV met werkelijk resultaat.")

            # Gebruik alleen single bets voor model-kalibratie
            # (parlays hebben een gecombineerde EV, niet per-bet vergelijkbaar)
            _model_bets = [
                r for r in _gedaan
                if not str(r.get("id", "")).startswith("parlay_") and not r.get("is_parlay")
            ]

            if len(_model_bets) < 5:
                st.caption("📊 Minimaal 5 afgeronde single bets nodig voor modelanalyse.")
            else:
                # ── Globale kalibratie KPIs (bovenaan, direct zichtbaar) ──────────
                _m_ev_vals   = [float(r.get("ev_score") or 0) for r in _model_bets]
                _m_inzet_sum = sum(float(r.get("inzet") or 0) for r in _model_bets)
                _m_wl_sum    = sum(float(r.get("winst_verlies") or 0) for r in _model_bets)
                _m_gem_ev    = sum(_m_ev_vals) / len(_m_ev_vals) if _m_ev_vals else 0.0
                _m_roi       = (_m_wl_sum / _m_inzet_sum * 100) if _m_inzet_sum > 0 else 0.0
                _m_bias      = _m_roi - _m_gem_ev * 100  # positief = model te conservatief

                if abs(_m_bias) < 5:
                    _bias_lbl = "✅ Goed gekalibreerd"
                    _bias_pos = True
                elif _m_bias > 5:
                    _bias_lbl = "🚀 Model te conservatief"
                    _bias_pos = True
                else:
                    _bias_lbl = "⚠️ Model te optimistisch"
                    _bias_pos = False

                _ks1, _ks2, _ks3 = st.columns(3)
                _ks1.markdown(kpi_card("🎯", "Gem. voorspeld EV",
                                       f"{_m_gem_ev:+.3f}", f"over {len(_model_bets)} bets"),
                              unsafe_allow_html=True)
                _ks2.markdown(kpi_card("📈", "Werkelijk ROI",
                                       f"{_m_roi:+.1f}%", f"vs {_m_gem_ev*100:+.1f}% voorspeld",
                                       positive=(_m_roi > 0) if _m_roi != 0 else None),
                              unsafe_allow_html=True)
                _ks3.markdown(kpi_card("🔬", "Model bias",
                                       f"{_m_bias:+.1f}%", _bias_lbl, positive=_bias_pos),
                              unsafe_allow_html=True)
                st.caption("💡 **Model bias**: werkelijke ROI minus gemiddeld voorspeld EV%. "
                           "Positief = model te conservatief (meer winst dan verwacht). "
                           "Negatief = model schat kansen te optimistisch in.")

                # ── Rating Tier Prestaties ────────────────────────────────────────
                st.markdown("##### 🎯 Prestaties per model-rating")

                def _derive_rating_tier(r: dict) -> str:
                    """Gebruik opgeslagen rating als beschikbaar, anders schat op EV."""
                    stored = (r.get("rating") or "").strip()
                    if "Sterk"  in stored: return "✅ Sterk"
                    if "Matig"  in stored: return "⚠️ Matig"
                    if "Vermijd" in stored: return "❌ Vermijd"
                    # Schatting voor bets zonder opgeslagen rating
                    ev_val = float(r.get("ev_score") or 0)
                    if ev_val >= 0.20:   return "✅ Sterk ~"
                    elif ev_val >= 0.05: return "⚠️ Matig ~"
                    else:               return "❌ Vermijd ~"

                _tier_agg: dict = {}
                _has_stored_rating = False
                for _mr in _model_bets:
                    _tier = _derive_rating_tier(_mr)
                    if "~" not in _tier:
                        _has_stored_rating = True
                    if _tier not in _tier_agg:
                        _tier_agg[_tier] = {"n": 0, "won": 0, "ev_sum": 0.0,
                                            "wl_sum": 0.0, "inzet_sum": 0.0}
                    _td = _tier_agg[_tier]
                    _td["n"] += 1
                    if _mr.get("uitkomst") == "gewonnen": _td["won"] += 1
                    _td["ev_sum"]    += float(_mr.get("ev_score") or 0)
                    _td["wl_sum"]    += float(_mr.get("winst_verlies") or 0)
                    _td["inzet_sum"] += float(_mr.get("inzet") or 0)

                _tier_order = ["✅ Sterk", "✅ Sterk ~", "⚠️ Matig", "⚠️ Matig ~",
                               "❌ Vermijd", "❌ Vermijd ~"]
                _tier_rows = []
                for _tier in _tier_order:
                    _td = _tier_agg.get(_tier)
                    if not _td or _td["n"] == 0:
                        continue
                    _t_winpct = _td["won"] / _td["n"] * 100
                    _t_gem_ev = _td["ev_sum"] / _td["n"]
                    _t_roi    = (_td["wl_sum"] / _td["inzet_sum"] * 100) if _td["inzet_sum"] > 0 else 0.0
                    _t_diff   = _t_roi - _t_gem_ev * 100
                    _t_sig    = ("✅ Op schema" if abs(_t_diff) < 10
                                 else ("🚀 Beter dan verwacht" if _t_diff > 10
                                       else "⚠️ Onder verwachting"))
                    _tier_rows.append({
                        "Rating": _tier, "N": _td["n"],
                        "Win %": f"{_t_winpct:.0f}%",
                        "Gem. EV": f"{_t_gem_ev:+.3f}",
                        "Werkelijk ROI": f"{_t_roi:+.1f}%",
                        "Signaal": _t_sig,
                    })
                if _tier_rows:
                    st.dataframe(pd.DataFrame(_tier_rows), hide_index=True, use_container_width=True)
                    if not _has_stored_rating:
                        st.caption("~ = rating geschat op EV score. "
                                   "Exacte ratings worden opgeslagen voor nieuwe bets.")

                # ── EV Kalibratie Buckets ─────────────────────────────────────────
                st.markdown("##### 📊 EV Kalibratie — voorspeld vs werkelijk")

                _ev_bucket_defs = [
                    ("<0",       None,  0.0),
                    ("0.00–0.09", 0.0,  0.10),
                    ("0.10–0.19", 0.10, 0.20),
                    ("0.20–0.29", 0.20, 0.30),
                    ("0.30+",    0.30,  None),
                ]
                _calib_rows = []
                for _bl, _blo, _bhi in _ev_bucket_defs:
                    if _blo is None:
                        _bb = [r for r in _model_bets if float(r.get("ev_score") or 0) < _bhi]
                    elif _bhi is None:
                        _bb = [r for r in _model_bets if float(r.get("ev_score") or 0) >= _blo]
                    else:
                        _bb = [r for r in _model_bets
                               if _blo <= float(r.get("ev_score") or 0) < _bhi]
                    if len(_bb) < 2:
                        continue
                    _bn      = len(_bb)
                    _bwon    = sum(1 for r in _bb if r.get("uitkomst") == "gewonnen")
                    _bgev    = sum(float(r.get("ev_score") or 0) for r in _bb) / _bn
                    _bwl     = sum(float(r.get("winst_verlies") or 0) for r in _bb)
                    _binzet  = sum(float(r.get("inzet") or 0) for r in _bb)
                    _broi    = (_bwl / _binzet * 100) if _binzet > 0 else 0.0
                    _bvoor   = _bgev * 100
                    _bdelta  = _broi - _bvoor
                    _bsig    = "🟢" if _bdelta >= -5 else ("🟡" if _bdelta >= -15 else "🔴")
                    _calib_rows.append({
                        "EV Bucket": _bl, "N": _bn,
                        "Win %": f"{_bwon/_bn*100:.0f}%",
                        "Voorspeld ROI": f"{_bvoor:+.1f}%",
                        "Werkelijk ROI": f"{_broi:+.1f}%",
                        "Delta": f"{_bdelta:+.1f}% {_bsig}",
                    })
                if _calib_rows:
                    st.dataframe(pd.DataFrame(_calib_rows), hide_index=True, use_container_width=True)
                    st.caption("🟢 delta ≥ −5%  ·  🟡 delta −5% tot −15%  ·  🔴 delta < −15%")

                # ── Modelnauwkeurigheid per sport ─────────────────────────────────
                st.markdown("##### 🏟️ Modelnauwkeurigheid per sport")
                _sport_model_rows = []
                for _bsport in sorted({r.get("sport", "?") for r in _model_bets}):
                    _sr = [r for r in _model_bets if r.get("sport", "") == _bsport]
                    if len(_sr) < 3:
                        continue
                    _s_won   = sum(1 for r in _sr if r.get("uitkomst") == "gewonnen")
                    _s_inzet = sum(float(r.get("inzet") or 0) for r in _sr)
                    _s_wl    = sum(float(r.get("winst_verlies") or 0) for r in _sr)
                    _s_ev    = [float(r.get("ev_score") or 0) for r in _sr]
                    _s_gev   = sum(_s_ev) / len(_s_ev)
                    _s_roi   = (_s_wl / _s_inzet * 100) if _s_inzet > 0 else 0.0
                    _s_bias  = _s_roi - _s_gev * 100
                    _s_sig   = "🟢" if abs(_s_bias) < 10 else ("🟡" if abs(_s_bias) < 20 else "🔴")
                    # Waarschuwing als sport significant onder verwachting presteert
                    if _s_gev > 0 and _s_roi < _s_gev * 100 * 0.5 and _s_roi < 0:
                        st.warning(f"⚠️ {_bsport} presteert significant onder verwachting "
                                   f"(voorspeld {_s_gev*100:+.1f}%, werkelijk {_s_roi:+.1f}%)")
                    _sport_model_rows.append({
                        "Sport": _bsport, "N": len(_sr),
                        "Win %": f"{_s_won/len(_sr)*100:.0f}%",
                        "Gem. EV": f"{_s_gev:+.3f}",
                        "ROI": f"{_s_roi:+.1f}%",
                        "Model bias": f"{_s_bias:+.1f}% {_s_sig}",
                    })
                if _sport_model_rows:
                    st.dataframe(pd.DataFrame(_sport_model_rows), hide_index=True, use_container_width=True)
    
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
            # _all_parlays_bk already loaded above (reuse, avoids duplicate DB call)
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

                # ── Parlay Leg Accuracy ───────────────────────────────────────────
                # Analyseer per leg of die geraakt of gemist was, ongeacht parlay-uitkomst.
                # Geeft een eerlijk beeld van de kwaliteit van individuele picks.
                st.markdown("---")
                st.markdown("#### 🎲 Parlay Leg Accuracy")
                st.caption(
                    "Hoeveel individuele parlay-legs waren correct, ongeacht de parlay-uitkomst. "
                    "Geeft een eerlijker beeld van pick-kwaliteit dan alleen parlay win/loss."
                )

                def _find_leg_status(legs_dict, player, bet_type, idx):
                    """Zoek leg-status op via exacte key, index-prefix of positie."""
                    _exact = f"{player}_{bet_type}"
                    if _exact in legs_dict:
                        return legs_dict[_exact]
                    _indexed = f"{idx}_{player}_{bet_type}"
                    if _indexed in legs_dict:
                        return legs_dict[_indexed]
                    _vals = list(legs_dict.values())
                    return _vals[idx] if idx < len(_vals) else "open"

                _settled_prl = [
                    p for p in _all_parlays_bk
                    if (p.get("uitkomst") or "open") in ("gewonnen", "verloren")
                ]

                # Verzamel alle leg-uitkomsten (alleen geraakt/gemist)
                _leg_records = []  # list of {"sport": str, "status": str}
                for _pla in _settled_prl:
                    _props_la  = _pla.get("props_json") or []
                    _legs_la   = _pla.get("legs_json")
                    if not _legs_la or not isinstance(_legs_la, dict):
                        continue
                    for _li, _prop_la in enumerate(_props_la):
                        _st_la = _find_leg_status(
                            _legs_la,
                            str(_prop_la.get("player") or ""),
                            str(_prop_la.get("bet_type") or ""),
                            _li,
                        )
                        if _st_la in ("geraakt", "gemist"):
                            _leg_records.append({
                                "sport":  _prop_la.get("sport") or "Overig",
                                "status": _st_la,
                            })

                if not _leg_records:
                    st.info(
                        "💡 Nog geen leg-uitkomsten bijgehouden. "
                        "Markeer individuele legs als **geraakt** of **gemist** in de "
                        "Parlay Builder tab na afloop van een wedstrijd."
                    )
                else:
                    _la_n    = len(_leg_records)
                    _la_hit  = sum(1 for r in _leg_records if r["status"] == "geraakt")
                    _la_miss = _la_n - _la_hit
                    _la_hr   = _la_hit / _la_n * 100 if _la_n else 0

                    _lac1, _lac2, _lac3, _lac4 = st.columns(4)
                    _lac1.metric("Legs bijgehouden", _la_n)
                    _lac2.metric("Geraakt ✅",        _la_hit)
                    _lac3.metric("Gemist ❌",         _la_miss)
                    _lac4.metric("Leg Hit Rate",      f"{_la_hr:.1f}%")

                    # Per sport
                    _la_sport: dict = {}
                    for _rec in _leg_records:
                        _sp = _rec["sport"]
                        _la_sport.setdefault(_sp, {"n": 0, "hit": 0})
                        _la_sport[_sp]["n"] += 1
                        if _rec["status"] == "geraakt":
                            _la_sport[_sp]["hit"] += 1

                    if len(_la_sport) > 1:
                        _la_rows = [
                            {
                                "Sport":    _sp,
                                "Legs":     _d["n"],
                                "Geraakt":  _d["hit"],
                                "Gemist":   _d["n"] - _d["hit"],
                                "Hit Rate": f"{_d['hit']/_d['n']*100:.0f}%",
                            }
                            for _sp, _d in sorted(
                                _la_sport.items(),
                                key=lambda x: x[1]["n"],
                                reverse=True,
                            )
                        ]
                        st.dataframe(
                            pd.DataFrame(_la_rows),
                            use_container_width=True,
                            hide_index=True,
                        )

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
            _k_current_bk = _start_bk_saved + sum(r.get("winst_verlies",0) for r in db.load_resultaten() if r.get("uitkomst") in ("gewonnen","verloren","void"))
            _k_euro = max(0.0, _k_current_bk * _k_advised)
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

    _fv     = st.session_state.parlay_form_ver
    _sports = ["NHL", "NBA", "MLB", "Voetbal", "Overig"]
    _sport_idx = _sports.index(st.session_state.parlay_last_sport) \
                 if st.session_state.parlay_last_sport in _sports else 0

    _ph1, _ph2 = st.columns(2)
    _p_speler  = _ph1.text_input("Speler / Team (optioneel)", key=f"p_speler_{_fv}",
                                  placeholder="bijv. Auston Matthews")
    _p_sport   = _ph2.selectbox("Sport", _sports,
                                 index=_sport_idx, key=f"p_sport_{_fv}")
    _bet_types  = ["Player Prop", "Match Result", "Odds Boost", "Other"]
    _ph3, _ph4 = st.columns(2)
    _p_bet_cat = _ph3.selectbox("Bet type", _bet_types, key=f"p_bet_cat_{_fv}")
    _p_odds    = _ph4.number_input("Odds", min_value=1.01, max_value=50.0,
                                    value=2.00, step=0.05, format="%.2f",
                                    key=f"p_odds_{_fv}")
    _p_bet_det = st.text_input("Details", key=f"p_bet_det_{_fv}",
                                placeholder="bijv. Anytime Goal Scorer, Over 2.5 goals, Colorado Avalanche to win")
    _p_bet     = f"{_p_bet_cat} — {_p_bet_det.strip()}" if _p_bet_det.strip() else _p_bet_cat

    # Hit rate: echt optioneel — vink aan om in te vullen
    _p_use_hr = st.checkbox("Hit rate opgeven", key=f"p_use_hr_{_fv}", value=False,
                            help="Laat uitgevinkt als je de hit rate niet weet of wilt meenemen.")
    _p_hr_val = None
    if _p_use_hr:
        _p_hr_val = st.number_input("Hit rate %", min_value=0, max_value=100,
                                     value=50, step=1, key=f"p_hr_{_fv}",
                                     help="Schatting van de kans dat de prop slaagt.")

    _padd_col, _pwis_col = st.columns([3, 1])
    if _padd_col.button("➕ Voeg toe aan parlay", key="p_add_leg",
                        type="primary", use_container_width=True):
        st.session_state.parlay_last_sport = _p_sport
        st.session_state.parlay_legs.append({
            "player":   _p_speler,
            "sport":    _p_sport,
            "bet_type": _p_bet,
            "odds":     float(_p_odds),
            "hit_rate": float(_p_hr_val) / 100 if _p_hr_val is not None else None,
        })
        st.session_state.parlay_form_ver += 1
        st.rerun()
    if _pwis_col.button("🗑️ Wissen", key="p_clear_form", use_container_width=True,
                        help="Leeg het formulier"):
        st.session_state.parlay_form_ver += 1
        st.rerun()

    _sc_client_p = anthropic.Anthropic(api_key=api_key) if ANTHROPIC_AVAILABLE and api_key else None
    screenshot_import.render_screenshot_import("parlay", client=_sc_client_p)

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
            _hr_val = _leg.get("hit_rate")
            _hr_lbl = f"{_hr_val*100:.0f}%" if _hr_val is not None else "—"
            _lc3.caption(f"HR: {_hr_lbl}")
            if _lc4.button("🗑️", key=f"rmleg_{_li}"):
                legs_to_remove.append(_li)
        for _idx in sorted(legs_to_remove, reverse=True):
            st.session_state.parlay_legs.pop(_idx)
        if legs_to_remove:
            st.rerun()

        _legs      = st.session_state.parlay_legs
        _comb_odds = 1.0
        _hit_ch    = 1.0
        _no_hr_cnt = 0
        for _leg in _legs:
            _comb_odds *= float(_leg.get("odds", 1.5))
            _hr = _leg.get("hit_rate")
            if _hr is not None:
                _hit_ch *= float(_hr)
            else:
                _no_hr_cnt += 1

        _hit_ch_known = _no_hr_cnt == 0   # alle legs hebben een hit rate
        _p_ev  = _hit_ch * (_comb_odds - 1) - (1 - _hit_ch) if _hit_ch_known else None
        _inzet = st.number_input("💰 Inzet (€)", min_value=1.0, max_value=10000.0,
                                  value=10.0, step=1.0, key="parlay_inzet")
        _winst = _inzet * _comb_odds - _inzet
        _ev_s2 = (f"+{_p_ev:.3f}" if _p_ev >= 0 else f"{_p_ev:.3f}") if _p_ev is not None else "—"

        if _no_hr_cnt > 0:
            st.caption(f"ℹ️ {_no_hr_cnt} leg(s) zonder hit rate — hit kans en EV worden niet berekend.")

        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _mc1.metric("Gecombineerde Odds", f"{_comb_odds:.2f}")
        _mc2.metric("Hit Kans",           f"{_hit_ch*100:.1f}%" if _hit_ch_known else "—")
        _mc3.metric("Parlay EV",          _ev_s2)
        _mc4.metric(f"Winst bij €{_inzet:.0f}", f"€{_winst:.2f}")

        if _p_ev is None:
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Voeg hit rates toe aan alle legs om EV te berekenen.</small>', unsafe_allow_html=True)
        elif _p_ev < 0:
            st.warning(f"⚠️ Negatieve EV ({_ev_s2}) — verliesgevend op lange termijn.")
        else:
            st.success(f"✅ Positieve EV ({_ev_s2})")

        _p_direct = st.checkbox(
            "📋 Direct registreren als geplaatste weddenschap",
            key="parlay_direct", value=False,
            help="Voegt de parlay ook direct toe aan Geplaatste Bets met status 'open'. "
                 "Je kunt de uitkomst later settleren in de Geplaatste Bets tab.",
        )

        _pb1, _pb2 = st.columns(2)
        if _pb1.button("⭐ Sla parlay op", use_container_width=True, type="primary"):
            _new_prl_id = str(uuid.uuid4())[:8]
            db.save_parlay({
                "id":                 _new_prl_id,
                "datum":              datetime.datetime.now().isoformat(),
                "props_json":         list(_legs),
                "gecombineerde_odds": round(_comb_odds, 4),
                "hit_kans":           round(_hit_ch, 6) if _hit_ch_known else None,
                "ev_score":           round(_p_ev, 6) if _p_ev is not None else None,
                "inzet":              float(_inzet),
                "uitkomst":           "open",
                "winst_verlies":      0.0,
                "legs_json":          {l.get("player","")+"_"+l.get("bet_type",""): "open" for l in _legs},
            })
            if _p_direct:
                _prl_fav_direct = {
                    "player":        f"🎰 Parlay ({len(_legs)} legs)",
                    "bet_type":      f"Parlay ({len(_legs)} legs)",
                    "sport":         "Parlay",
                    "odds":          round(_comb_odds, 4),
                    "ev":            round(_p_ev, 6) if _p_ev is not None else 0.0,
                    "ev_score":      round(_p_ev, 6) if _p_ev is not None else 0.0,
                    "speler":        f"🎰 Parlay ({len(_legs)} legs)",
                    "bet":           ", ".join(
                        l.get("player","") or l.get("bet_type","") for l in _legs[:3]
                    ),
                    "datum":         datetime.datetime.now().isoformat(),
                    "import_method": "handmatig",
                    "bookmaker":     "",
                    "rating":        "",
                    "composite":     0.0,
                }
                db.upsert_resultaat(f"parlay_{_new_prl_id}", _prl_fav_direct, "open", float(_inzet))
            st.session_state.parlay_legs = []
            st.success("✅ Parlay opgeslagen!")
            st.rerun()
        if _pb2.button("🗑️ Wis parlay", use_container_width=True):
            st.session_state.parlay_legs = []
            st.rerun()
    else:
        st.markdown('<small style="color:#a0c4ff;">ℹ️ Voeg props toe om een parlay te bouwen.</small>', unsafe_allow_html=True)

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
            _prl_ev_raw = _prl.get("ev_score")
            _prl_ev_s = (f"+{_prl_ev_raw:.3f}" if _prl_ev_raw >= 0 else f"{_prl_ev_raw:.3f}") \
                        if _prl_ev_raw is not None else "—"
            with st.expander(
                f"🎯 {len(_prl_legs)} legs · Odds {_prl.get('gecombineerde_odds',0):.2f}"
                f" · EV {_prl_ev_s} · {(_prl.get('uitkomst') or 'open').upper()}"
            ):
                _upd_legs = dict(_prl_lj)
                _changed  = False
                _leg_opts = ["open", "geraakt", "gemist", "void"]
                for _leg_idx, _pleg in enumerate(_prl_legs):
                    _lk  = str(_pleg.get("player","")) + "_" + str(_pleg.get("bet_type",""))
                    _lst = _upd_legs.get(_lk, "open")
                    _plc1, _plc2 = st.columns([3, 2])
                    _plc1.write(f"**{_pleg.get('player','')}** — {_pleg.get('bet_type','')} @ {_pleg.get('odds','—')}")
                    _nst = _plc2.selectbox(
                        "Status", options=_leg_opts,
                        index=_leg_opts.index(_lst) if _lst in _leg_opts else 0,
                        key=f"legst_{_prl.get('id','')}_{_leg_idx}_{_lk}", label_visibility="collapsed",
                    )
                    if _nst != _lst:
                        _upd_legs[_lk] = _nst
                        _changed = True
                if _changed:
                    # Herbereken gecombineerde odds: void legs tellen niet mee
                    _eff_odds = 1.0
                    for _leg_item in _prl_legs:
                        _lk_item = str(_leg_item.get("player","")) + "_" + str(_leg_item.get("bet_type",""))
                        if _upd_legs.get(_lk_item, "open") != "void":
                            try:
                                _eff_odds *= float(_leg_item.get("odds") or 1.0)
                            except Exception:
                                pass
                    _eff_odds = round(max(_eff_odds, 1.0), 4)
                    _stored_odds = float(_prl.get("gecombineerde_odds", 1.0) or 1.0)
                    _upd_parlay_fields = {"legs_json": _upd_legs}
                    if abs(_eff_odds - _stored_odds) > 0.001:
                        _upd_parlay_fields["gecombineerde_odds"] = _eff_odds
                    db.update_parlay(_prl.get("id",""), _upd_parlay_fields)
                    # Auto-settle op basis van leg-statussen (alleen als parlay nog open is)
                    if (_prl.get("uitkomst") or "open") == "open":
                        _auto_id    = _prl.get("id","")
                        _auto_inzet = float(_prl.get("inzet", 10) or 10)
                        _auto_fav_base = {
                            "odds":       _eff_odds,
                            "datum":      datetime.date.today().isoformat(),
                            "speler":     f"🎰 Parlay ({len(_prl_legs)} legs)",
                            "bet":        ", ".join(str(l.get("player","")) for l in _prl_legs[:3]) or "Parlay",
                            "sport":      "Parlay",
                            "ev_score":   float(_prl.get("ev_score") or 0.0),
                            "props_json": _prl_legs,
                        }
                        # Alle leg-statussen ophalen (ook voor legs die nog niet gewijzigd zijn)
                        _all_keys     = [str(l.get("player","")) + "_" + str(l.get("bet_type","")) for l in _prl_legs]
                        _all_statuses = [_upd_legs.get(k, "open") for k in _all_keys]
                        if any(s == "gemist" for s in _all_statuses):
                            # Eén leg gemist → parlay verloren
                            db.update_parlay(_auto_id, {"uitkomst": "verloren", "winst_verlies": round(-_auto_inzet, 2)})
                            db.upsert_resultaat(f"parlay_{_auto_id}", _auto_fav_base, "verloren", _auto_inzet)
                        elif all(s != "open" for s in _all_statuses) and _all_statuses:
                            # Alle legs gesettled, geen gemist
                            if all(s == "void" for s in _all_statuses):
                                # Alle legs void → parlay void
                                db.update_parlay(_auto_id, {"uitkomst": "void", "winst_verlies": 0.0})
                                db.upsert_resultaat(f"parlay_{_auto_id}", _auto_fav_base, "void", _auto_inzet)
                            else:
                                # Alle legs geraakt (of mix geraakt+void) → parlay gewonnen
                                # Uitbetaling op basis van effectieve odds (zonder void legs)
                                _pw = round(_auto_inzet * _eff_odds - _auto_inzet, 2)
                                db.update_parlay(_auto_id, {"uitkomst": "gewonnen", "winst_verlies": _pw})
                                db.upsert_resultaat(f"parlay_{_auto_id}", _auto_fav_base, "gewonnen", _auto_inzet)
                    st.rerun()

                _oc1, _oc2, _oc3 = st.columns(3)
                if (_prl.get("uitkomst") or "open") == "open":
                    if _oc1.button("✅ Gewonnen", key=f"pwon_{_prl.get('id','')}"):
                        _prl_id    = _prl.get("id","")
                        _prl_inzet = _prl.get("inzet", 10)
                        _prl_odds  = _prl.get("gecombineerde_odds", 1.0)
                        _pw = round(_prl_inzet * _prl_odds - _prl_inzet, 2)
                        db.update_parlay(_prl_id, {"uitkomst":"gewonnen","winst_verlies":_pw})
                        _prl_legs = _prl.get("props_json", []) or []
                        _prl_fav  = {
                            "odds":      _prl_odds,
                            "datum":     datetime.date.today().isoformat(),
                            "speler":    f"🎰 Parlay ({len(_prl_legs)} legs)",
                            "bet":       ", ".join([str(l.get("player","")) for l in _prl_legs[:3]]) or "Parlay",
                            "sport":     "Parlay",
                            "ev_score":  float(_prl.get("ev_score") or 0.0),
                            "props_json": _prl_legs,
                        }
                        db.upsert_resultaat(f"parlay_{_prl_id}", _prl_fav, "gewonnen", _prl_inzet)
                        st.rerun()
                    if _oc2.button("❌ Verloren", key=f"plost_{_prl.get('id','')}"):
                        _prl_id    = _prl.get("id","")
                        _prl_inzet = _prl.get("inzet", 10)
                        _prl_odds  = _prl.get("gecombineerde_odds", 1.0)
                        db.update_parlay(_prl_id, {"uitkomst":"verloren","winst_verlies":round(-_prl_inzet,2)})
                        _prl_legs = _prl.get("props_json", []) or []
                        _prl_fav  = {
                            "odds":      _prl_odds,
                            "datum":     datetime.date.today().isoformat(),
                            "speler":    f"🎰 Parlay ({len(_prl_legs)} legs)",
                            "bet":       ", ".join([str(l.get("player","")) for l in _prl_legs[:3]]) or "Parlay",
                            "sport":     "Parlay",
                            "ev_score":  float(_prl.get("ev_score") or 0.0),
                            "props_json": _prl_legs,
                        }
                        db.upsert_resultaat(f"parlay_{_prl_id}", _prl_fav, "verloren", _prl_inzet)
                        st.rerun()
                else:
                    _wv  = _prl.get("winst_verlies", 0) or 0
                    _uit = _prl.get("uitkomst") or ""
                    _kl  = "#4ade80" if _wv > 0 else ("#a0a0c0" if _uit == "void" else "#f87171")
                    st.markdown(f"<span style='color:{_kl};font-weight:700'>Uitkomst: {_uit.upper()} · W/V: €{_wv:.2f}</span>", unsafe_allow_html=True)
                if _oc3.button("🗑️ Verwijder", key=f"pdel_{_prl.get('id','')}"):
                    db.delete_parlay(_prl.get("id",""))
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — GEPLAATSTE BETS
# ══════════════════════════════════════════════════════════════════════════════

with tab_geplaatst:
    st.markdown("### 📋 Geplaatste Weddenschappen")
    st.caption("Alle weddenschappen die je hebt ingezet, gerangschikt per maand en week.")

    _sc_client_g = anthropic.Anthropic(api_key=api_key) if ANTHROPIC_AVAILABLE and api_key else None
    screenshot_import.render_screenshot_import("geplaatste_bets", client=_sc_client_g)

    st.markdown("---")

    from datetime import date as _date

    _alle_res_gp = db.load_resultaten()

    # Laad alle parlays één keer, zodat we per parlay-rij de legs kunnen tonen
    # in een expander. Map op de *resultaten*-id (= "parlay_<orig>").
    _gp_all_parlays    = db.load_parlays()
    _gp_parlays_by_rid = {f"parlay_{p['id']}": p for p in _gp_all_parlays}

    # ── Open en ontbrekende parlays toevoegen ─────────────────────────────────
    # Open parlays staan nooit in resultaten; gesettlede parlays die vóór de
    # settlement-fix zijn opgeslagen kunnen ook ontbreken. Beide gevallen worden
    # hier hersteld zodat de tab een compleet beeld geeft.
    _gp_bestaande_parlay_ids = {
        r.get("id", "") for r in _alle_res_gp
        if str(r.get("id", "")).startswith("parlay_")
    }
    for _gpp in _gp_all_parlays:
        _gpp_res_id = f"parlay_{_gpp['id']}"
        if _gpp_res_id in _gp_bestaande_parlay_ids:
            continue  # al aanwezig in resultaten, niet dubbel tellen
        _gpp_legs = _gpp.get("props_json") or []
        _alle_res_gp.append({
            "id":            _gpp_res_id,
            "datum":         (_gpp.get("datum") or datetime.date.today().isoformat())[:10],
            "speler":        f"🎰 Parlay ({len(_gpp_legs)} legs)",
            "bet":           f"{len(_gpp_legs)}-leg parlay",
            "sport":         "Parlay",
            "odds":          float(_gpp.get("gecombineerde_odds") or 1.0),
            "inzet":         float(_gpp.get("inzet") or 0),
            "uitkomst":      _gpp.get("uitkomst") or "open",
            "winst_verlies": float(_gpp.get("winst_verlies") or 0),
            "ev_score":      float(_gpp.get("ev_score") or 0),
            "is_parlay":     True,
        })

    if not _alle_res_gp:
        st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen weddenschappen geregistreerd. Voeg bets toe via de Shortlist of Bankroll tab.</small>', unsafe_allow_html=True)
    else:
        # ── Filters ──────────────────────────────────────────────────────────
        _gp_c1, _gp_c2, _gp_c3 = st.columns(3)
        _gp_sport    = _gp_c1.selectbox("Sport", ["Alles","NHL","NBA","MLB","Voetbal","Overig"], key="gp_sport")
        _gp_uitkomst = _gp_c2.selectbox("Uitkomst", ["Alles","open","gewonnen","verloren","void"], key="gp_uitkomst")
        _gp_zoek     = _gp_c3.text_input("🔍 Zoek speler", key="gp_zoek", placeholder="naam...")

        _gp_data = _alle_res_gp
        if _gp_sport != "Alles":
            _gp_data = [r for r in _gp_data if _gp_sport.lower() in (r.get("sport") or "").lower()]
        if _gp_uitkomst != "Alles":
            _gp_data = [r for r in _gp_data if r.get("uitkomst","") == _gp_uitkomst]
        if _gp_zoek:
            _gp_data = [r for r in _gp_data if _gp_zoek.lower() in (r.get("speler") or "").lower()]

        if not _gp_data:
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Geen weddenschappen gevonden met deze filters.</small>', unsafe_allow_html=True)
        else:
            # ── Totaalsamenvatting ────────────────────────────────────────────
            _gp_afgerond = [r for r in _gp_data if r.get("uitkomst") in ("gewonnen","verloren","void")]
            if _gp_afgerond:
                _gp_won   = sum(1 for r in _gp_afgerond if r.get("uitkomst") == "gewonnen")
                _gp_inzet = sum(r.get("inzet", 0) for r in _gp_afgerond)
                _gp_wl    = sum(r.get("winst_verlies", 0) for r in _gp_afgerond)
                _gp_roi   = (_gp_wl / _gp_inzet * 100) if _gp_inzet > 0 else 0.0
                _gp_wr    = (_gp_won / len(_gp_afgerond) * 100) if _gp_afgerond else 0.0
                sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                sc1.markdown(kpi_card("🎰", "Totaal bets",  str(len(_gp_data)), f"{len(_gp_afgerond)} afgerond"), unsafe_allow_html=True)
                sc2.markdown(kpi_card("🎯", "Win rate",     f"{_gp_wr:.1f}%",   f"{_gp_won}/{len(_gp_afgerond)}", positive=(_gp_wr >= 55) if _gp_afgerond else None), unsafe_allow_html=True)
                sc3.markdown(kpi_card("💶", "Totale inzet", f"€{_gp_inzet:.2f}"), unsafe_allow_html=True)
                sc4.markdown(kpi_card("💰", "P&L",          f"€{_gp_wl:+.2f}",  positive=(_gp_wl > 0) if _gp_wl != 0 else None, tooltip=f"€{_gp_wl:+.4f}"), unsafe_allow_html=True)
                sc5.markdown(kpi_card("📈", "ROI",          f"{_gp_roi:+.1f}%", positive=(_gp_roi > 0) if _gp_roi != 0 else None, tooltip=f"{_gp_roi:+.4f}%"), unsafe_allow_html=True)
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
                _m_rijen      = [r for wk in _weken.values() for r in wk]
                _m_afgerond   = [r for r in _m_rijen if r.get("uitkomst") in ("gewonnen","verloren","void")]
                _m_open       = [r for r in _m_rijen if r.get("uitkomst") == "open"]
                _m_won        = sum(1 for r in _m_afgerond if r.get("uitkomst") == "gewonnen")
                _m_inzet_set  = sum(float(r.get("inzet") or 0) for r in _m_afgerond)
                _m_inzet_tot  = sum(float(r.get("inzet") or 0) for r in _m_rijen)
                _m_open_st    = sum(float(r.get("inzet") or 0) for r in _m_open)
                _m_open_pot   = sum(float(r.get("inzet") or 0) * float(r.get("odds") or 1.0) for r in _m_open)
                _m_wl         = sum(float(r.get("winst_verlies") or 0) for r in _m_afgerond)
                _m_roi        = (_m_wl / _m_inzet_set * 100) if _m_inzet_set > 0 else 0.0
                _m_wr_str     = f"{_m_won}/{len(_m_afgerond)}" if _m_afgerond else "—"
                _m_wl_str     = f"€{_m_wl:+.2f}" if _m_afgerond else "—"
                _m_roi_str    = f"{_m_roi:+.1f}%" if _m_afgerond else "—"
                _m_open_str   = f"{len(_m_open)} open €{_m_open_st:.0f}" if _m_open else "0 open"

                with st.expander(
                    f"📅 **{_maand}**  ·  {len(_m_rijen)} bets  ·  {_m_open_str}"
                    f"  ·  Stake €{_m_inzet_tot:.0f}  ·  W/L {_m_wr_str}"
                    f"  ·  P&L {_m_wl_str}  ·  ROI {_m_roi_str}",
                    expanded=True,
                ):
                    # ── Open bets samenvatting (wat staat er nog uit?) ─────
                    if _m_open:
                        _m_open_winst = _m_open_pot - _m_open_st
                        st.markdown(
                            f"<div style='background:#11112b;border:1px solid #2e2e56;"
                            f"border-radius:6px;padding:10px 14px;margin:4px 0 10px 0;"
                            f"font-size:0.88rem;'>"
                            f"<span style='color:#a8aace;'>⏳ <strong>Nog open:</strong></span> "
                            f"<span style='color:#e8eaf6;'>{len(_m_open)} bets</span>"
                            f"<span style='color:#6868a0;'> · </span>"
                            f"<span style='color:#e8eaf6;'>stake <strong>€{_m_open_st:.2f}</strong></span>"
                            f"<span style='color:#6868a0;'> · </span>"
                            f"<span style='color:#7c3aed;font-weight:600;'>"
                            f"potenti&euml;le uitbetaling €{_m_open_pot:.2f}</span>"
                            f"<span style='color:#6868a0;'> (winst €{_m_open_winst:+.2f})</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                    # ── Per-sport uitsplitsing ─────────────────────────────
                    _per_sport: dict = OrderedDict()
                    for _r in _m_rijen:
                        _sp = _r.get("sport") or "Overig"
                        if _sp not in _per_sport:
                            _per_sport[_sp] = {
                                "n": 0, "open": 0, "stake": 0.0,
                                "settled_stake": 0.0, "pnl": 0.0,
                                "won": 0, "lost": 0,
                            }
                        _ps = _per_sport[_sp]
                        _ps["n"]     += 1
                        _ps["stake"] += float(_r.get("inzet") or 0)
                        _u = _r.get("uitkomst") or ""
                        if _u == "open":
                            _ps["open"] += 1
                        elif _u in ("gewonnen", "verloren", "void"):
                            _ps["settled_stake"] += float(_r.get("inzet") or 0)
                            _ps["pnl"] += float(_r.get("winst_verlies") or 0)
                            if _u == "gewonnen":
                                _ps["won"] += 1
                            elif _u == "verloren":
                                _ps["lost"] += 1
                    if len(_per_sport) > 1:
                        _ps_rows = []
                        # Sorteer op aantal bets aflopend
                        _ps_sorted = sorted(_per_sport.items(),
                                            key=lambda x: x[1]["n"], reverse=True)
                        for _sp, _d in _ps_sorted:
                            _spi = (SPORT_ICONS.get(_sp.upper(), "⚽")
                                    if _sp != "Parlay" else "🎰")
                            _r_pnl   = _d["pnl"]
                            _r_settl = _d["won"] + _d["lost"]
                            _r_wr    = f"{_d['won']}/{_r_settl}" if _r_settl else "—"
                            _r_roi   = ((_r_pnl / _d["settled_stake"] * 100)
                                        if _d["settled_stake"] > 0 else None)
                            _pnl_col = ("#4ade80" if _r_pnl > 0
                                        else "#f87171" if _r_pnl < 0
                                        else "#a8aace")
                            _pnl_str = (f"€{_r_pnl:+.2f}"
                                        if _d["settled_stake"] > 0 else "—")
                            _roi_str = (f"{_r_roi:+.1f}%"
                                        if _r_roi is not None else "—")
                            _open_col = "#7c3aed" if _d["open"] > 0 else "#6868a0"
                            _ps_rows.append(
                                f"<tr style='border-top:1px solid #1c1c3a;'>"
                                f"<td style='padding:6px 10px;color:#e8eaf6;'>"
                                f"{_spi} {_sp}</td>"
                                f"<td style='padding:6px 10px;text-align:right;color:#e8eaf6;'>"
                                f"{_d['n']}</td>"
                                f"<td style='padding:6px 10px;text-align:right;color:{_open_col};'>"
                                f"{_d['open']}</td>"
                                f"<td style='padding:6px 10px;text-align:right;color:#e8eaf6;'>"
                                f"€{_d['stake']:.2f}</td>"
                                f"<td style='padding:6px 10px;text-align:right;color:#a8aace;'>"
                                f"{_r_wr}</td>"
                                f"<td style='padding:6px 10px;text-align:right;color:{_pnl_col};font-weight:600;'>"
                                f"{_pnl_str}</td>"
                                f"<td style='padding:6px 10px;text-align:right;color:{_pnl_col};'>"
                                f"{_roi_str}</td>"
                                f"</tr>"
                            )
                        st.markdown(
                            f"<div style='background:#0e0e24;border:1px solid #2e2e56;"
                            f"border-radius:6px;padding:8px 4px 4px 4px;margin:4px 0 12px 0;'>"
                            f"<div style='padding:0 10px 4px 10px;color:#a8aace;"
                            f"font-size:0.82rem;font-weight:600;'>📊 Per sport</div>"
                            f"<table style='width:100%;border-collapse:collapse;"
                            f"font-size:0.85rem;'>"
                            f"<thead><tr style='color:#6868a0;font-size:0.78rem;"
                            f"text-transform:uppercase;letter-spacing:0.4px;'>"
                            f"<th style='padding:4px 10px;text-align:left;'>Sport</th>"
                            f"<th style='padding:4px 10px;text-align:right;'>Bets</th>"
                            f"<th style='padding:4px 10px;text-align:right;'>Open</th>"
                            f"<th style='padding:4px 10px;text-align:right;'>Stake</th>"
                            f"<th style='padding:4px 10px;text-align:right;'>W/L</th>"
                            f"<th style='padding:4px 10px;text-align:right;'>P&amp;L</th>"
                            f"<th style='padding:4px 10px;text-align:right;'>ROI</th>"
                            f"</tr></thead><tbody>"
                            + "".join(_ps_rows) +
                            f"</tbody></table></div>",
                            unsafe_allow_html=True,
                        )

                    for _week, _bets in _weken.items():
                        # Week samenvatting
                        _w_afgerond = [r for r in _bets if r.get("uitkomst") in ("gewonnen","verloren","void")]
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
                            _b_id   = _b.get("id","")
                            _b_uit  = _b.get("uitkomst","")
                            _b_icon = "✅" if _b_uit == "gewonnen" else ("❌" if _b_uit == "verloren" else ("⚪" if _b_uit == "void" else "⏳"))
                            _b_wl   = _b.get("winst_verlies",0)
                            _b_wl_s = "⚪ Void" if _b_uit == "void" else (f"€{_b_wl:+.2f}" if _b_uit not in ("open",) else "—")
                            _b_inzet_val = _b.get("inzet")
                            _b_odds_val  = _b.get("odds")
                            if _b_inzet_val is not None and _b_odds_val is not None:
                                _b_te_winnen_s = f"€{round(float(_b_inzet_val) * (float(_b_odds_val) - 1), 2):.2f}"
                            else:
                                _b_te_winnen_s = "—"
                            _bc1, _bc2, _bc2b, _bc3, _bc4, _bc5, _bc6, _bc7 = st.columns([3, 0.85, 0.85, 0.85, 0.85, 0.85, 0.4, 0.4])
                            # Hoofdtitel: speler + (voor niet-parlays) bet-omschrijving.
                            # Voor parlays laten we de legs in de expander eronder zien.
                            _b_is_parlay = str(_b_id).startswith("parlay_")
                            _speler_disp = str(_b.get('speler','') or '')
                            _bet_disp    = str(_b.get('bet','') or '')
                            # Team-suffix via de centrale helper (parlay altijd leeg)
                            _team_suffix = "" if _b_is_parlay else _team_caption_suffix(_b)
                            if _b_is_parlay:
                                _bc1.write(f"{_b_icon} **{_speler_disp}**{_team_suffix}")
                            else:
                                _bc1.write(f"{_b_icon} **{_speler_disp}** — {_bet_disp}{_team_suffix}")
                            _bc2.write(f"@ {_b.get('odds','—')}")
                            _bc2b.write(_b_te_winnen_s)
                            _bc3.write(f"€{_b.get('inzet',0):.2f}")
                            _bc4.write(_b_wl_s)
                            _bc5.caption(_b.get("datum",""))
                            if _bc6.button("✏️", key=f"gpedit_{_b_id}", help="Bewerk weddenschap"):
                                st.session_state.gp_editing = _b_id
                                st.rerun()
                            if _bc7.button("🗑️", key=f"gpdel_{_b_id}", help="Verwijder weddenschap"):
                                if _b_is_parlay:
                                    _orig_prl_id = str(_b_id)[len("parlay_"):]
                                    if _b.get("uitkomst") == "open":
                                        # Open parlay staat niet in resultaten → verwijder uit parlays tabel
                                        db.delete_parlay(_orig_prl_id)
                                    else:
                                        # Gesettled parlay staat in resultaten én parlays tabel
                                        db.remove_resultaat(_b_id)
                                        db.update_parlay(_orig_prl_id, {"uitkomst": "open", "winst_verlies": 0.0})
                                else:
                                    db.remove_resultaat(_b_id)
                                st.rerun()

                            # ── Parlay legs: uitklapbaar per parlay-rij ────
                            if _b_is_parlay:
                                _parlay_obj  = _gp_parlays_by_rid.get(_b_id)
                                _parlay_legs = (_parlay_obj or {}).get("props_json") or []
                                _leg_status  = (_parlay_obj or {}).get("legs_json") or {}
                                if _parlay_legs:
                                    _exp_label = (
                                        f"🎰 {len(_parlay_legs)} legs"
                                        f"  ·  gecomb. odds {_b.get('odds','—')}"
                                    )
                                    with st.expander(_exp_label, expanded=False):
                                        for _li, _lg in enumerate(_parlay_legs):
                                            _lg_player = str(_lg.get("player") or "")
                                            _lg_team   = str(_lg.get("team") or "")
                                            _lg_bt     = str(_lg.get("bet_type") or "")
                                            _lg_odds   = _lg.get("odds")
                                            _lg_odds_s = (f"@ {float(_lg_odds):.2f}"
                                                          if _lg_odds is not None else "@ —")
                                            # Status per leg: sleutel-conventies in de app
                                            # wisselen ("player_bettype" en "i_player_bettype").
                                            _lg_st = (
                                                _leg_status.get(f"{_li}_{_lg_player}_{_lg_bt}")
                                                or _leg_status.get(f"{_lg_player}_{_lg_bt}")
                                                or "open"
                                            )
                                            _lg_ico = ("✅" if _lg_st == "geraakt"
                                                       else "❌" if _lg_st == "gemist"
                                                       else "⚪" if _lg_st == "void"
                                                       else "⏳")
                                            # Team tonen als het niet al in speler/bet-type voorkomt
                                            _lg_team_suffix = ""
                                            if (_lg_team
                                                and _lg_team.lower() not in _lg_player.lower()
                                                and _lg_team.lower() not in _lg_bt.lower()):
                                                _lg_team_suffix = f"  ·  {_lg_team}"
                                            st.markdown(
                                                f"- {_lg_ico} **{_lg_player or '—'}** — "
                                                f"{_lg_bt or '—'}  {_lg_odds_s}"
                                                f"{_lg_team_suffix}"
                                            )
                                        _hk = _parlay_obj.get("hit_kans") if _parlay_obj else None
                                        _ev = _parlay_obj.get("ev_score") if _parlay_obj else None
                                        _foot = []
                                        if _hk is not None:
                                            _foot.append(f"Hit kans: {float(_hk)*100:.1f}%")
                                        if _ev is not None:
                                            _foot.append(f"EV: {float(_ev):+.3f}")
                                        if _foot:
                                            st.caption("  ·  ".join(_foot))
                                else:
                                    st.caption("ℹ️ Geen leg-details beschikbaar voor deze parlay.")

                            # ── Inline edit-formulier ──────────────────────
                            if st.session_state.gp_editing == _b_id:
                                _sport_opts_e = ["NHL", "NBA", "MLB", "Voetbal", "Overig", "Parlay"]
                                _cur_sport_e  = _b.get("sport", "Overig")
                                _sport_idx_e  = _sport_opts_e.index(_cur_sport_e) if _cur_sport_e in _sport_opts_e else 4
                                _uit_opts_e   = ["open", "gewonnen", "verloren", "void"]
                                _cur_uit_e    = _b.get("uitkomst", "open")
                                _uit_idx_e    = _uit_opts_e.index(_cur_uit_e) if _cur_uit_e in _uit_opts_e else 0
                                try:
                                    _cur_datum_e = datetime.date.fromisoformat(_b.get("datum","")[:10])
                                except Exception:
                                    _cur_datum_e = datetime.date.today()

                                with st.form(key=f"gp_edit_{_b_id}"):
                                    st.markdown("**✏️ Weddenschap wijzigen**")
                                    _ef1, _ef2 = st.columns(2)
                                    _e_speler = _ef1.text_input(
                                        "Speler / Team", value=_b.get("speler",""))
                                    _e_sport  = _ef2.selectbox(
                                        "Sport", _sport_opts_e, index=_sport_idx_e)
                                    _ef3, _ef4 = st.columns(2)
                                    _e_bet    = _ef3.text_input(
                                        "Bet omschrijving", value=_b.get("bet",""))
                                    _e_odds   = _ef4.number_input(
                                        "Odds", min_value=1.01, max_value=10_000.0,
                                        value=float(_b.get("odds") or 1.5),
                                        step=0.05, format="%.2f")
                                    _ef5, _ef6, _ef7 = st.columns(3)
                                    _e_inzet  = _ef5.number_input(
                                        "Inzet (€)", min_value=0.01, max_value=100_000.0,
                                        value=float(_b.get("inzet") or 10.0),
                                        step=1.0, format="%.2f")
                                    _e_uit    = _ef6.selectbox(
                                        "Uitkomst", _uit_opts_e, index=_uit_idx_e)
                                    _e_datum  = _ef7.date_input(
                                        "Datum", value=_cur_datum_e)
                                    _fcol1, _fcol2 = st.columns(2)
                                    _save_e   = _fcol1.form_submit_button(
                                        "💾 Opslaan", type="primary", use_container_width=True)
                                    _cancel_e = _fcol2.form_submit_button(
                                        "↩️ Annuleren", use_container_width=True)

                                if _save_e:
                                    _upd_fav = {
                                        "speler":        _e_speler,
                                        "player":        _e_speler,
                                        "bet":           _e_bet,
                                        "bet_type":      _e_bet,
                                        "sport":         _e_sport,
                                        "odds":          _e_odds,
                                        "datum":         _e_datum.isoformat(),
                                        "ev_score":      float(_b.get("ev_score") or 0),
                                        "rating":        _b.get("rating", ""),
                                        "composite":     float(_b.get("composite") or 0),
                                        "import_method": _b.get("import_method", ""),
                                        "bookmaker":     _b.get("bookmaker", ""),
                                    }
                                    db.upsert_resultaat(_b_id, _upd_fav, _e_uit, _e_inzet)
                                    if str(_b_id).startswith("parlay_"):
                                        _ep_id = str(_b_id)[len("parlay_"):]
                                        _ep_wl = (round(_e_inzet * (_e_odds - 1), 2)
                                                  if _e_uit == "gewonnen"
                                                  else round(-_e_inzet, 2)
                                                  if _e_uit == "verloren"
                                                  else 0.0)  # void en open: 0
                                        db.update_parlay(_ep_id, {
                                            "inzet":              float(_e_inzet),
                                            "gecombineerde_odds": float(_e_odds),
                                            "uitkomst":           _e_uit,
                                            "winst_verlies":      _ep_wl,
                                        })
                                    st.session_state.gp_editing = None
                                    st.rerun()
                                if _cancel_e:
                                    st.session_state.gp_editing = None
                                    st.rerun()


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
        st.markdown('<small style="color:#a0c4ff;">ℹ️ Nog geen analyses opgeslagen. Voer een analyse uit om de geschiedenis te vullen.</small>', unsafe_allow_html=True)
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
                        }, source_session_id=_sid, game_date=datetime.date.today().isoformat())
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
            st.markdown('<small style="color:#a0c4ff;">ℹ️ Geen analyses gevonden met deze filters.</small>', unsafe_allow_html=True)
