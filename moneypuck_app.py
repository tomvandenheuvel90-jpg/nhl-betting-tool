"""
Moneypuck NHL Stats Explorer
Start: streamlit run moneypuck_app.py
"""

import streamlit as st
import pandas as pd
from pathlib import Path

# ─── CONFIG ───────────────────────────────────────────────────────────────────

PASSWORD = "jullie_wachtwoord"
DATA_DIR = Path(__file__).parent / "moneypuck_data" / "raw"

DISPLAY_COLS = {
    "name":                    "Speler",
    "team":                    "Team",
    "season":                  "Seizoen",
    "games_played":            "GP",
    "I_F_goals":               "Goals",
    "I_F_assists":             "Assists",
    "I_F_points":              "Punten",
    "I_F_shotsOnGoal":         "Shots",
    "I_F_blockedShotAttempts": "Blocks",
    "I_F_hits":                "Hits",
    "icetime":                 "Icetime",
    "I_F_xGoals":              "xGoals",
    "I_F_highDangerGoals":     "HD Goals",
    "onIce_xGoalsPercentage":  "xG%",
    "gameScore":               "GScore",
}

NUMERIC_COLS = [
    "games_played", "I_F_goals", "I_F_assists", "I_F_points",
    "I_F_shotsOnGoal", "I_F_blockedShotAttempts", "I_F_hits",
    "I_F_xGoals", "I_F_highDangerGoals", "onIce_xGoalsPercentage", "gameScore",
]

# Kolommen die we bewaren uit de ruwe CSV (100+ kolommen)
RAW_KEEP = list(DISPLAY_COLS.keys()) + [
    "position", "situation",
    "I_F_primaryAssists", "I_F_secondaryAssists",
]

# ─── DATA LADEN ───────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Data laden...")
def load_data() -> dict[str, pd.DataFrame]:
    """Laad alle skater CSVs, return {'regular': df, 'playoffs': df}"""
    frames = {"regular": [], "playoffs": []}

    for season_type in ("regular", "playoffs"):
        type_dir = DATA_DIR / season_type
        if not type_dir.exists():
            continue
        for year_dir in sorted(type_dir.iterdir()):
            csv_path = year_dir / "skaters.csv"
            if not csv_path.exists():
                continue
            try:
                df = pd.read_csv(csv_path, low_memory=False)
                # Bereken I_F_assists als die ontbreekt
                if "I_F_assists" not in df.columns:
                    df["I_F_assists"] = (
                        pd.to_numeric(df.get("I_F_primaryAssists", 0), errors="coerce").fillna(0)
                        + pd.to_numeric(df.get("I_F_secondaryAssists", 0), errors="coerce").fillna(0)
                    ).astype(int)
                # Bewaar alleen relevante kolommen (wat aanwezig is)
                keep = [c for c in RAW_KEEP if c in df.columns]
                df = df[keep].copy()
                # Numeriek maken
                for col in NUMERIC_COLS:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                frames[season_type].append(df)
            except Exception:
                continue

    result = {}
    for k, dfs in frames.items():
        if dfs:
            result[k] = pd.concat(dfs, ignore_index=True)
        else:
            result[k] = pd.DataFrame()
    return result


def icetime_to_minutes(val) -> float:
    """Zet MM:SS of seconden terug naar decimale minuten voor sorteren."""
    try:
        s = str(val)
        if ":" in s:
            parts = s.split(":")
            return int(parts[0]) + int(parts[1]) / 60
        return float(s) / 60
    except Exception:
        return 0.0


def fmt_pct(val) -> str:
    try:
        return f"{float(val)*100:.1f}%"
    except Exception:
        return str(val)


# ─── WACHTWOORD ───────────────────────────────────────────────────────────────

def check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True
    st.markdown("## NHL Stats Explorer")
    pwd = st.text_input("Wachtwoord", type="password", key="pwd_input")
    if st.button("Inloggen"):
        if pwd == PASSWORD:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Onjuist wachtwoord.")
    return False


# ─── PLAYER CARD ──────────────────────────────────────────────────────────────

def render_stat_row(label: str, reg_val, play_val):
    col1, col2, col3 = st.columns([2, 1, 1])
    col1.markdown(f"**{label}**")
    col2.markdown(str(reg_val) if reg_val is not None else "—")
    col3.markdown(str(play_val) if play_val is not None else "—")


def render_player_comparison(player_name: str, season: str, situation: str, data: dict):
    """Toon regular vs playoffs naast elkaar voor één speler."""

    def get_row(df_type):
        df = data.get(df_type, pd.DataFrame())
        if df.empty:
            return None
        mask = df["name"].str.lower() == player_name.lower()
        if "situation" in df.columns:
            mask &= df["situation"] == situation
        if season != "Alle seizoenen" and "season" in df.columns:
            mask &= df["season"].astype(str) == season
        sub = df[mask]
        if sub.empty:
            return None
        # Per seizoen aggregeren (sommeer numerieke kolommen)
        num_cols = [c for c in NUMERIC_COLS if c in sub.columns]
        agg = {c: "sum" for c in num_cols}
        agg["icetime"] = lambda x: _sum_icetime(x)
        agg["team"] = "last"
        agg["games_played"] = "sum"
        try:
            result = sub.groupby("season").agg(agg).reset_index()
            result = result.sort_values("season", ascending=False)
        except Exception:
            result = sub
        return result

    def _sum_icetime(series):
        total = sum(icetime_to_minutes(v) for v in series)
        h = int(total) // 60
        m = int(total) % 60
        return f"{h}:{m:02d}" if h else f"{int(total)}:{int((total % 1)*60):02d}"

    reg = get_row("regular")
    play = get_row("playoffs")

    if reg is None and play is None:
        st.warning(f"Geen data gevonden voor **{player_name}** met de huidige filters.")
        return

    # Header
    st.markdown(f"### {player_name}")
    if reg is not None and "team" in reg.columns:
        st.caption(f"Team: {reg['team'].iloc[0]}")

    # Seizoen-voor-seizoen tabel
    st.markdown("#### Seizoen-overzicht")

    col_reg, col_play = st.columns(2)

    rename_map = {k: v for k, v in DISPLAY_COLS.items() if k != "name" and k != "team"}

    with col_reg:
        st.markdown("**Regular Season**")
        if reg is not None:
            show = reg.rename(columns=rename_map)
            show_cols = [c for c in ["season", "GP", "Goals", "Assists", "Punten",
                                      "Shots", "Blocks", "Hits", "Icetime",
                                      "xGoals", "HD Goals", "xG%", "GScore"] if c in show.columns]
            if "xG%" in show.columns:
                show["xG%"] = show["xG%"].apply(fmt_pct)
            if "GScore" in show.columns:
                show["GScore"] = show["GScore"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
            st.dataframe(show[show_cols], hide_index=True, use_container_width=True)
        else:
            st.info("Geen regular season data.")

    with col_play:
        st.markdown("**Playoffs**")
        if play is not None:
            show = play.rename(columns=rename_map)
            show_cols = [c for c in ["season", "GP", "Goals", "Assists", "Punten",
                                      "Shots", "Blocks", "Hits", "Icetime",
                                      "xGoals", "HD Goals", "xG%", "GScore"] if c in show.columns]
            if "xG%" in show.columns:
                show["xG%"] = show["xG%"].apply(fmt_pct)
            if "GScore" in show.columns:
                show["GScore"] = show["GScore"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
            st.dataframe(show[show_cols], hide_index=True, use_container_width=True)
        else:
            st.info("Geen playoffs data.")

    # Carrière-totalen (regular)
    if reg is not None and len(reg) > 1:
        st.markdown("#### Totalen (regular season)")
        num_cols = [c for c in NUMERIC_COLS if c in reg.columns]
        totals = reg[num_cols].sum()
        tot_df = pd.DataFrame({"Stat": num_cols, "Totaal": totals.values})
        rename_col = {k: v for k, v in DISPLAY_COLS.items()}
        tot_df["Stat"] = tot_df["Stat"].map(rename_col).fillna(tot_df["Stat"])
        st.dataframe(tot_df.set_index("Stat").T, use_container_width=True)


# ─── RANGLIJST ────────────────────────────────────────────────────────────────

def render_leaderboard(season_type: str, season: str, situation: str,
                        sort_col: str, position: str, data: dict, top_n: int = 25):
    df = data.get(season_type, pd.DataFrame())
    if df.empty:
        st.info("Geen data beschikbaar.")
        return

    mask = pd.Series([True] * len(df))
    if "situation" in df.columns:
        mask &= df["situation"] == situation
    if season != "Alle seizoenen" and "season" in df.columns:
        mask &= df["season"].astype(str) == season
    if position != "Alle posities" and "position" in df.columns:
        mask &= df["position"] == position

    sub = df[mask].copy()
    if sub.empty:
        st.info("Geen data met deze filters.")
        return

    # Aggregeer per speler over seizoenen
    num_cols = [c for c in NUMERIC_COLS if c in sub.columns]
    agg = {c: "sum" for c in num_cols}
    agg["team"] = "last"
    if "position" in sub.columns:
        agg["position"] = "last"

    try:
        grouped = sub.groupby("name").agg(agg).reset_index()
    except Exception:
        grouped = sub

    if sort_col not in grouped.columns:
        sort_col = "I_F_points"
    grouped = grouped.sort_values(sort_col, ascending=False).head(top_n)

    # Rename en format
    show = grouped.rename(columns=DISPLAY_COLS)
    show_order = ["Speler", "Team", "GP", "Goals", "Assists", "Punten",
                  "Shots", "Blocks", "Hits", "xGoals", "HD Goals", "xG%", "GScore"]
    show_order = [c for c in show_order if c in show.columns]

    if "xG%" in show.columns:
        show["xG%"] = show["xG%"].apply(fmt_pct)
    if "GScore" in show.columns:
        show["GScore"] = show["GScore"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")

    for col in ["GP", "Goals", "Assists", "Punten", "Shots", "Blocks", "Hits"]:
        if col in show.columns:
            show[col] = show[col].apply(lambda x: int(x) if pd.notna(x) else 0)

    st.dataframe(show[show_order], hide_index=True, use_container_width=True)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="NHL Stats — Moneypuck",
        page_icon="🏒",
        layout="wide",
    )

    if not check_password():
        return

    # Data laden
    data = load_data()

    # Beschikbare seizoenen bepalen
    all_seasons = set()
    for df in data.values():
        if not df.empty and "season" in df.columns:
            all_seasons.update(df["season"].astype(str).unique())
    seasons_sorted = sorted(all_seasons, reverse=True)

    # ── Sidebar ──
    with st.sidebar:
        st.markdown("## 🏒 NHL Stats")
        st.markdown("---")

        season = st.selectbox(
            "Seizoen",
            ["Alle seizoenen"] + seasons_sorted,
            index=1,
        )
        situation = st.selectbox(
            "Situatie",
            ["all", "5on5", "powerPlay", "penaltyKill"],
            index=0,
            format_func=lambda x: {
                "all": "All (totaal)",
                "5on5": "5-op-5",
                "powerPlay": "Power Play",
                "penaltyKill": "Penalty Kill",
            }.get(x, x),
        )
        position = st.selectbox(
            "Positie",
            ["Alle posities", "L", "R", "C", "D"],
            index=0,
        )
        st.markdown("---")
        if st.button("Cache wissen", use_container_width=True):
            load_data.clear()
            st.rerun()

        # Data-overzicht
        st.markdown("**Data beschikbaar:**")
        for stype, df in data.items():
            if not df.empty and "season" in df.columns:
                s_list = sorted(df["season"].astype(str).unique())
                st.caption(f"{stype.capitalize()}: {s_list[0]}–{s_list[-1]}")

    # ── Tabs ──
    tab_search, tab_rank = st.tabs(["🔍 Spelerskaart", "🏆 Ranglijst"])

    # ── Tab 1: Spelerskaart ──
    with tab_search:
        st.markdown("### Speler zoeken")

        # Autocomplete via dataframe
        all_names = set()
        for df in data.values():
            if not df.empty and "name" in df.columns:
                all_names.update(df["name"].dropna().unique())
        all_names_sorted = sorted(all_names)

        query = st.text_input("Naam (of deel van naam)", placeholder="bijv. MacKinnon")

        if query:
            matches = [n for n in all_names_sorted if query.lower() in n.lower()]
            if not matches:
                st.warning("Geen spelers gevonden.")
            elif len(matches) == 1:
                render_player_comparison(matches[0], season, situation, data)
            else:
                chosen = st.selectbox("Meerdere matches — kies speler:", matches)
                if chosen:
                    render_player_comparison(chosen, season, situation, data)

    # ── Tab 2: Ranglijst ──
    with tab_rank:
        st.markdown("### Ranglijst")

        col_type, col_sort, col_n = st.columns([1, 2, 1])
        with col_type:
            rank_type = st.selectbox("Type", ["regular", "playoffs"], key="rank_type")
        with col_sort:
            sort_display = st.selectbox(
                "Sorteren op",
                ["Punten", "Goals", "Assists", "Shots", "Blocks", "Hits", "xGoals", "HD Goals", "GScore"],
                key="sort_col",
            )
            sort_col_map = {v: k for k, v in DISPLAY_COLS.items()}
            sort_col = sort_col_map.get(sort_display, "I_F_points")
        with col_n:
            top_n = st.selectbox("Top", [10, 25, 50, 100], index=1, key="top_n")

        render_leaderboard(rank_type, season, situation, sort_col, position, data, top_n)


if __name__ == "__main__":
    main()
