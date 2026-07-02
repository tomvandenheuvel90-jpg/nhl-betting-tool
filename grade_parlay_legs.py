"""
grade_parlay_legs.py — Automatische per-leg beoordeling van opgeslagen parlays.

Loopt over alle opgeslagen parlays en probeert voor elke leg met status "open"
en een wedstrijddatum vóór vandaag automatisch te bepalen of de prop geraakt
of gemist is, op basis van de bestaande sport-API's (sports/nba.py, mlb.py,
nhl.py, soccer.py) en de bet-type parsing uit scorer.py.

Bedoeld om dagelijks te draaien (via een scheduled task), zodat resultaten die
"vandaag" gespeeld zijn de volgende dag automatisch verwerkt worden.

Wat dit script WEL doet:
  - Vult per leg de status in legs_json in: "geraakt" of "gemist"
  - Zet legs_auto_json[leg_key] = datum van beoordeling (voor de 🤖-badge in de UI)
  - Laat legs die niet automatisch te bepalen zijn op "open" staan — nooit gokken

Wat dit script NIET doet:
  - Het settelt nooit de hele parlay (uitkomst / winst_verlies). Dat blijft een
    bewuste, handmatige actie van Tom via de ✅ Gewonnen / ❌ Verloren-knoppen
    op het dashboard — expliciet zo gekozen, want dat onderdeel vindt hij leuk
    om zelf te doen.
  - Het overschrijft nooit een leg die al een status heeft (handmatig of eerder
    automatisch gezet) — alleen legs met status "open" worden aangepakt.

Gebruik:
    python3 grade_parlay_legs.py            # voert wijzigingen door
    python3 grade_parlay_legs.py --dry-run   # toont alleen wat er zou gebeuren
"""

import json
import os
import re
import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _load_secrets() -> dict:
    """
    Leest .streamlit/secrets.toml handmatig — er is geen streamlit-context
    beschikbaar in een standalone script, dus st.secrets werkt hier niet.
    Alleen top-level KEY = "value"-regels vóór de eerste [sectie] worden
    gelezen; dat is voldoende voor SUPABASE_URL/KEY en FOOTBALL_DATA_TOKEN,
    die in secrets.toml altijd boven de [gcp_service_account]-sectie staan.
    """
    secrets = {}
    path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if not path.exists():
        return secrets
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"\s*$', line)
        if m:
            secrets[m.group(1)] = m.group(2)
    return secrets


_secrets = _load_secrets()
# Vóór het importeren van sports.soccer instellen — API_KEY wordt daar op
# module-niveau uit de omgevingsvariabele gelezen (zelfde patroon als
# streamlit_app.py's "Secrets injecteren vóór import van sports modules").
os.environ.setdefault("FOOTBALL_DATA_API_KEY", _secrets.get("FOOTBALL_DATA_TOKEN", ""))

import db
import scorer
from sports import nba, mlb, nhl, soccer

# Belt-and-suspenders: ook direct instellen mocht de env-var al eerder gezet zijn
# geweest (bijv. een lege string) vóór dit script draaide.
if _secrets.get("FOOTBALL_DATA_TOKEN"):
    soccer.API_KEY = _secrets["FOOTBALL_DATA_TOKEN"]

# Supabase verbinden zodat legs_json/legs_auto_json persistent worden opgeslagen.
# Lukt dit niet (geen secrets, verbindingsfout), dan valt db.py automatisch terug
# op de lokale JSON-bestanden — precies zoals in de Streamlit-app zelf.
db.init(_secrets.get("SUPABASE_URL", ""), _secrets.get("SUPABASE_KEY", ""))


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _sport_key(sport: str) -> str:
    return (sport or "").strip().upper()


def _resolve_player_stats(sport: str, player_name: str, team_hint: str = ""):
    """
    Zoek de speler op en haal per-game raw stats + datums op via de juiste
    sport-module. Geeft (stats_dict_of_None, skip_reason_of_None) terug.
    """
    sk = _sport_key(sport)
    try:
        if sk == "NHL":
            player_id, _team = nhl.find_player(player_name)
            if not player_id:
                return None, "speler niet gevonden (NHL)"
            return nhl.get_player_stats(player_id), None

        if sk == "NBA":
            player = nba.find_player(player_name)
            if not player:
                return None, "speler niet gevonden (NBA)"
            return nba.get_player_stats(player["id"]), None

        if sk == "MLB":
            player = mlb.find_player(player_name)
            if not player:
                return None, "speler niet gevonden (MLB)"
            pos_code = (player.get("primaryPosition") or {}).get("code", "")
            pos_type = "pitching" if pos_code == "1" else "hitting"
            return mlb.get_player_stats(player.get("id"), position_type=pos_type), None

        # Alles overig behandelen we als voetbal (EPL, Championship, La Liga, ...)
        # — zelfde aanpak als analysis.py enrich_bet().
        comp = sk if sk not in ("VOETBAL", "") else "EPL"
        player = soccer.find_player(player_name, team_hint=team_hint, competition=comp)
        if not player:
            return None, f"speler niet gevonden (voetbal — {comp})"
        stats = soccer.get_player_stats(player.get("id"), player.get("team_id"), comp)
        return stats, None
    except Exception as e:
        return None, f"API-fout: {type(e).__name__}: {e}"


def grade_leg(leg: dict, fallback_game_date: str):
    """
    Beoordeelt één leg.
    Geeft (nieuwe_status_of_None, reden_string) terug.
    nieuwe_status is "geraakt" / "gemist", of None als (nog) niet automatisch
    te bepalen — in dat geval blijft de leg op "open" staan.
    """
    player_name = str(leg.get("player") or "").strip()
    bet_type    = str(leg.get("bet_type") or "").strip()
    sport       = str(leg.get("sport") or "").strip()
    game_date   = str(leg.get("game_date") or fallback_game_date or "")[:10]

    if not player_name or not bet_type or not sport:
        return None, "onvolledige leg-data (speler/bet_type/sport ontbreekt)"

    if not game_date:
        return None, "geen wedstrijddatum bekend"

    if game_date >= _today_iso():
        return None, "wedstrijd nog niet (zeker) gespeeld"

    stats, skip_reason = _resolve_player_stats(sport, player_name, leg.get("team", ""))
    if skip_reason:
        return None, skip_reason

    dates = stats.get("dates") or []
    if game_date not in dates:
        return None, "wedstrijddatum niet gevonden in recente API-data (buiten opgehaald bereik)"

    idx = dates.index(game_date)

    raw_values, threshold, use_gte = scorer._get_raw_and_line(bet_type, stats)
    if not raw_values or idx >= len(raw_values):
        return None, "onbekend bet-type of geen bijbehorende statistiek beschikbaar"

    actual = raw_values[idx]
    hit = (actual >= threshold) if use_gte else (actual > threshold)
    return ("geraakt" if hit else "gemist"), f"waarde={actual}, grens={threshold}, gte={use_gte}"


def _load_json_field(parlay: dict, field: str) -> dict:
    val = parlay.get(field) or {}
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return dict(val)


def main(dry_run: bool = False):
    parlays = db.load_parlays()
    n_graded = 0
    n_left_open = 0
    n_parlays_touched = 0
    details = []

    for parlay in parlays:
        legs = parlay.get("props_json") or []
        if isinstance(legs, str):
            try:
                legs = json.loads(legs)
            except Exception:
                legs = []
        if not legs:
            continue

        legs_json = _load_json_field(parlay, "legs_json")
        legs_auto = _load_json_field(parlay, "legs_auto_json")

        # Fallback game_date voor oudere legs zonder eigen datum: de plaatsingsdatum
        # van de parlay zelf (best-effort — parlays worden meestal dezelfde dag
        # geplaatst als de wedstrijd(en) plaatsvinden).
        fallback_date = str(parlay.get("datum") or "")[:10]
        parlay_id = parlay.get("id", "")
        changed = False

        for leg in legs:
            key = str(leg.get("player", "")) + "_" + str(leg.get("bet_type", ""))
            current_status = legs_json.get(key, "open")
            if current_status != "open":
                continue  # al beoordeeld (handmatig of eerder automatisch) — nooit overschrijven

            new_status, reason = grade_leg(leg, fallback_date)
            if new_status is None:
                n_left_open += 1
                details.append(f"  \u25cb open gelaten — parlay {parlay_id} · {key} — {reason}")
                continue

            legs_json[key] = new_status
            legs_auto[key] = _today_iso()
            changed = True
            n_graded += 1
            details.append(f"  \u2705 {new_status} — parlay {parlay_id} · {key} — {reason}")

        if changed:
            n_parlays_touched += 1
            if not dry_run:
                db.update_parlay(parlay_id, {
                    "legs_json": legs_json,
                    "legs_auto_json": legs_auto,
                })

    summary = (
        f"Parlay-leg beoordeling voltooid ({_today_iso()}).\n"
        f"  Legs automatisch beoordeeld: {n_graded}\n"
        f"  Legs open gelaten (handmatige review nodig): {n_left_open}\n"
        f"  Parlays bijgewerkt: {n_parlays_touched}\n"
    )
    if dry_run:
        summary += "  (DRY RUN — er is niets opgeslagen)\n"

    print(summary)
    if details:
        print("\n".join(details))

    return {
        "graded": n_graded,
        "left_open": n_left_open,
        "parlays_touched": n_parlays_touched,
    }


if __name__ == "__main__":
    _dry = "--dry-run" in sys.argv
    main(dry_run=_dry)
