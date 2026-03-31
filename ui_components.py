"""
ui_components.py — BetAnalyzer UI render-functies
Alle Streamlit-afhankelijke weergave-logica.
"""

import streamlit as st
import db

SPORT_ICONS = {"NHL": "🏒", "NBA": "🏀", "MLB": "⚾"}


def _rating_color(rat: str) -> str:
    if "Sterk" in rat:
        return "green"
    if "Matig" in rat:
        return "orange"
    return "red"


# ─── Gedeelde match-weergave helpers ─────────────────────────────────────────

def render_option_box(col, opt: dict, best: dict):
    """Toont één wedstrijdoptie (thuis/gelijkspel/uit) als gekleurde box."""
    with col:
        ev_val  = opt["ev"]
        ev_str  = f"+{ev_val:.3f}" if ev_val and ev_val >= 0 else (f"{ev_val:.3f}" if ev_val is not None else "—")
        rat     = opt["rating"]
        is_best = best and opt["label"] == best["label"] and ev_val is not None and ev_val >= 0
        bc = "#4ade80" if "Waarde" in rat else ("#facc15" if "Neutraal" in rat else "#f87171")
        bg = "#081a10" if "Waarde" in rat else ("#1a1500" if "Neutraal" in rat else "#1a0808")
        st.markdown(
            f"<div style='background:{bg};border:1px solid {bc};"
            f"border-radius:8px;padding:10px;text-align:center;'>"
            f"<div style='font-size:0.75rem;color:#a0a0c8;margin-bottom:4px'>"
            f"{opt['label']}{'  ⭐' if is_best else ''}</div>"
            f"<div style='font-size:1.1rem;font-weight:700;color:#fff'>"
            f"Odds: {'{:.2f}'.format(opt['odds']) if opt['odds'] else '—'}</div>"
            f"<div style='color:#a0a0c8;font-size:0.85rem'>Model: {opt['prob']*100:.1f}%</div>"
            f"<div style='font-size:1.0rem;font-weight:700;color:{bc}'>EV {ev_str}</div>"
            f"<div style='font-size:0.8rem;color:{bc}'>{rat}</div></div>",
            unsafe_allow_html=True,
        )


def render_fav_button(ma: dict, sport_label: str, bet_source: str):
    """Opslaan-knop voor wedstrijd (beste optie) als favoriet."""
    best = ma.get("best")
    if not (best and best.get("odds") and best.get("ev") is not None):
        return
    key = f"{sport_label[:3]}_{ma['home_team'][:4]}_{ma['away_team'][:4]}_{ma.get('time','')}"
    fav_bet = {
        "player":      f"{ma['home_team']} vs {ma['away_team']}",
        "sport":       sport_label,
        "team":        ma["home_team"][:3].upper(),
        "bet_type":    best["label"].replace("🏠 ", "").replace("✈️ ", "").replace("🤝 ", "").replace("🔄 ", ""),
        "odds":        best["odds"],
        "ev":          best["ev"],
        "rating":      best["rating"],
        "composite":   best["prob"],
        "linemate_hr": best["prob"],
        "season_hr":   best["prob"],
        "sample":      bet_source,
        "source":      bet_source,
    }
    fav_ids = {f["id"] for f in db.load_favorieten()}
    fid     = db.make_fav_id(fav_bet["player"], fav_bet["bet_type"])
    if fid not in fav_ids:
        if st.button("☆ Opslaan in Favorieten", key=f"fav_{key}", use_container_width=True):
            db.add_favoriet(fid, fav_bet)
            st.rerun()
    else:
        st.markdown("<div style='color:#4ade80;text-align:center;padding:4px'>"
                    "⭐ Opgeslagen in Favorieten</div>", unsafe_allow_html=True)


def _match_top3_header(match_analyses: list, sport_label: str):
    top3 = sorted(
        [ma for ma in match_analyses
         if ma.get("best") and ma["best"].get("ev") is not None and ma["best"]["ev"] > 0],
        key=lambda ma: ma["best"]["ev"], reverse=True
    )[:3]
    if not top3:
        return
    st.markdown(f"#### 🏆 Top 3 aanbevolen {sport_label} wedstrijden")
    for i, ma in enumerate(top3, 1):
        b   = ma["best"]
        ev_s = f"+{b['ev']:.3f}" if b["ev"] >= 0 else f"{b['ev']:.3f}"
        hf  = ma.get("home_form") or {}
        af  = ma.get("away_form") or {}
        extra = ""
        if sport_label == "NHL":
            extra = (f"Thuis L10: `{hf.get('last10','—')}` | Uit L10: `{af.get('last10','—')}`  \n"
                     f"Gem. goals: {hf.get('gf_avg',0):.2f} – {af.get('gf_avg',0):.2f}")
        elif sport_label == "NBA":
            p = ma.get("probs", {})
            extra = (f"Verwachte marge: {p.get('exp_margin',0):+.1f} punten  \n"
                     f"L10 thuis: `{hf.get('last10','—')}` | L10 uit: `{af.get('last10','—')}`")
        elif sport_label == "MLB":
            p = ma.get("probs", {})
            extra = f"xRuns: {p.get('lH',0):.2f} – {p.get('lA',0):.2f}"
        elif sport_label == "Voetbal":
            p = ma.get("probs", {})
            extra = (f"xG: {p.get('lH',0):.2f} – {p.get('lA',0):.2f}  \n"
                     f"Form thuis: `{hf.get('form','—')}` | Form uit: `{af.get('form','—')}`")
        st.markdown(
            f"**{i}. {ma['home_team']} vs {ma['away_team']}**  \n"
            f"Beste inzet: {b['label']} · EV `{ev_s}` · {b['rating']}  \n"
            f"{extra}"
        )
    st.markdown("---")


def _match_card_header(home: str, away: str, time_str: str, icon: str, comp: str = ""):
    hcol, tcol = st.columns([4, 1])
    with hcol:
        title = f"#### {icon} {home}  vs  {away}"
        if comp:
            title += f"  <small style='color:#8888b8'> {comp}</small>"
        st.markdown(title, unsafe_allow_html=True)
    with tcol:
        if time_str:
            st.markdown(f"<div style='text-align:right;color:#a0a0c8;padding-top:8px'>⏰ {time_str}</div>",
                        unsafe_allow_html=True)


# ─── NHL ─────────────────────────────────────────────────────────────────────

def render_nhl_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### 🏒 NHL Wedstrijd-analyse")
    _match_top3_header(match_analyses, "NHL")

    for ma in match_analyses:
        home  = ma["home_team"]
        away  = ma["away_team"]
        probs = ma.get("probs", {})
        best  = ma.get("best")
        hf    = ma.get("home_form") or {}
        af    = ma.get("away_form") or {}
        odds_src = ma.get("odds_bron", "")

        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>",
                        unsafe_allow_html=True)
            _match_card_header(home, away, ma.get("time", ""), "🏒")

            if hf or af:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if hf:
                        st.metric(f"🏠 {hf.get('abbrev', home[:3])}", "")
                        st.caption(f"Punten: {hf.get('points',0)} ({hf.get('points_pct',0):.1%})  \n"
                                   f"L10: {hf.get('last10','—')}  \n"
                                   f"Reeks: {hf.get('streak','—')}  \nThuis: {hf.get('home_record','—')}")
                with c2:
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#a0a0c8;'>"
                                f"<div>xGoals: {probs.get('lH',0):.2f} – {probs.get('lA',0):.2f}</div></div>",
                                unsafe_allow_html=True)
                with c3:
                    if af:
                        st.metric(f"✈️ {af.get('abbrev', away[:3])}", "")
                        st.caption(f"Punten: {af.get('points',0)} ({af.get('points_pct',0):.1%})  \n"
                                   f"L10: {af.get('last10','—')}  \n"
                                   f"Reeks: {af.get('streak','—')}  \nUit: {af.get('road_record','—')}")

            st.markdown("---")
            opt_cols = st.columns(3)
            for col, opt in zip(opt_cols, ma["options"]):
                render_option_box(col, opt, best)
            st.markdown(f"<div style='color:#8888b8;font-size:0.75rem;margin-top:8px;'>"
                        f"Odds bron: {odds_src}  ·  "
                        f"Model: Poisson (xG {probs.get('lH',0):.2f}–{probs.get('lA',0):.2f})</div>",
                        unsafe_allow_html=True)
            render_fav_button(ma, "NHL", "NHL Standings")
            st.markdown("</div>", unsafe_allow_html=True)


# ─── Soccer ───────────────────────────────────────────────────────────────────

def render_soccer_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### ⚽ Voetbal Wedstrijd-analyse")
    _match_top3_header(match_analyses, "Voetbal")

    for ma in match_analyses:
        home  = ma["home_team"]
        away  = ma["away_team"]
        probs = ma.get("probs", {})
        best  = ma.get("best")
        hf    = ma.get("home_form") or {}
        af    = ma.get("away_form") or {}

        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>",
                        unsafe_allow_html=True)
            _match_card_header(home, away, ma.get("time", ""), "⚽", comp=ma.get("competition", ""))

            if hf or af:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if hf:
                        st.metric(f"🏠 {hf.get('abbrev', home[:3])}", "")
                        st.caption(f"GF avg: {hf.get('gf_avg',0):.2f}\n"
                                   f"GA avg: {hf.get('ga_avg',0):.2f}\nForm: {hf.get('form','—')}")
                with c2:
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#a0a0c8;'>"
                                f"<div>xG: {probs.get('lH',0):.2f} – {probs.get('lA',0):.2f}</div></div>",
                                unsafe_allow_html=True)
                with c3:
                    if af:
                        st.metric(f"✈️ {af.get('abbrev', away[:3])}", "")
                        st.caption(f"GF avg: {af.get('gf_avg',0):.2f}\n"
                                   f"GA avg: {af.get('ga_avg',0):.2f}\nForm: {af.get('form','—')}")

            st.markdown("---")
            opt_cols = st.columns(3)
            for col, opt in zip(opt_cols, ma["options"]):
                render_option_box(col, opt, best)
            st.markdown(f"<div style='color:#8888b8;font-size:0.75rem;margin-top:8px;'>"
                        f"Odds bron: {ma.get('odds_bron','')}  ·  "
                        f"xG {probs.get('lH',0):.2f}–{probs.get('lA',0):.2f}</div>",
                        unsafe_allow_html=True)
            render_fav_button(ma, "Soccer", "Football-data.org")
            st.markdown("</div>", unsafe_allow_html=True)


# ─── NBA ──────────────────────────────────────────────────────────────────────

def render_nba_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### 🏀 NBA Wedstrijd-analyse")
    _match_top3_header(match_analyses, "NBA")

    for ma in match_analyses:
        home  = ma["home_team"]
        away  = ma["away_team"]
        probs = ma.get("probs", {})
        best  = ma.get("best")
        hf    = ma.get("home_form") or {}
        af    = ma.get("away_form") or {}

        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>",
                        unsafe_allow_html=True)
            _match_card_header(home, away, ma.get("time", ""), "🏀")

            if hf or af:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if hf:
                        st.metric(f"🏠 {hf.get('abbrev', home[:3])}", "")
                        st.caption(f"Record: {hf.get('wins',0)}-{hf.get('losses',0)}\n"
                                   f"L10: {hf.get('last10','—')}\nReeks: {hf.get('streak','—')}\n"
                                   f"Thuis: {hf.get('home_record','—')}")
                with c2:
                    margin = probs.get("exp_margin", 0)
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#a0a0c8;'>"
                                f"<div>Verwachte marge:</div>"
                                f"<div style='font-size:1.2rem;font-weight:700;color:#fff'>{margin:+.1f} pts</div>"
                                f"<div style='font-size:0.85rem;margin-top:4px'>"
                                f"{probs.get('exp_home_pts',0):.0f} – {probs.get('exp_away_pts',0):.0f}</div>"
                                f"</div>", unsafe_allow_html=True)
                with c3:
                    if af:
                        st.metric(f"✈️ {af.get('abbrev', away[:3])}", "")
                        st.caption(f"Record: {af.get('wins',0)}-{af.get('losses',0)}\n"
                                   f"L10: {af.get('last10','—')}\nReeks: {af.get('streak','—')}\n"
                                   f"Uit: {af.get('road_record','—')}")

            st.markdown("---")
            opt_cols = st.columns(min(len(ma["options"]), 4))
            for col, opt in zip(opt_cols, ma["options"]):
                render_option_box(col, opt, best)
            st.markdown(f"<div style='color:#8888b8;font-size:0.75rem;margin-top:8px;'>"
                        f"Odds bron: {ma.get('odds_bron','')}  ·  "
                        f"Marge model: {probs.get('exp_margin',0):+.1f} punten</div>",
                        unsafe_allow_html=True)
            render_fav_button(ma, "NBA", "NBA API")
            st.markdown("</div>", unsafe_allow_html=True)


# ─── MLB ──────────────────────────────────────────────────────────────────────

def render_mlb_match_cards(match_analyses: list):
    if not match_analyses:
        return
    st.markdown("---")
    st.markdown("### ⚾ MLB Wedstrijd-analyse")
    _match_top3_header(match_analyses, "MLB")

    for ma in match_analyses:
        home  = ma["home_team"]
        away  = ma["away_team"]
        probs = ma.get("probs", {})
        best  = ma.get("best")
        hf    = ma.get("home_form") or {}
        af    = ma.get("away_form") or {}
        hp    = ma.get("home_pitcher") or {}   # startende werper thuis
        ap    = ma.get("away_pitcher") or {}   # startende werper uit

        with st.container():
            st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                        "border-radius:12px;padding:16px;margin-bottom:14px;'>",
                        unsafe_allow_html=True)
            _match_card_header(home, away, ma.get("time", ""), "⚾")

            # ── Startende werpers ──────────────────────────────────────────────
            if hp or ap:
                def _pitcher_label(p: dict) -> str:
                    if not p:
                        return "onbekend"
                    name = p.get("name", "?")
                    if not p.get("era"):
                        return f"**{name}**"
                    # ERA: geblend getal + (huidig / vorig) voor context
                    era_str = f"ERA {p['era']:.2f}"
                    if p.get("era_current") and p.get("era_prev"):
                        era_str += f" ({p['era_current']:.2f} / {p['era_prev']:.2f})"
                    parts = [
                        era_str,
                        f"WHIP {p['whip']:.2f}"   if p.get("whip")     else "",
                        f"K/9 {p['k_per_9']:.1f}" if p.get("k_per_9") else "",
                        f"H/9 {p['hits_per_9']:.1f}" if p.get("hits_per_9") else "",
                        f"BB/9 {p['bb_per_9']:.1f}"  if p.get("bb_per_9")  else "",
                    ]
                    stats = "  ·  ".join(s for s in parts if s)
                    return f"**{name}**  —  {stats}"

                pc1, pc2 = st.columns(2)
                pc1.markdown(f"🏠 {_pitcher_label(hp)}")
                pc2.markdown(f"✈️ {_pitcher_label(ap)}")

            # ── Team statistieken ──────────────────────────────────────────────
            if hf or af:
                c1, c2, c3 = st.columns(3)
                with c1:
                    if hf:
                        st.metric(f"🏠 {hf.get('abbrev', home[:3])}", "")
                        st.caption(f"Record: {hf.get('wins',0)}-{hf.get('losses',0)}\n"
                                   f"Runs avg: {hf.get('runs_avg',0):.2f}\n"
                                   f"Thuis: {hf.get('home_record','—')}")
                with c2:
                    st.markdown(f"<div style='text-align:center;padding-top:12px;color:#a0a0c8;'>"
                                f"<div>xRuns: {probs.get('lH',0):.2f} – {probs.get('lA',0):.2f}</div>"
                                f"<div style='font-size:0.85rem;margin-top:4px'>"
                                f"Run line: ±{ma.get('run_line',1.5):.1f}</div></div>",
                                unsafe_allow_html=True)
                with c3:
                    if af:
                        st.metric(f"✈️ {af.get('abbrev', away[:3])}", "")
                        st.caption(f"Record: {af.get('wins',0)}-{af.get('losses',0)}\n"
                                   f"Runs avg: {af.get('runs_avg',0):.2f}\n"
                                   f"Uit: {af.get('road_record','—')}")

            st.markdown("---")
            opt_cols = st.columns(min(len(ma["options"]), 4))
            for col, opt in zip(opt_cols, ma["options"]):
                render_option_box(col, opt, best)

            # Voetnoot: ERA-correctie melding indien pitcher bekend
            _src = f"Odds bron: {ma.get('odds_bron','')}  ·  xRuns {probs.get('lH',0):.2f}–{probs.get('lA',0):.2f}"
            if hp or ap:
                _src += "  ·  model incl. pitcher ERA"
            st.markdown(f"<div style='color:#8888b8;font-size:0.75rem;margin-top:8px;'>{_src}</div>",
                        unsafe_allow_html=True)
            render_fav_button(ma, "MLB", "MLB API")
            st.markdown("</div>", unsafe_allow_html=True)


# ─── Flashscore tekst ────────────────────────────────────────────────────────

def render_flashscore(text: str):
    st.markdown("---")
    st.markdown("### 📺 Flashscore Analyse")
    st.markdown(text)


# ─── Prop kaarten ─────────────────────────────────────────────────────────────

def render_top3(top3: list):
    st.markdown("### 🏆 Top prop aanbevelingen")
    for i, b in enumerate(top3, 1):
        ev_str = f"+{b['ev']:.3f}" if b['ev'] >= 0 else f"{b['ev']:.3f}"
        st.markdown(
            f"**{i}. {b['player']}** · {b['bet_type']} @ {b['odds']}  "
            f"&nbsp;&nbsp; EV `{ev_str}`"
        )


def render_bet_card(bet: dict, rank: int, total: int, is_fav: bool = False, session_id: str = ""):
    sport_icon    = SPORT_ICONS.get(bet["sport"].upper(), "⚽")
    ev_str        = f"+{bet['ev']:.3f}" if bet["ev"] >= 0 else f"{bet['ev']:.3f}"
    composite_pct = int(bet["composite"] * 100)
    rat_color     = _rating_color(bet["rating"])

    with st.container():
        st.markdown("<div style='background:#11112b;border:1px solid #2a2a58;"
                    "border-radius:12px;padding:16px;margin-bottom:12px;'>",
                    unsafe_allow_html=True)
        col_l, col_r = st.columns([3, 1])
        with col_l:
            st.markdown(f"**{sport_icon} #{rank} van {total}**")
        with col_r:
            st.markdown(f"<span style='color:{rat_color};font-weight:700;'>{bet['rating']}</span>",
                        unsafe_allow_html=True)
        st.markdown(f"#### {bet['player']}")

        if bet.get("_ev_penalty_note"):
            st.warning(bet["_ev_penalty_note"])
        if bet.get("_sample_warning"):
            st.warning(bet["_sample_warning"])

        b365_label   = bet.get("bet365", {}).get("label", "")
        caption_line = f"{bet['bet_type']} · {bet['sport']}"
        if b365_label:
            caption_line += f"  ·  {b365_label}"
        st.caption(caption_line)

        ev_color = "#4ade80" if bet["ev"] >= 0.05 else "#facc15"
        st.markdown(f"<span style='color:{ev_color};font-size:1.4rem;font-weight:800;'>EV {ev_str}</span>",
                    unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Linemate HR", f"{bet['linemate_hr']*100:.1f}%")
        c2.metric("Seizoens HR", f"{bet['season_hr']*100:.1f}%")
        c3.metric("Odds",        f"{bet['odds']}")
        c4.metric("Sample",      bet["sample"])

        st.progress(bet["composite"], text=f"Composite: {composite_pct}%")

        if bet.get("no_linemate_hr"):
            st.warning("⚠️ **Onvoldoende data** — Linemate hit rate niet gevonden in screenshot. "
                       "EV is uitsluitend gebaseerd op historische statistieken.")

        info_parts = []
        if bet.get("opponent"):
            info_parts.append(f"vs {bet['opponent']}")
        if bet.get("gaa"):
            info_parts.append(f"GAA {bet['gaa']}")
        if bet.get("source"):
            info_parts.append(f"Bron: {bet['source']}")
        if info_parts:
            st.caption(" · ".join(info_parts))

        # Odds aanpassen
        _adj_key     = "adj_" + db.make_fav_id(bet["player"], bet["bet_type"])
        _stored_odds = st.session_state.get(_adj_key)
        _display     = _stored_odds if _stored_odds is not None else float(bet["odds"])

        with st.expander("📝 Odds aangepast op Bet365?"):
            _inp_key      = f"odds_inp_{rank}_{total}"
            _new_odds_inp = st.number_input(
                "Nieuwe odds", min_value=1.01, max_value=50.0,
                value=_display, step=0.01, format="%.2f", key=_inp_key,
            )
            if st.button("Herbereken EV", key=f"recalc_{rank}_{total}"):
                st.session_state[_adj_key] = float(st.session_state.get(_inp_key, _new_odds_inp))
                st.rerun()
            _eff_odds = st.session_state.get(_adj_key)
            if _eff_odds is not None and abs(_eff_odds - float(bet["odds"])) > 0.001:
                _composite = bet.get("composite", 0.5)
                _orig_ev   = bet["ev"]
                _new_ev    = _composite * (_eff_odds - 1) - (1 - _composite)
                _diff      = _new_ev - _orig_ev
                _o_str     = f"+{_orig_ev:.3f}" if _orig_ev >= 0 else f"{_orig_ev:.3f}"
                _n_str     = f"+{_new_ev:.3f}"  if _new_ev  >= 0 else f"{_new_ev:.3f}"
                _reden     = "hogere" if _diff >= 0 else "lagere"
                st.caption(f"EV: **{_o_str}** → **{_n_str}** ({_diff:+.3f} door {_reden} odds)")
                if _new_ev < 0:
                    st.error("❌ Weddenschap niet meer interessant bij deze odds")
                else:
                    st.success("✅ Nog steeds interessant")

        # Favoriet knop
        _fav_label = "⭐ Verwijder uit Shortlist" if is_fav else "⭐ Voeg toe aan Shortlist"
        fid = db.make_fav_id(bet["player"], bet["bet_type"])
        if st.button(_fav_label, key=f"fav_{rank}_{total}", use_container_width=False):
            if is_fav:
                db.remove_favoriet(fid)
            else:
                _fav_adj = st.session_state.get(_adj_key)
                if _fav_adj is not None and abs(_fav_adj - float(bet["odds"])) > 0.001:
                    _composite = bet.get("composite", 0.5)
                    _adj_ev    = _composite * (_fav_adj - 1) - (1 - _composite)
                    db.add_favoriet(fid, {**bet, "odds": _fav_adj, "ev": _adj_ev}, source_session_id=session_id)
                else:
                    db.add_favoriet(fid, bet, source_session_id=session_id)
            st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)
