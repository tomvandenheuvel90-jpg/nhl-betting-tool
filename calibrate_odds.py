#!/usr/bin/env python3
"""
calibrate_odds.py — meet hoeveel hoger de Linemate-odds liggen dan de odds
waarmee Tom werkelijk heeft geplaatst.

WAAROM DIT SCRIPT BESTAAT
-------------------------
De EV in de app wordt berekend met `linemate_odds` (analysis.py, enrich_bet).
Linemate toont odds van bookmakers die Tom niet kan gebruiken en die hoger
liggen dan bij BetCity/Bet365. Daardoor is elke EV in de app te optimistisch.

Belangrijk, en contra-intuïtief: dat vertekent de VOLGORDE van de proplijst
NIET. `scorer.ev()` komt neer op `kans × odds − 1`, dus sorteren op EV is
hetzelfde als sorteren op `kans × odds`. Als alle odds met dezelfde factor te
hoog staan, schaal je die hele kolom met een constante en blijft de rangorde
exact gelijk.

Wat wél kapot gaat zijn de DREMPELS, want die vergelijken met een vast getal:
  * `filter_and_rank_props()` sluit props uit op `ev <= 0`   (analysis.py)
  * `scorer.rating()` gebruikt de grenzen 0.25 en 0.05
  * `_weighted_ev` (ev × sample/20) stuurt top3 en parlay-suggesties

Eén getal repareert alle drie: de verhouding tussen de werkelijke odds en de
Linemate-odds. Dit script meet die verhouding uit Tom's eigen geschiedenis.

HOE DE METING WERKT
-------------------
  * `favorieten.odds`  = de odds zoals Linemate ze toonde (bij opslaan).
  * `resultaten.odds`  = de odds waarmee werkelijk is geplaatst. In de
                         ⭐ Shortlist is dat veld aanpasbaar bij het plaatsen.
  * Beide tabellen delen dezelfde `id` (db.make_fav_id), dus ze zijn te koppelen.

  verhouding = resultaten.odds / favorieten.odds

Een verhouding van 0.89 betekent: je krijgt gemiddeld 89% van de odds die
Linemate toonde — oftewel 11% minder.

BELANGRIJKE BEPERKING
---------------------
Dit werkt alleen voor bets waarbij de odds bij het plaatsen zijn AANGEPAST.
Heb je de voorgevulde waarde laten staan, dan zijn beide odds identiek en
zegt die rij niets. Het script telt die apart en rekent ze niet mee — anders
zou de uitkomst richting 1.00 worden getrokken en lijkt het alsof er geen
verschil is terwijl het alleen niet gemeten is.

GEBRUIK
-------
    cd /Users/tomvandenheuvel/Documents/BetAnalyzer
    python3 calibrate_odds.py

Het script LEEST alleen. Het schrijft niets naar Supabase of naar schijf.
"""

import os
import re
import statistics
import sys
from pathlib import Path


# ─── Secrets laden (zelfde patroon als grade_parlay_legs.py) ──────────────────

def _load_secrets() -> dict:
    """Leest SUPABASE_URL/KEY uit environment variables, anders uit
    .streamlit/secrets.toml. Lukt geen van beide, dan valt db.py terug op de
    lokale JSON-bestanden."""
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
                    break                      # alleen top-level regels
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


# ─── Markttype-indeling ───────────────────────────────────────────────────────

_MARKT_REGELS = [
    ("Wedstrijdmarkt", ("moneyline", "money line", "1x2", "double chance", "draw no bet",
                        "puck line", "run line", "spread", "handicap", "btts",
                        "both teams", "winnaar", "to win")),
    ("Totalen",        ("total", "over/under", "o/u", "corner")),
    ("Speler — doelpunt/score", ("anytime", "scorer", "goal", "home run", "homer", "touchdown")),
    ("Speler — volume",         ("shot", "sog", "point", "pts", "rebound", "reb", "assist",
                                 "ast", "three", "3pt", "3pm", "block", "steal", "hit",
                                 "base", "rbi", "strikeout", "save", "pass")),
]


def _markt_bucket(bet: str) -> str:
    b = (bet or "").lower()
    for naam, sleutels in _MARKT_REGELS:
        if any(s in b for s in sleutels):
            return naam
    return "Overig"


# ─── Paren opbouwen ───────────────────────────────────────────────────────────

def _odds(row: dict) -> float:
    try:
        return float(row.get("odds") or 0)
    except (TypeError, ValueError):
        return 0.0


def verzamel_paren():
    """Koppelt favorieten aan resultaten en geeft (bruikbaar, uitleg) terug."""
    favs = {f.get("id"): f for f in db.load_favorieten() if f.get("id")}
    res  = [r for r in db.load_resultaten() if r.get("id")]

    bruikbaar = []
    redenen   = {
        "geen favoriet":        0,   # resultaat zonder bijbehorende favoriet
        "parlay":               0,
        "odds ontbreken":       0,
        "screenshot-import":    0,
        "odds ongewijzigd":     0,
    }

    for r in res:
        rid = r.get("id")
        if str(rid).startswith("parlay_"):
            redenen["parlay"] += 1
            continue

        fav = favs.get(rid)
        if not fav:
            redenen["geen favoriet"] += 1
            continue

        # Screenshot-import: de favoriet is gemaakt vanaf een betslip van zijn
        # eigen bookmaker, dus BEIDE kanten zijn al BetCity-odds. Zo'n paar zegt
        # niets over het verschil met Linemate.
        if str(fav.get("import_method") or "").strip():
            redenen["screenshot-import"] += 1
            continue

        o_lm, o_echt = _odds(fav), _odds(r)
        if o_lm <= 1.0 or o_echt <= 1.0:
            redenen["odds ontbreken"] += 1
            continue

        ratio = o_echt / o_lm
        if abs(ratio - 1.0) < 0.005:
            redenen["odds ongewijzigd"] += 1
            continue

        # Herkomst van de opgeslagen odds. Dit is cruciaal en ging eerder mis:
        # alleen een favoriet die aantoonbaar uit de 🔍 Analyse-tab komt heeft
        # een LINEMATE-odds in het `odds`-veld. Een favoriet uit het handmatige
        # formulier bevat de odds die Tom zelf intypte — doorgaans al die van
        # zijn eigen bookmaker. Zo'n paar vergelijkt BetCity met BetCity en
        # levert per definitie een verhouding rond 1.00 op, wat ten onrechte
        # leest als "Linemate wijkt nauwelijks af".
        #
        # `composite > 0` is het bewijs van analyse-herkomst: dat veld wordt
        # alleen gevuld door het model. (Tot 13-10-2026 wiste een latere
        # betslip-import dit veld, waardoor ALLE favorieten op 0 stonden en
        # geen enkel paar als betrouwbaar kon worden aangemerkt.)
        try:
            _uit_analyse = float(fav.get("composite") or 0) > 0
        except (TypeError, ValueError):
            _uit_analyse = False

        bruikbaar.append({
            "id":     rid,
            "speler": fav.get("speler") or r.get("speler") or "?",
            "bet":    fav.get("bet") or r.get("bet") or "?",
            "sport":  (fav.get("sport") or r.get("sport") or "?").upper() or "?",
            "markt":  _markt_bucket(fav.get("bet") or r.get("bet")),
            "o_lm":   o_lm,
            "o_echt": o_echt,
            "ratio":  ratio,
            "betrouwbaar": _uit_analyse,
        })

    return bruikbaar, redenen, len(res)


# ─── Statistiek ───────────────────────────────────────────────────────────────

def _samenvatting(ratios: list) -> dict:
    n = len(ratios)
    out = {"n": n, "mediaan": statistics.median(ratios), "gemiddelde": sum(ratios) / n}
    if n >= 4:
        q = statistics.quantiles(ratios, n=4)
        out["p25"], out["p75"] = q[0], q[2]
    else:
        out["p25"] = out["p75"] = None
    return out


def _regel(label: str, s: dict) -> str:
    # Negatief = je krijgt MINDER dan Linemate toont. Bewust als verandering
    # t.o.v. Linemate geschreven en niet als "korting 10%", omdat een los
    # "+10%" leest alsof de odds hoger zouden zijn.
    verandering = (s["mediaan"] - 1) * 100
    spreiding = ""
    if s["p25"] is not None:
        spreiding = f"   spreiding {s['p25']:.3f}–{s['p75']:.3f}"
    return (f"  {label:<28} n={s['n']:<4} mediaan {s['mediaan']:.3f} "
            f"({verandering:+.1f}% t.o.v. Linemate){spreiding}")


# ─── Rapport ──────────────────────────────────────────────────────────────────

_MIN_N = 15   # onder dit aantal is de uitkomst te wankel om op te sturen


def main() -> int:
    bron = "Supabase" if db.is_cloud() else "lokale JSON-bestanden"
    print()
    print("=" * 72)
    print("  ODDS-KALIBRATIE — Linemate versus werkelijk geplaatst")
    print("=" * 72)
    print(f"  Databron: {bron}   (dit script leest alleen, het schrijft nooit)")
    print()

    paren, redenen, n_res = verzamel_paren()

    print(f"  Resultaat-rijen bekeken: {n_res}")
    for reden, aantal in redenen.items():
        if aantal:
            print(f"    overgeslagen — {reden:<22} {aantal}")
    print(f"  Bruikbare paren (odds wél aangepast): {len(paren)}")
    print()

    if not paren:
        print("  GEEN BRUIKBARE DATA.")
        print()
        if redenen["odds ongewijzigd"]:
            print(f"  Let op: bij {redenen['odds ongewijzigd']} bets stonden de odds in")
            print("  `resultaten` exact gelijk aan die in `favorieten`. Dat betekent dat je")
            print("  bij het plaatsen de voorgevulde Linemate-odds hebt laten staan, niet dat")
            print("  er geen verschil is — het verschil is simpelweg nooit vastgelegd.")
            print()
            print("  Om dit te kunnen meten: vul bij het plaatsen in de ⭐ Shortlist het")
            print("  odds-veld in met wat je bookmaker je werkelijk gaf. Na een stuk of")
            print("  twintig bets kun je dit script opnieuw draaien.")
        else:
            print("  Er zijn nog geen geplaatste bets die aan een favoriet gekoppeld zijn.")
        print()
        return 0

    # ── Herkomstcontrole ──────────────────────────────────────────────────────
    # Zonder deze stap meet het script mogelijk BetCity tegen BetCity. Zie de
    # uitleg bij `betrouwbaar` hierboven.
    _zeker = [p for p in paren if p.get("betrouwbaar")]
    print(f"  Waarvan aantoonbaar uit de Analyse-tab (met modelvoorspelling): "
          f"{len(_zeker)} van {len(paren)}")
    if not _zeker:
        print()
        print("  ⚠️  GEEN ENKEL PAAR IS BETROUWBAAR.")
        print("  Van geen van deze favorieten staat vast dat het opgeslagen")
        print("  odds-veld een Linemate-odds bevat. Komen ze uit het handmatige")
        print("  formulier, dan heb je die odds zelf ingetypt — doorgaans al die")
        print("  van je eigen bookmaker. Dan vergelijkt dit script BetCity met")
        print("  BetCity en komt er vanzelf iets rond 1.00 uit, wat niets zegt.")
        print()
        print("  De cijfers hieronder zijn dus NIET te gebruiken als maat voor de")
        print("  Linemate-opslag. Ze staan er alleen ter informatie.")
        print()
    elif len(_zeker) < len(paren):
        print(f"  De overige {len(paren) - len(_zeker)} paren hebben geen bewijs van")
        print("  herkomst; die kunnen BetCity-tegen-BetCity zijn. Hieronder staan")
        print("  beide groepen apart.")
        print()

    ratios = [p["ratio"] for p in paren]
    totaal = _samenvatting(ratios)

    if _zeker:
        print("  BETROUWBAAR (odds aantoonbaar van Linemate)")
        print(_regel("uit de Analyse-tab", _samenvatting([p["ratio"] for p in _zeker])))
        print()

    print("  ALLE PAREN (incl. onbewezen herkomst)")
    print(_regel("alle bets", totaal))
    print()

    # ── Per sport ──
    per_sport = {}
    for p in paren:
        per_sport.setdefault(p["sport"], []).append(p["ratio"])
    if len(per_sport) > 1:
        print("  PER SPORT")
        for sport in sorted(per_sport, key=lambda s: -len(per_sport[s])):
            print(_regel(sport, _samenvatting(per_sport[sport])))
        print()

    # ── Per markttype ──
    per_markt = {}
    for p in paren:
        per_markt.setdefault(p["markt"], []).append(p["ratio"])
    print("  PER MARKTTYPE")
    for markt in sorted(per_markt, key=lambda m: -len(per_markt[m])):
        print(_regel(markt, _samenvatting(per_markt[markt])))
    print()

    # ── Grootste uitschieters, als steekproef om met het oog te controleren ──
    print("  GROOTSTE AFWIJKINGEN (handmatig controleren of dit klopt)")
    for p in sorted(paren, key=lambda x: x["ratio"])[:5]:
        print(f"    {p['speler'][:22]:<22} {p['bet'][:26]:<26} "
              f"{p['o_lm']:.2f} → {p['o_echt']:.2f}  ({(p['ratio']-1)*100:+.0f}%)")
    print()

    # ── Conclusie ──
    print("=" * 72)
    print("  CONCLUSIE")
    print("=" * 72)

    # Conclusie uitsluitend op de betrouwbare groep. Is die leeg, dan is er geen
    # conclusie — een getal uit onbewezen paren is erger dan geen getal, want het
    # leest als bewijs terwijl het BetCity-tegen-BetCity kan zijn.
    if not _zeker:
        print("  GEEN CONCLUSIE MOGELIJK.")
        print()
        print("  Er is geen enkel paar waarvan vaststaat dat het de Linemate-odds")
        print("  bevat. Dit script kan de opslag pas meten als er bets zijn die")
        print("  via de 🔍 Analyse-tab op de shortlist zijn gezet (die krijgen een")
        print("  modelvoorspelling mee) én waarbij je de odds bij het plaatsen hebt")
        print("  aangepast naar wat je bookmaker werkelijk gaf.")
        print()
        return 0

    _zeker_stats = _samenvatting([p["ratio"] for p in _zeker])
    totaal = _zeker_stats
    med = totaal["mediaan"]

    if totaal["n"] < _MIN_N:
        print(f"  Te weinig data: {totaal['n']} paren, minimaal {_MIN_N} gewenst.")
        print(f"  De voorlopige mediaan is {med:.3f} ({(1-med)*100:+.1f}%), maar daar")
        print("  moet je nog geen drempel op baseren. Draai dit script opnieuw zodra")
        print("  er meer bets met aangepaste odds in staan.")
        print()
        return 0

    print(f"  Je krijgt mediaan {med*100:.1f}% van de odds die Linemate toont")
    print(f"  ({(1-med)*100:.1f}% lager).")
    print()

    # Wat betekent dat voor de EV-drempel?
    # EV = kans × odds − 1. Met odds_echt = med × odds_linemate geldt:
    #   EV_echt = med × (EV_app + 1) − 1
    # EV_echt > 0  <=>  EV_app > 1/med − 1
    drempel = (1.0 / med) - 1.0
    print(f"  Daaruit volgt de filterdrempel: een prop is pas écht positief als de")
    print(f"  EV in de app boven +{drempel:.3f} ligt, niet boven 0. Alles daartussen")
    print(f"  toont de app nu als winstgevend terwijl het dat niet is.")
    print()
    print(f"  Omrekenen van app-EV naar echte EV:   echt = {med:.3f} × (app_EV + 1) − 1")
    print()

    # Verschilt de korting per markttype? Dan is óók de volgorde vertekend.
    bruikbare_markten = {m: r for m, r in per_markt.items() if len(r) >= 5}
    if len(bruikbare_markten) >= 2:
        medianen = {m: statistics.median(r) for m, r in bruikbare_markten.items()}
        spreiding = max(medianen.values()) - min(medianen.values())
        print(f"  Spreiding tussen markttypes: {spreiding:.3f}")
        if spreiding >= 0.05:
            _laag = min(medianen, key=medianen.get)
            _hoog = max(medianen, key=medianen.get)
            print(f"  Dit verschilt wezenlijk per markt ({_laag} vs {_hoog}).")
            print("  Dan is niet alleen de drempel verkeerd, maar óók de VOLGORDE van de")
            print("  lijst: markten met een kleine korting worden stelselmatig te laag")
            print("  gerangschikt. Een korting per markttype is dan de moeite waard.")
        else:
            print("  Dat is klein genoeg om te verwaarlozen: de korting is in de praktijk")
            print("  uniform, dus de VOLGORDE van je proplijst klopt al. Eén enkele")
            print("  drempelcorrectie is voldoende — per markt differentiëren voegt niets toe.")
    else:
        print("  Nog te weinig data per markttype om te zien of de korting verschilt.")
        print("  Daarvoor zijn minstens 5 bets per markttype nodig.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
