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


def _leg_key(leg: dict, legs_json: dict, idx: int) -> str:
    """
    Bepaalt welke sleutel voor deze leg daadwerkelijk in legs_json gebruikt
    wordt/moet worden. Twee historische conventies komen voor:
      - "player_bettype"      — huidige/nieuwe parlays (Parlay Builder in de app)
      - "idx_player_bettype"  — oudere parlays aangemaakt via screenshot_import.py

    Geeft de sleutel terug die al in legs_json voorkomt (idx-vorm heeft
    voorrang); bestaat geen van beide, dan de vlakke vorm. Moet exact gelijk
    blijven aan _parlay_leg_key() in streamlit_app.py — anders schrijft dit
    script naar een sleutel die de UI niet leest (en lijkt het net als eerder
    alsof een leg "beoordeeld" is terwijl de badge in de app "open" blijft
    tonen).
    """
    _player = str(leg.get("player", ""))
    _bt     = str(leg.get("bet_type", ""))
    _indexed = f"{idx}_{_player}_{_bt}"
    if _indexed in (legs_json or {}):
        return _indexed
    return f"{_player}_{_bt}"


def _load_secrets() -> dict:
    """
    Config-bron voor dit standalone script — twee mogelijke bronnen:

      1. Environment variables (SUPABASE_URL, SUPABASE_KEY, FOOTBALL_DATA_TOKEN).
         Gebruikt door de GitHub Actions workflow (.github/workflows/grade-parlay-legs.yml),
         die deze drie waarden als repo-secrets aanlevert. Heeft voorrang.
      2. .streamlit/secrets.toml — zelfde bestand als de Streamlit-app gebruikt,
         handig als dit script lokaal/handmatig gedraaid wordt. Alleen top-level
         KEY = "value"-regels vóór de eerste [sectie] worden gelezen; dat is
         voldoende voor SUPABASE_URL/KEY en FOOTBALL_DATA_TOKEN, die in
         secrets.toml altijd boven de [gcp_service_account]-sectie staan.

    Env vars winnen als beide aanwezig zijn — zo kan de GitHub Actions runner
    nooit per ongeluk van een lokaal secrets.toml-bestand afhangen (dat bestand
    staat sowieso niet in git, dus in Actions bestaat het simpelweg niet).

    Alle waarden worden opgeschoond tot printbare ASCII: bij het kopiëren/
    plakken van lange strings (met name de Supabase JWT) in de GitHub-
    secrets-UI kan een onzichtbaar teken meekomen (bijv. U+2028 "line
    separator"), wat verderop een UnicodeEncodeError geeft zodra het als
    HTTP-header (Authorization: Bearer ...) gebruikt wordt. URL's en JWT's
    bevatten legitiem nooit iets buiten printbare ASCII, dus dit is veilig.
    """
    def _sanitize(val: str) -> str:
        return re.sub(r"[^\x20-\x7e]", "", val).strip()

    secrets = {}
    for key in ("SUPABASE_URL", "SUPABASE_KEY", "FOOTBALL_DATA_TOKEN"):
        val = os.environ.get(key, "")
        if val:
            secrets[key] = _sanitize(val)

    path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                break
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"\s*$', line)
            if m and m.group(1) not in secrets:
                secrets[m.group(1)] = _sanitize(m.group(2))
    return secrets


_secrets = _load_secrets()
# Vóór het importeren van sports.soccer instellen — API_KEY wordt daar op
# module-niveau uit de omgevingsvariabele gelezen (zelfde patroon als
# streamlit_app.py's "Secrets injecteren vóór import van sports modules").
os.environ.setdefault("FOOTBALL_DATA_API_KEY", _secrets.get("FOOTBALL_DATA_TOKEN", ""))

import db
import scorer
from prompts import SOCCER_COMPS
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

        # Alleen sportwaarden die we kennen als voetbal (SOCCER_COMPS uit
        # prompts.py — zelfde whitelist als de rest van de app gebruikt, bijv.
        # "EPL", "CHAMPIONSHIP", "VOETBAL", "SOCCER", of "" als het sportveld
        # leeg is) naar football-data.org sturen. Alles daarbuiten (bijv.
        # "TENNIS", "OVERIG") is een sport waar we geen module voor hebben —
        # die moet expliciet als niet-ondersteund overgeslagen worden i.p.v.
        # als een (niet-bestaande) football-data.org competitiecode ernaartoe
        # te sturen. Voorheen gebeurde dat wel: /competitions/TENNIS/teams
        # gaf een HTTP 400 en de misleidende reden "speler niet gevonden
        # (voetbal — TENNIS)" voor wat eigenlijk een tennis-leg was.
        if sk not in SOCCER_COMPS and sk != "":
            return None, f"sport niet ondersteund ({sk}) — geen sport-module beschikbaar"

        comp = sk if sk not in ("VOETBAL", "SOCCER", "FOOTBALL", "") else "EPL"
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
    # Diagnose vooraf: zonder dit is een resultaat van "0 beoordeeld" niet te
    # onderscheiden van een stille verbindingsfout met Supabase (db.init()
    # slikt alle exceptions en valt dan terug op lokale JSON, die op een
    # verse GitHub Actions-runner niet bestaat — dus ook 0 parlays oplevert).
    print(f"Supabase verbonden: {db.is_cloud()}")
    if not db.is_cloud():
        print(f"  Reden: {getattr(db, '_last_init_error', '(onbekend)')}")
        print(f"  SUPABASE_URL aanwezig: {bool(_secrets.get('SUPABASE_URL'))}  |  SUPABASE_KEY aanwezig: {bool(_secrets.get('SUPABASE_KEY'))}")

    parlays = db.load_parlays()
    print(f"Parlays geladen: {len(parlays)}")

    n_legs_total = 0
    n_legs_open = 0
    for _p in parlays:
        _legs = _p.get("props_json") or []
        if isinstance(_legs, str):
            try:
                _legs = json.loads(_legs)
            except Exception:
                _legs = []
        _lj = _load_json_field(_p, "legs_json")
        for _li, _leg in enumerate(_legs):
            n_legs_total += 1
            _key = _leg_key(_leg, _lj, _li)
            if _lj.get(_key, "open") == "open":
                n_legs_open += 1
    print(f"Legs totaal: {n_legs_total}  |  Legs met status 'open': {n_legs_open}")

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

        for _leg_idx, leg in enumerate(legs):
            key = _leg_key(leg, legs_json, _leg_idx)
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
