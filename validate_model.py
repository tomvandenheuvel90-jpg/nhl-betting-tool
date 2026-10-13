#!/usr/bin/env python3
"""
validate_model.py — klopt de voorspelde trefkans met de werkelijkheid?

WAAROM DIT SCRIPT BESTAAT
-------------------------
De "Min. odds"-chip op de prop-kaart rekent `1 / voorspelde kans` uit: de odds
waarbij een bet volgens het model break-even draait. Tom stuurt zijn inzet op
dat getal. Maar niemand heeft ooit gecontroleerd of een voorspelde 65% ook
werkelijk in 65% van de gevallen raak is.

Dat is geen detail. Bij 65% heb je odds van 1.54 nodig om quitte te draaien.
Is het in werkelijkheid 52%, dan heb je 1.92 nodig — en zet je maandenlang op
1.60 in de overtuiging dat je waarde pakt, terwijl je gestaag verliest.

Dit script legt de voorspellingen naast de uitkomsten en zegt of ze kloppen.

WAT DIT SCRIPT NIET DOET
------------------------
Het verandert niets. Geen modelaanpassing, geen correctiefactor die ergens
wordt weggeschreven, geen database-write. Het meet en rapporteert, meer niet.
Wat er met de uitkomst gebeurt, is een aparte, bewuste beslissing.

TWEE DATABRONNEN, NIET EVEN BETROUWBAAR
---------------------------------------
  1. SINGLES uit `resultaten`: het veld `composite` is de kans die het model
     destijds voorspelde, `uitkomst` is wat er werkelijk gebeurde. Dit is de
     schone bron — de voorspelling komt gegarandeerd uit het model.

  2. PARLAY-LEGS uit `parlays`: `props_json` bevat per leg een `hit_rate`,
     `legs_json` de uitkomst per leg ("geraakt"/"gemist"). Rommeliger: legs die
     via de Shortlist zijn toegevoegd krijgen `hit_rate: None`, legs uit een
     auto-parlay krijgen de composite, en handmatig toegevoegde legs krijgen
     wat Tom zelf intypte. Legs zonder hit_rate vallen af, maar van de rest is
     de herkomst niet altijd het model.

Daarom worden beide bronnen APART gerapporteerd. Wijkt de conclusie tussen de
twee af, dan leunt ze op de zwakke bron en is ze minder waard.

HOE JE DE UITKOMST LEEST
------------------------
Per kansgroep staat het werkelijke percentage met een 95%-betrouwbaarheids-
interval (Wilson). Dat interval is er omdat toeval meespeelt: bij 12 bets zegt
"58% raak" vrijwel niets. Valt de voorspelling BINNEN het interval, dan is er
geen aantoonbaar verschil. Valt ze erbuiten, dan zit het model er stelselmatig
naast — en zie je meteen welke kant op.

BEPERKINGEN
-----------
  * Alleen bets die Tom daadwerkelijk heeft gezet. Dat is een selectie: het
    zegt iets over het soort props waar hij op afgaat, niet over alle props.
  * Void bets tellen niet mee (geen uitkomst, inzet terugbetaald).
  * Met weinig bets zijn de intervallen breed en is geen conclusie mogelijk.
    Het script zegt dat dan ook, in plaats van een getal te suggereren.

GEBRUIK
-------
    cd /Users/tomvandenheuvel/Documents/BetAnalyzer
    python3 validate_model.py
"""

import json
import math
import os
import re
import sys
from pathlib import Path


# ─── Secrets laden (zelfde patroon als grade_parlay_legs.py) ──────────────────

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


# ─── Parlay-helpers (moeten gelijk blijven aan streamlit_app.py) ──────────────

def _leg_key(leg: dict, legs_json: dict, idx: int) -> str:
    """Zelfde sleutelbepaling als `_parlay_leg_key()` in streamlit_app.py en
    `_leg_key()` in grade_parlay_legs.py: index-vorm als die al bestaat, anders
    de vlakke vorm. Wijkt dit af, dan lezen we de verkeerde leg-status."""
    _player = str(leg.get("player", ""))
    _bt     = str(leg.get("bet_type", ""))
    _indexed = f"{idx}_{_player}_{_bt}"
    if _indexed in (legs_json or {}):
        return _indexed
    return f"{_player}_{_bt}"


def _json_field(row: dict, name: str) -> dict:
    val = row.get(name) or {}
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            val = {}
    return val if isinstance(val, dict) else {}


def _json_list(row: dict, name: str) -> list:
    val = row.get(name) or []
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            val = []
    return val if isinstance(val, list) else []


# ─── Waarnemingen verzamelen ──────────────────────────────────────────────────

def _kans(val) -> float:
    """Normaliseert een kans naar 0–1. Accepteert zowel 0.65 als 65."""
    try:
        p = float(val)
    except (TypeError, ValueError):
        return 0.0
    if p > 1.0:
        p = p / 100.0
    # 1.0 telt mee: dat is een geldige (zij het absurde) voorspelling van 100%.
    # 0 of negatief betekent "geen voorspelling vastgelegd".
    return p if 0.0 < p <= 1.0 else 0.0


def verzamel_singles():
    """(waarnemingen, overgeslagen-tellers) uit de resultaten-tabel."""
    obs, skip = [], {"open of void": 0, "parlay-rij": 0, "geen voorspelling": 0}

    for r in db.load_resultaten():
        rid = str(r.get("id") or "")
        if rid.startswith("parlay_") or r.get("is_parlay"):
            skip["parlay-rij"] += 1
            continue

        uitkomst = str(r.get("uitkomst") or "").lower()
        if uitkomst not in ("gewonnen", "verloren"):
            skip["open of void"] += 1
            continue

        p = _kans(r.get("composite"))
        if p <= 0:
            skip["geen voorspelling"] += 1
            continue

        obs.append({
            "p":      p,
            "raak":   uitkomst == "gewonnen",
            "label":  f"{r.get('speler') or '?'} — {r.get('bet') or '?'}",
            "sport":  (r.get("sport") or "?").upper(),
        })
    return obs, skip


def verzamel_legs():
    """(waarnemingen, overgeslagen-tellers) uit de losse parlay-legs."""
    obs, skip = [], {"nog niet beoordeeld": 0, "void": 0, "geen hit_rate": 0}

    for p_row in db.load_parlays():
        legs   = _json_list(p_row, "props_json")
        status = _json_field(p_row, "legs_json")

        for idx, leg in enumerate(legs):
            st = str(status.get(_leg_key(leg, status, idx), "open")).lower()
            if st == "void":
                skip["void"] += 1
                continue
            if st not in ("geraakt", "gemist"):
                skip["nog niet beoordeeld"] += 1
                continue

            p = _kans(leg.get("hit_rate"))
            if p <= 0:
                skip["geen hit_rate"] += 1
                continue

            obs.append({
                "p":     p,
                "raak":  st == "geraakt",
                "label": f"{leg.get('player') or '?'} — {leg.get('bet_type') or '?'}",
                "sport": (leg.get("sport") or "?").upper(),
            })
    return obs, skip


# ─── Statistiek ───────────────────────────────────────────────────────────────

_Z = 1.96   # 95%


def _wilson(k: int, n: int):
    """95%-betrouwbaarheidsinterval voor een trefpercentage (Wilson score).
    Bewust Wilson en niet de simpele normale benadering: die laatste geeft bij
    kleine n en percentages dicht bij 0 of 100 intervallen die buiten [0,1]
    vallen of veel te smal zijn — precies het bereik waar we hier in zitten."""
    if n == 0:
        return 0.0, 1.0
    phat   = k / n
    denom  = 1 + _Z**2 / n
    center = (phat + _Z**2 / (2 * n)) / denom
    half   = _Z / denom * math.sqrt(phat * (1 - phat) / n + _Z**2 / (4 * n**2))
    return max(0.0, center - half), min(1.0, center + half)


_BUCKETS = [
    ("tot 50%",   0.00, 0.50),
    ("50 – 60%",  0.50, 0.60),
    ("60 – 70%",  0.60, 0.70),
    ("vanaf 70%", 0.70, 1.01),
]

_MIN_N_GROEP  = 10   # onder dit aantal is een groep betekenisloos
_MIN_N_TOTAAL = 25


def _oordeel(voorspeld: float, lo: float, hi: float, n: int, min_n: int = _MIN_N_GROEP) -> str:
    if n < min_n:
        return "te weinig data"
    if voorspeld > hi:
        return "TE OPTIMISTISCH"
    if voorspeld < lo:
        return "te pessimistisch"
    return "klopt"


def _rapporteer(naam: str, obs: list) -> dict:
    print(f"  {naam.upper()}  ({len(obs)} waarnemingen)")
    if not obs:
        print("    geen bruikbare waarnemingen")
        print()
        return {}

    n   = len(obs)
    k   = sum(1 for o in obs if o["raak"])
    gem = sum(o["p"] for o in obs) / n
    lo, hi = _wilson(k, n)

    print(f"    {'groep':<12} {'n':>4}  {'voorspeld':>10}  {'werkelijk':>10}  "
          f"{'95%-interval':>15}   oordeel")
    print("    " + "-" * 74)

    n_groepen_getest = 0
    n_groepen_afwijkend = 0
    for label, ondergrens, bovengrens in _BUCKETS:
        groep = [o for o in obs if ondergrens <= o["p"] < bovengrens]
        if not groep:
            continue
        gn = len(groep)
        gk = sum(1 for o in groep if o["raak"])
        gv = sum(o["p"] for o in groep) / gn
        glo, ghi = _wilson(gk, gn)
        _oor = _oordeel(gv, glo, ghi, gn)
        if _oor not in ("te weinig data",):
            n_groepen_getest += 1
            if _oor != "klopt":
                n_groepen_afwijkend += 1
        print(f"    {label:<12} {gn:>4}  {gv*100:>9.1f}%  {gk/gn*100:>9.1f}%  "
              f"{glo*100:>6.1f}–{ghi*100:>5.1f}%   {_oor}")

    print("    " + "-" * 74)
    _oor_tot = _oordeel(gem, lo, hi, n, _MIN_N_TOTAAL)
    print(f"    {'TOTAAL':<12} {n:>4}  {gem*100:>9.1f}%  {k/n*100:>9.1f}%  "
          f"{lo*100:>6.1f}–{hi*100:>5.1f}%   {_oor_tot}")

    # Waarschuwing voor meervoudig toetsen. Elke groep wordt los op 95% getoetst,
    # dus bij vier groepen is de kans dat er minstens één toevallig uitslaat
    # ongeveer 1 op 5 — ook als het model perfect is. Zonder deze regel leest
    # één rood vakje als bewijs, terwijl het ruis kan zijn.
    if n_groepen_afwijkend == 1 and n_groepen_getest >= 3 and _oor_tot == "klopt":
        print()
        print(f"    ℹ️  Eén van de {n_groepen_getest} groepen wijkt af terwijl het totaal klopt.")
        print(f"       Bij {n_groepen_getest} losse toetsen op 95% gebeurt dat ongeveer één keer")
        print("       op de vijf puur door toeval. Lees dat dus nog niet als een patroon.")

    print()
    return {"n": n, "k": k, "voorspeld": gem, "werkelijk": k / n, "lo": lo, "hi": hi}


# ─── Rapport ──────────────────────────────────────────────────────────────────

def main() -> int:
    bron = "Supabase" if db.is_cloud() else "lokale JSON-bestanden"
    print()
    print("=" * 78)
    print("  MODELVALIDATIE — voorspelde trefkans versus werkelijke uitkomst")
    print("=" * 78)
    print(f"  Databron: {bron}   (dit script leest alleen en verandert niets)")
    print()

    singles, skip_s = verzamel_singles()
    legs,    skip_l = verzamel_legs()

    print("  Overgeslagen — singles:")
    for reden, aantal in skip_s.items():
        if aantal:
            print(f"    {reden:<24} {aantal}")
    print("  Overgeslagen — parlay-legs:")
    for reden, aantal in skip_l.items():
        if aantal:
            print(f"    {reden:<24} {aantal}")
    print()

    res_s = _rapporteer("Singles (schone bron: composite uit het model)", singles)
    res_l = _rapporteer("Parlay-legs (gemengde bron: hit_rate, herkomst wisselt)", legs)

    print("=" * 78)
    print("  CONCLUSIE")
    print("=" * 78)

    if not res_s or res_s["n"] < _MIN_N_TOTAAL:
        n_nu = res_s.get("n", 0)
        print(f"  Te weinig singles om iets te kunnen zeggen: {n_nu}, "
              f"minimaal {_MIN_N_TOTAAL} nodig.")
        if res_l and res_l["n"] >= _MIN_N_TOTAAL:
            print(f"  De parlay-legs ({res_l['n']} stuks) wijzen op "
                  f"{res_l['werkelijk']*100:.1f}% werkelijk tegen "
                  f"{res_l['voorspeld']*100:.1f}% voorspeld, maar die bron is te")
            print("  rommelig om een modelcorrectie op te baseren — de hit_rate komt")
            print("  daar niet altijd uit het model.")
        print()
        print("  Laat eerst meer bets afronden en draai dit script opnieuw.")
        print()
        return 0

    v, w = res_s["voorspeld"], res_s["werkelijk"]
    print(f"  Over {res_s['n']} afgeronde singles voorspelde het model gemiddeld "
          f"{v*100:.1f}%.")
    print(f"  Werkelijk raak: {w*100:.1f}% "
          f"(95%-interval {res_s['lo']*100:.1f}–{res_s['hi']*100:.1f}%).")
    print()

    if v > res_s["hi"]:
        print(f"  Het model is TE OPTIMISTISCH, met {(v-w)*100:.1f} procentpunt.")
        print(f"  Gevolg voor de \"Min. odds\"-chip: waar nu {1/v:.2f} staat, zou er")
        print(f"  ongeveer {1/w:.2f} moeten staan. Je hebt dus hogere odds nodig dan")
        print("  de app je nu voorhoudt.")
    elif v < res_s["lo"]:
        print(f"  Het model is te pessimistisch, met {(w-v)*100:.1f} procentpunt.")
        print(f"  Waar nu {1/v:.2f} staat, zou {1/w:.2f} volstaan — je laat dus bets")
        print("  liggen die wél de moeite waard waren.")
    else:
        print("  Geen aantoonbaar verschil: de voorspelling valt binnen het")
        print("  betrouwbaarheidsinterval van de werkelijke uitkomst. Dat betekent")
        print("  niet dat het model perfect is, wel dat er met deze hoeveelheid data")
        print("  geen stelselmatige afwijking te zien is.")
    print()

    if res_l and res_l["n"] >= _MIN_N_GROEP:
        _verschil = abs(res_l["werkelijk"] - res_s["werkelijk"]) * 100
        print(f"  Controle op de tweede bron: parlay-legs komen uit op "
              f"{res_l['werkelijk']*100:.1f}% werkelijk")
        print(f"  tegen {res_s['werkelijk']*100:.1f}% bij de singles "
              f"({_verschil:.1f} procentpunt verschil).")
        if _verschil > 10:
            print("  Dat is een fors verschil. Vertrouw de singles — daar is zeker dat")
            print("  de voorspelling uit het model komt.")
    print()
    print("  LET OP: dit zijn alleen bets die je zelf hebt gezet. Het zegt iets over")
    print("  jouw selectie van props, niet over alle props die de app toont.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
