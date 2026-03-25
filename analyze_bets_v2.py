#!/usr/bin/env python3
"""
Bet Analyzer v2 — Linemate screenshot + live seizoensstats (NHL/NBA/MLB/Voetbal)

Gebruik:
  python3 analyze_bets_v2.py screenshot1.png [screenshot2.png ...]
  python3 analyze_bets_v2.py *.png --bet365 "Speler:1.85" "Andere:2.10"

Vereist:
  pip install anthropic nba_api
  export ANTHROPIC_API_KEY='sk-ant-...'
  export FOOTBALL_DATA_API_KEY='...'   # alleen voor voetbal
"""

import sys
import os
import base64
import json
import argparse
import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Installeer eerst: pip install anthropic")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from sports import nhl, nba, mlb, soccer
from scorer import composite_score, ev, rating

EXTRACT_PROMPT = """
Je ziet één of meerdere screenshots van Linemate en/of Flashscore.

Geef een JSON object terug met twee arrays:

1. "bets": ALLE Linemate spelersprops. Elk item:
   - "player": naam (bijv. "S. Rinzel" of "Connor McDavid")
   - "sport": "NHL", "NBA", "MLB", "EPL", "La Liga", "Bundesliga", "Serie A" of "Ligue 1"
   - "team": teamafkorting indien zichtbaar (bijv. "CHI"), anders null
   - "bet_type": bijv. "Over 1.5 Shots on Goal" of "Anytime Goal Scorer"
   - "linemate_odds": odds als decimaal getal (number)
   - "hit_rate": percentage als decimaal: 100%→1.0, 92.3%→0.923 (number)
   - "sample": bijv. "12/13" (string)
   - "sample_n": totaal aantal wedstrijden als getal (number)

2. "matches": ALLE Flashscore wedstrijden. Elk item:
   - "home_team": naam thuisploeg (string)
   - "away_team": naam uitploeg (string)
   - "home_form": laatste 5 resultaten thuisploeg, bijv. "WWDLW" (W=win, D=draw, L=verlies), of null
   - "away_form": idem voor uitploeg, of null
   - "h2h": korte H2H samenvatting indien zichtbaar, bijv. "Arsenal won 3/5", of null
   - "competition": competitienaam (bijv. "Premier League"), of null
   - "date": datum indien zichtbaar (bijv. "2025-03-25"), of null
   - "status": "gepland", "bezig" of "afgelopen"
   - "score": score indien zichtbaar (bijv. "2-1"), of null

Als er geen Linemate screenshots zijn, geef dan een lege array voor "bets".
Als er geen Flashscore screenshots zijn, geef dan een lege array voor "matches".
Geef ALLEEN het JSON object terug, geen andere tekst.
"""

CACHE_FILE = Path(__file__).parent / ".stats_cache.json"

SOCCER_COMPS = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}


# ─── Cache (dagelijks, per speler) ────────────────────────────────────────────

def _load_daily_cache() -> dict:
    today = datetime.date.today().isoformat()
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if data.get("date") == today:
                return data.get("players", {})
        except Exception:
            pass
    return {}


def _save_daily_cache(players: dict):
    data = {"date": datetime.date.today().isoformat(), "players": players}
    CACHE_FILE.write_text(json.dumps(data))


# ─── Screenshots → bets ───────────────────────────────────────────────────────

def image_to_base64(path: str):
    ext = Path(path).suffix.lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}
    media_type = media_map.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), media_type


def extract_bets(client, image_paths: list) -> tuple:
    """Extraheer bets + wedstrijden uit screenshots in één API-call (haiku).
    Geeft (bets, matches) tuple terug."""
    print(f"📷  {len(image_paths)} screenshot(s) analyseren…")
    content = []
    for path in image_paths:
        img_data, media_type = image_to_base64(path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_data},
        })
    content.append({"type": "text", "text": EXTRACT_PROMPT})

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().strip("```")
    try:
        data = json.loads(raw)
        # Ondersteun zowel nieuw {bets, matches} formaat als oud array formaat
        if isinstance(data, list):
            return data, []
        return data.get("bets", []), data.get("matches", [])
    except json.JSONDecodeError:
        print("⚠️  JSON parse fout:", raw[:200])
        return [], []


FLASHSCORE_PROMPT = """
Je bent een expert sportsbetting analist. Analyseer de volgende wedstrijden en props in het Nederlands.

## WEDSTRIJDEN (Flashscore)
{matches_json}

## PROPS (Linemate — al gescoord)
{bets_json}

## STAP 2 — FLASHSCORE ANALYSE
Geef een scoretabel voor de wedstrijden:
| Wedstrijd | Thuis vorm | Uit vorm | H2H | Advies |
|---|---|---|---|---|

Daarna: **Top 3 wedstrijden** om op te focussen, met 1-zin uitleg per wedstrijd.

{combo_section}

## STAP 5 — TE VERMIJDEN
- Welke wedstrijden vermijd je en waarom? (max 3 bullets)
- Welke props vermijd je en waarom? (max 3 bullets)

## DISCLAIMER
Dit is een statistische analyse ter ondersteuning van je eigen beslissing. Wedden brengt financiële risico's met zich mee. Speel verantwoord.
"""

COMBO_SECTION = """## STAP 4 — COMBINATIE ADVIES
Koppel de beste props aan de beste wedstrijden:
- Welke speler props passen bij de aanbevolen wedstrijden?
- Geef een definitief **Top 3 advies** met onderbouwing (speler + wedstrijd + motivatie)
"""


def analyze_flashscore(client, matches: list, enriched_bets: list) -> str:
    """Claude-analyse van Flashscore wedstrijden + eventuele combinatie met props."""
    has_bets = bool(enriched_bets)

    # Vereenvoudigde bets voor de prompt (geen ruwe arrays)
    bets_summary = []
    for b in enriched_bets:
        bets_summary.append({
            "player":   b.get("player"),
            "sport":    b.get("sport"),
            "bet_type": b.get("bet_type"),
            "odds":     b.get("odds_used"),
            "ev":       b.get("ev"),
            "rating":   b.get("rating"),
            "composite": b.get("score", {}).get("composite"),
        })

    prompt = FLASHSCORE_PROMPT.format(
        matches_json=json.dumps(matches, ensure_ascii=False, indent=2),
        bets_json=json.dumps(bets_summary, ensure_ascii=False, indent=2) if has_bets else "(geen props meegegeven)",
        combo_section=COMBO_SECTION if has_bets else "",
    )

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ─── Bet verrijken met seizoensstats ──────────────────────────────────────────

def _enrich_nhl(player_name: str, cache: dict) -> dict:
    if player_name in cache:
        print(f"  💾 {player_name} (NHL) — uit cache")
        return cache[player_name]

    player_id, team = nhl.find_player(player_name)
    if not player_id:
        print(f"  ⚠️  {player_name} niet gevonden in NHL")
        return {}

    print(f"  🏒 {player_name} → NHL ID {player_id} ({team})")
    player_stats = nhl.get_player_stats(player_id)
    opponent     = nhl.get_opponent(team) if team else None
    opp_stats    = nhl.get_team_defense(opponent) if opponent else {}
    if opponent:
        print(f"     Tegenstander vandaag: {opponent}")

    entry = {
        "player_stats":  player_stats,
        "opponent":      opponent,
        "opponent_stats": opp_stats,
    }
    cache[player_name] = entry
    return entry


def _enrich_nba(player_name: str, cache: dict) -> dict:
    if player_name in cache:
        print(f"  💾 {player_name} (NBA) — uit cache")
        return cache[player_name]

    player = nba.find_player(player_name)
    if not player:
        print(f"  ⚠️  {player_name} niet gevonden in NBA")
        return {}

    player_id = player["id"]
    print(f"  🏀 {player_name} → NBA ID {player_id}")
    player_stats = nba.get_player_stats(player_id)

    entry = {"player_stats": player_stats, "opponent": None, "opponent_stats": {}}
    cache[player_name] = entry
    return entry


def _enrich_mlb(player_name: str, cache: dict) -> dict:
    if player_name in cache:
        print(f"  💾 {player_name} (MLB) — uit cache")
        return cache[player_name]

    player = mlb.find_player(player_name)
    if not player:
        print(f"  ⚠️  {player_name} niet gevonden in MLB")
        return {}

    player_id = player.get("id")
    print(f"  ⚾ {player_name} → MLB ID {player_id}")
    # Detecteer positie type (pitcher als primaryPosition code = "1")
    pos_code = player.get("primaryPosition", {}).get("code", "")
    pos_type = "pitching" if pos_code == "1" else "hitting"
    player_stats = mlb.get_player_stats(player_id, position_type=pos_type)

    entry = {"player_stats": player_stats, "opponent": None, "opponent_stats": {}}
    cache[player_name] = entry
    return entry


def _enrich_soccer(player_name: str, team_hint: str, competition: str, cache: dict) -> dict:
    cache_key = f"{player_name}_{competition}"
    if cache_key in cache:
        print(f"  💾 {player_name} (voetbal) — uit cache")
        return cache[cache_key]

    player = soccer.find_player(player_name, team_hint=team_hint or "", competition=competition)
    if not player:
        print(f"  ⚠️  {player_name} niet gevonden in voetbal ({competition})")
        return {}

    player_id = player.get("id")
    team_id   = player.get("team_id")
    print(f"  ⚽ {player_name} → ID {player_id} ({player.get('team_name', '')})")
    player_stats = soccer.get_player_stats(player_id, team_id, competition)
    opp_stats    = {}  # tegenstander data per wedstrijd vereist extra API calls

    entry = {"player_stats": player_stats, "opponent": None, "opponent_stats": opp_stats}
    cache[cache_key] = entry
    return entry


def enrich_bet(bet: dict, bet365_odds: dict, daily_cache: dict) -> dict:
    sport       = (bet.get("sport") or "").upper().strip()
    player_name = bet.get("player", "")
    team_hint   = bet.get("team") or ""
    bet_type    = bet.get("bet_type", "")
    sample_n    = bet.get("sample_n") or 5

    entry = {}

    if sport == "NHL":
        entry = _enrich_nhl(player_name, daily_cache)
    elif sport == "NBA":
        entry = _enrich_nba(player_name, daily_cache)
    elif sport == "MLB":
        entry = _enrich_mlb(player_name, daily_cache)
    elif sport in SOCCER_COMPS:
        entry = _enrich_soccer(player_name, team_hint, sport, daily_cache)
    else:
        print(f"  ℹ️  {player_name} ({sport}) — sport niet ondersteund")

    player_stats  = entry.get("player_stats", {})
    opponent_name = entry.get("opponent")
    opp_stats     = entry.get("opponent_stats", {})

    # Odds: Bet365 heeft prioriteit
    odds = bet365_odds.get(player_name) or bet.get("linemate_odds", 1.0)

    score   = composite_score(
        linemate_hit_rate=bet.get("hit_rate", 0.5),
        sample_size=sample_n,
        bet_type=bet_type,
        player_stats=player_stats,
        opponent_stats=opp_stats,
        sport=sport,
    )
    ev_val  = ev(score["composite"], odds)
    rat     = rating(ev_val, score["composite"])

    return {
        **bet,
        "odds_used":     odds,
        "sport_norm":    sport,
        "opponent":      opponent_name,
        "player_stats":  player_stats,
        "opponent_stats": opp_stats,
        "score":         score,
        "ev":            ev_val,
        "rating":        rat,
    }


# ─── Output formattering ──────────────────────────────────────────────────────

def _sport_icon(sport: str) -> str:
    icons = {"NHL": "🏒", "NBA": "🏀", "MLB": "⚾", "EPL": "⚽",
             "LA LIGA": "⚽", "BUNDESLIGA": "⚽", "SERIE A": "⚽", "LIGUE 1": "⚽"}
    return icons.get(sport.upper(), "🎯")


def _relevant_stats(bet_type: str, ps: dict, sport: str) -> list:
    """Selecteer relevante statistieken voor display."""
    bt    = bet_type.lower()
    lines = []

    if sport == "NHL":
        if ("shot" in bt or "sog" in bt) and "block" not in bt:
            raw = ps.get("raw_shots", [])
            lines += [
                f"Seizoen gem. shots: {ps.get('avg_shots', '?')}",
                f"Laatste {len(raw)} games: {', '.join(str(int(v)) for v in raw[:8])}{'…' if len(raw)>8 else ''}",
            ]
        elif "block" in bt:
            raw = ps.get("raw_blocks", [])
            lines += [
                f"Gem. blocks/game: {ps.get('avg_blocks', '?')}",
                f"Laatste {len(raw)} games: {', '.join(str(int(v)) for v in raw[:8])}{'…' if len(raw)>8 else ''}",
            ]
        elif "hit" in bt:
            raw = ps.get("raw_hits", [])
            lines += [
                f"Gem. hits/game: {ps.get('avg_hits', '?')}",
                f"Laatste {len(raw)} games: {', '.join(str(int(v)) for v in raw[:8])}{'…' if len(raw)>8 else ''}",
            ]
        elif "point" in bt or "assist" in bt or "goal" in bt or "scorer" in bt:
            lines += [
                f"Gem. doelpunten/game: {ps.get('avg_goals', '?')}",
                f"Gem. assists/game:    {ps.get('avg_assists', '?')}",
                f"Gem. punten/game:     {ps.get('avg_points', '?')}",
            ]
        if ps.get("corsi_pct"):
            lines.append(f"Corsi%: {ps['corsi_pct']:.1f}  xG/game: {ps.get('avg_xgoals', '?')}")

    elif sport == "NBA":
        lines += [
            f"Gem. pts/game:  {ps.get('avg_points', '?')}",
            f"Gem. ast/game:  {ps.get('avg_assists', '?')}",
            f"Gem. reb/game:  {ps.get('avg_rebounds', '?')}",
            f"Gem. 3PM/game:  {ps.get('avg_threes', '?')}",
        ]
        if "rebound" in bt:
            raw = ps.get("raw_reb", [])
            lines.append(f"Laatste {len(raw)} games (reb): {', '.join(str(int(v)) for v in raw[:8])}")
        elif "point" in bt or "pts" in bt:
            raw = ps.get("raw_pts", [])
            lines.append(f"Laatste {len(raw)} games (pts): {', '.join(str(int(v)) for v in raw[:8])}")

    elif sport == "MLB":
        pos = ps.get("position_type", "hitting")
        if pos == "pitching":
            lines += [
                f"Gem. strikeouts/game: {ps.get('avg_strikeouts', '?')}",
                f"Gem. innings:         {ps.get('avg_innings', '?')}",
            ]
        else:
            lines += [
                f"Gem. hits/game:        {ps.get('avg_hits', '?')}",
                f"Gem. total bases/game: {ps.get('avg_total_bases', '?')}",
                f"Gem. RBI/game:         {ps.get('avg_rbi', '?')}",
            ]

    elif sport in SOCCER_COMPS:
        raw = ps.get("raw_goals", [])
        lines += [
            f"Gem. goals/game: {ps.get('avg_goals', '?')}",
            f"Laatste {len(raw)} games: {', '.join(str(int(v)) for v in raw[:8])}{'…' if len(raw)>8 else ''}",
        ]

    return lines


SOCCER_COMPS_SET = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}


def format_output(enriched_bets: list, flashscore_analysis: str = ""):
    ranked = sorted(enriched_bets, key=lambda x: x.get("ev", -99), reverse=True)

    print("\n" + "=" * 68)
    print("  BET ANALYSE — Linemate + Seizoensstats")
    print("=" * 68)

    # Flashscore analyse (bovenaan indien aanwezig)
    if flashscore_analysis:
        print("\n📺  FLASHSCORE ANALYSE")
        print("─" * 68)
        print(flashscore_analysis)
        if enriched_bets:
            print("\n" + "─" * 68)
            print("  LINEMATE PROPS")
            print("─" * 68)

    for i, bet in enumerate(ranked, 1):
        score    = bet.get("score", {})
        ps       = bet.get("player_stats", {})
        opp      = bet.get("opponent")
        ev_val   = bet.get("ev", 0)
        rat      = bet.get("rating", "")
        sport    = bet.get("sport_norm", bet.get("sport", ""))
        icon     = _sport_icon(sport)

        print(f"\n#{i}  {rat}")
        print(f"    {icon}  {bet.get('player')} ({sport})")
        print(f"    Bet  : {bet.get('bet_type')}")
        print(f"    Odds : {bet.get('odds_used')}   EV: {ev_val:+.3f}")
        if opp:
            opp_stats = bet.get("opponent_stats", {})
            gaa = opp_stats.get("goals_against_avg")
            gaa_str = f"  ({gaa} GAA)" if gaa else ""
            print(f"    Vs   : {opp}{gaa_str}")
        print()
        print(f"    ┌─ Linemate hit rate : {score.get('linemate_hr', 0):.1%}  "
              f"(sample: {bet.get('sample', '?')})")
        print(f"    ├─ Seizoens hit rate : {score.get('season_hr', 0):.1%}  "
              f"({score.get('games_sampled', 0)} gesampeld via {ps.get('source', '?')})")
        print(f"    ├─ Tegenstander factor: {score.get('opp_factor', 0):.2f}")
        print(f"    └─ Composite score  : {score.get('composite', 0):.1%}")

        stat_lines = _relevant_stats(bet.get("bet_type", ""), ps, sport)
        if stat_lines:
            print()
            for line in stat_lines:
                print(f"    📊 {line}")

        print("    " + "─" * 58)

    # Top 3
    top3 = [b for b in ranked if b.get("rating", "").startswith("✅")][:3]
    if not top3:
        top3 = ranked[:3]

    print("\n🏆  TOP 3 AANBEVELINGEN")
    print("─" * 68)
    for i, bet in enumerate(top3, 1):
        icon = _sport_icon(bet.get("sport_norm", ""))
        print(f"  {i}. {icon} {bet.get('player')} — {bet.get('bet_type')} @ {bet.get('odds_used')}")
        print(f"     EV: {bet.get('ev'):+.3f}  |  Composite: {bet.get('score',{}).get('composite',0):.1%}  |  {bet.get('rating')}")
        if bet.get("opponent"):
            print(f"     Speelt vandaag tegen: {bet.get('opponent')}")
        print()

    print("─" * 68)
    print("⚠️  Disclaimer: verleden prestaties bieden geen garantie voor de toekomst.")
    print("   Gok verantwoord. Zet alleen wat je kunt missen.")
    print("=" * 68)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bet Analyzer v2")
    parser.add_argument("images", nargs="+", help="Linemate screenshots")
    parser.add_argument("--api-key", help="Anthropic API key")
    parser.add_argument("--bet365", nargs="*",
                        help='Bet365 odds: "Speler:1.85" ...')
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌  Geen API key. Stel in via: export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    image_paths = [str(p) for p in map(Path, args.images) if p.exists()]
    if not image_paths:
        print("❌  Geen geldige afbeeldingen gevonden.")
        sys.exit(1)

    bet365_odds = {}
    for item in (args.bet365 or []):
        if ":" in item:
            speler, odds = item.rsplit(":", 1)
            try:
                bet365_odds[speler.strip()] = float(odds.strip())
            except ValueError:
                pass

    client = anthropic.Anthropic(api_key=api_key)

    # Stap 1: bets + wedstrijden extraheren
    bets, matches = extract_bets(client, image_paths)
    if not bets and not matches:
        print("❌  Geen bets of wedstrijden gevonden.")
        sys.exit(1)
    print(f"✅  {len(bets)} props  |  {len(matches)} wedstrijden gevonden\n")

    # Stap 2: props verrijken met seizoensstats
    enriched = []
    if bets:
        print("🔍  Seizoensstats ophalen…")
        daily_cache = _load_daily_cache()
        enriched    = [enrich_bet(bet, bet365_odds, daily_cache) for bet in bets]
        _save_daily_cache(daily_cache)
        print(f"  💾 Dagcache opgeslagen\n")

    # Stap 3: Flashscore analyse (als wedstrijden aanwezig)
    flashscore_analysis = ""
    if matches:
        print("📺  Flashscore wedstrijden analyseren…")
        flashscore_analysis = analyze_flashscore(client, matches, enriched)

    # Stap 4: output
    format_output(enriched, flashscore_analysis)


if __name__ == "__main__":
    main()
