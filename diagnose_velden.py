#!/usr/bin/env python3
"""
diagnose_velden.py — waarom legt de app zijn eigen voorspellingen niet vast?

AANLEIDING
----------
`validate_model.py` vond 0 bruikbare waarnemingen op 351 afgeronde singles:
geen enkele rij in `resultaten` heeft een `composite` (de kans die het model
voorspelde). Zonder dat veld is nooit te controleren of het model klopt, en
heeft de sectie "Model Prestaties" in de Bankroll-tab nooit echte data gehad.

Twee mogelijke oorzaken, en ze vragen een verschillende oplossing:

  A. DE KOLOM BESTAAT NIET in Supabase. `db.py` is bewust schema-drift-tolerant:
     slaagt een insert met de optionele kolommen niet, dan probeert hij het
     stilzwijgend opnieuw ZONDER die kolommen (zie `_optional_cols` in
     `add_favoriet()` en `upsert_resultaat()`). Dat is alles-of-niets: ontbreekt
     één optionele kolom, dan sneuvelen ze allemaal. Oplossing: ALTER TABLE.

  B. DE KOLOM BESTAAT WEL maar wordt niet gevuld, omdat het codepad waarlangs
     jij bets afhandelt de voorspelling niet doorgeeft. Oplossing: codewijziging.

Dit script stelt vast welke van de twee het is, en kijkt daarna of de verloren
voorspellingen met terugwerkende kracht te herstellen zijn uit `geschiedenis`.

WAT DIT SCRIPT NIET DOET
------------------------
Niets wijzigen. Geen ALTER TABLE, geen insert, geen update. Het stelt alleen
vast wat er aan de hand is en stelt voor wat er zou moeten gebeuren.

GEBRUIK
-------
    cd /Users/tomvandenheuvel/Documents/BetAnalyzer
    python3 diagnose_velden.py
"""

import json
import os
import re
import sys
from pathlib import Path


def _load_secrets() -> dict:
    def _sanitize(val: str) -> str:
        return re.sub(r"[^\x20-\x7e]", "", val).strip()

    secrets = {}
    for key in ("SUPABASE_URL", "SUPABASE_KEY"):
        val = os.environ.get(key, "")
        if val:
            secrets[key] = _sanitize(val)

    path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("["):
                    break
                m = re.match(r'^(\w+)\s*=\s*"(.*)"\s*$', line)
                if m and m.group(1) not in secrets:
                    secrets[m.group(1)] = _sanitize(m.group(2))
        except Exception:
            pass
    return secrets


_secrets = _load_secrets()
sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

db.init(_secrets.get("SUPABASE_URL", ""), _secrets.get("SUPABASE_KEY", ""))


# Kolommen die `db.py` als "optioneel" behandelt en dus stilzwijgend laat vallen.
_TE_TESTEN = {
    "favorieten":  ["composite", "rating", "team", "game_date",
                    "import_method", "bookmaker", "source_session_id"],
    "resultaten":  ["composite", "rating", "team", "is_parlay",
                    "import_method", "bookmaker", "source_session_id"],
    "geschiedenis": ["session_id", "alle_props_json", "parlay_suggesties_json"],
    "parlays":     ["legs_json", "legs_auto_json", "props_json"],
}


def test_kolommen():
    """Vraagt elke kolom los op. Bestaat hij niet, dan geeft PostgREST een
    foutmelding (SQLSTATE 42703) — dat is het harde bewijs voor oorzaak A."""
    print("  KOLOMMEN IN SUPABASE")
    print(f"    {'tabel':<14} {'kolom':<22} bestaat?")
    print("    " + "-" * 56)

    ontbreekt = {}
    for tabel, kolommen in _TE_TESTEN.items():
        for kol in kolommen:
            try:
                db._supabase.table(tabel).select(kol).limit(1).execute()
                status = "ja"
            except Exception as e:
                msg = str(e)
                if "42703" in msg or "does not exist" in msg.lower():
                    status = "NEE — kolom ontbreekt"
                    ontbreekt.setdefault(tabel, []).append(kol)
                else:
                    status = f"onbekend ({type(e).__name__})"
            print(f"    {tabel:<14} {kol:<22} {status}")
        print()
    return ontbreekt


def test_vulgraad():
    """Van de rijen die we terugkrijgen: hoe vaak is een veld daadwerkelijk
    gevuld? Een kolom die bestaat maar overal leeg is, wijst op oorzaak B."""
    print("  VULGRAAD VAN DE OPGEHAALDE RIJEN")

    for naam, rijen, velden in (
        ("favorieten", db.load_favorieten(), ["composite", "rating", "team", "game_date"]),
        ("resultaten", db.load_resultaten(), ["composite", "rating", "team", "source_session_id"]),
    ):
        print(f"    {naam} — {len(rijen)} rijen")
        if not rijen:
            print("      (leeg)")
            continue
        _aanwezig = set()
        for r in rijen[:200]:
            _aanwezig |= set(r.keys())
        for veld in velden:
            if veld not in _aanwezig:
                print(f"      {veld:<20} sleutel komt in GEEN ENKELE rij voor")
                continue
            gevuld = sum(1 for r in rijen if r.get(veld) not in (None, "", 0, 0.0, False))
            print(f"      {veld:<20} gevuld in {gevuld}/{len(rijen)} rijen "
                  f"({gevuld/len(rijen)*100:.0f}%)")
        print()


def test_herkomst():
    """Waar komen de bets vandaan?

    Dit onderscheidt de twee overgebleven verklaringen voor composite=0%:

      B1. De bets KOMEN uit de analyse, maar `composite` raakt onderweg kwijt.
          Herkenbaar aan: `ev_score` wél gevuld, `composite` niet. De EV komt
          namelijk uit hetzelfde verrijkte bet-object als de composite, dus als
          de een er is en de ander niet, lekt er iets in de doorgifte.

      B2. De bets komen NIET uit de analyse, maar uit een geïmporteerde betslip
          of het handmatige formulier. Die hebben domweg geen modelvoorspelling
          — `screenshot_import.py` zet `composite` expliciet op 0.0 en het
          handmatige formulier geeft het veld helemaal niet mee. Dan is er geen
          bug, maar is de modelvalidatie principieel onmogelijk: je kunt een
          model niet toetsen op bets die het zelf nooit heeft voorgesteld.
    """
    print("  HERKOMST VAN DE BETS")

    for naam, rijen in (("favorieten", db.load_favorieten()),
                        ("resultaten", db.load_resultaten())):
        if not rijen:
            print(f"    {naam}: leeg")
            continue

        via_screenshot = sum(1 for r in rijen if str(r.get("import_method") or "").strip())
        met_ev   = sum(1 for r in rijen if _getal(r.get("ev_score")) != 0)
        met_comp = sum(1 for r in rijen if _getal(r.get("composite")) > 0)
        ev_zonder_comp = sum(1 for r in rijen
                             if _getal(r.get("ev_score")) != 0 and _getal(r.get("composite")) <= 0)

        print(f"    {naam} — {len(rijen)} rijen")
        print(f"      via screenshot-import       {via_screenshot:>5}")
        print(f"      met ev_score (uit analyse)  {met_ev:>5}")
        print(f"      met composite               {met_comp:>5}")
        _vlag = "   <- hier zit een lek" if ev_zonder_comp else ""
        print(f"      ev_score WEL, composite NIET{ev_zonder_comp:>5}{_vlag}")
        print()


def _getal(val) -> float:
    try:
        return float(str(val).replace("+", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm(s) -> str:
    return str(s or "").strip().lower()


def test_herstel():
    """Zijn de verloren voorspellingen terug te halen uit `geschiedenis`?
    Die tabel bewaart per analyse alle props inclusief composite, en
    `resultaten.source_session_id` wijst naar de bijbehorende sessie."""
    print("  HERSTEL UIT GESCHIEDENIS")

    res = [r for r in db.load_resultaten()
           if not str(r.get("id") or "").startswith("parlay_")
           and not r.get("is_parlay")
           and str(r.get("uitkomst") or "").lower() in ("gewonnen", "verloren")]
    print(f"    Afgeronde singles:                      {len(res)}")

    met_sid = [r for r in res if str(r.get("source_session_id") or "").strip()]
    print(f"    ...met een source_session_id:           {len(met_sid)}")
    if not met_sid:
        print("    Zonder die koppeling is herstel niet mogelijk.")
        print()
        return

    # Bouw een index: session_id -> {(speler, bet): composite}
    index, n_sessies = {}, 0
    for entry in db.load_history():
        sid = str(entry.get("session_id") or "").strip()
        if not sid:
            continue
        n_sessies += 1
        props = entry.get("alle_props_json") or entry.get("alle_props") or []
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = []
        for p in props or []:
            comp = p.get("composite")
            try:
                comp = float(comp or 0)
            except (TypeError, ValueError):
                comp = 0.0
            if comp > 0:
                index.setdefault(sid, {})[(_norm(p.get("player")), _norm(p.get("bet_type")))] = comp

    print(f"    Sessies in geschiedenis:                {n_sessies}")
    print(f"    ...waarvan met bruikbare composites:    {len(index)}")

    herstelbaar = 0
    for r in met_sid:
        sleutel = (_norm(r.get("speler")), _norm(r.get("bet")))
        if index.get(str(r.get("source_session_id")).strip(), {}).get(sleutel):
            herstelbaar += 1
    print(f"    Singles die te herstellen zijn:         {herstelbaar}")
    print()
    return herstelbaar


def main() -> int:
    print()
    print("=" * 64)
    print("  VELD-DIAGNOSE — waarom ontbreekt de voorspelling?")
    print("=" * 64)
    if not db.is_cloud():
        print("  Supabase is NIET verbonden — dit script heeft die verbinding nodig")
        print("  om kolommen te kunnen testen. Controleer .streamlit/secrets.toml.")
        print()
        return 1
    print("  Verbonden met Supabase. Dit script leest alleen en wijzigt niets.")
    print()

    ontbreekt = test_kolommen()
    test_vulgraad()
    test_herkomst()
    herstelbaar = test_herstel()

    print("=" * 64)
    print("  CONCLUSIE")
    print("=" * 64)

    if ontbreekt:
        print("  OORZAAK A — er ontbreken kolommen in Supabase.")
        print("  db.py laat optionele kolommen stilzwijgend vallen zodra er één")
        print("  ontbreekt (alles-of-niets), dus hierdoor is ALLES weggevallen.")
        print()
        print("  Draai dit in de Supabase SQL Editor (idempotent, veilig):")
        print()
        _types = {
            "composite": "REAL", "rating": "TEXT", "team": "TEXT",
            "game_date": "TEXT", "import_method": "TEXT", "bookmaker": "TEXT",
            "source_session_id": "TEXT", "is_parlay": "BOOLEAN",
            "session_id": "TEXT", "alle_props_json": "TEXT",
            "parlay_suggesties_json": "TEXT", "legs_json": "TEXT",
            "legs_auto_json": "TEXT", "props_json": "TEXT",
        }
        for tabel, kols in ontbreekt.items():
            for kol in kols:
                print(f"    ALTER TABLE {tabel} ADD COLUMN IF NOT EXISTS "
                      f"{kol} {_types.get(kol, 'TEXT')};")
        print()
        print("  Daarna legt de app zijn voorspellingen weer vast — vooruit.")
    else:
        print("  OORZAAK B — alle kolommen bestaan, dus het ligt niet aan Supabase.")
        print()
        _favs = db.load_favorieten()
        _lek  = sum(1 for r in _favs
                    if _getal(r.get("ev_score")) != 0 and _getal(r.get("composite")) <= 0)
        _uit_analyse = sum(1 for r in _favs if _getal(r.get("ev_score")) != 0)

        if _lek > 0:
            print(f"  B1 — {_lek} favorieten hebben wél een ev_score maar geen composite.")
            print("  Die bets kwamen dus uit de analyse, maar de voorspelling is")
            print("  onderweg kwijtgeraakt. Dat is een echte bug in de doorgifte en")
            print("  vraagt een codewijziging.")
        elif _uit_analyse == 0:
            print("  B2 — geen enkele bewaarde bet komt uit de analyse-tab.")
            print("  Ze komen uit geïmporteerde betslips en het handmatige formulier,")
            print("  en die hebben van nature geen modelvoorspelling. Er is dus geen")
            print("  bug: de app kan niet vastleggen wat het model nooit heeft")
            print("  voorgesteld.")
            print()
            print("  Gevolg: de modelvalidatie is principieel onmogelijk zolang je")
            print("  bets niet via de 🔍 Analyse-tab in je shortlist belanden. Je")
            print("  kunt een model niet toetsen op bets die het niet heeft gekozen.")
        else:
            print(f"  Gemengd beeld: {_uit_analyse} bets met ev_score, waarvan")
            print(f"  {_lek} zonder composite. Bekijk de herkomst-tabel hierboven.")
    print()

    if herstelbaar:
        print(f"  Daarnaast: {herstelbaar} afgeronde bets zijn met terugwerkende")
        print("  kracht te herstellen uit de geschiedenis-tabel. Dat zou de")
        print("  modelvalidatie meteen bruikbaar maken in plaats van maanden")
        print("  wachten op nieuwe data.")
    else:
        print("  Herstel met terugwerkende kracht lijkt niet mogelijk; de")
        print("  validatie zal op nieuwe bets moeten wachten.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
