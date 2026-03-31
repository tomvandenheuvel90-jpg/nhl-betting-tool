"""
prompts.py — BetAnalyzer constanten, prompts en scenario-configuratie
"""

# ─── Sporten ──────────────────────────────────────────────────────────────────

SOCCER_COMPS = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}

_NHL_TEAM_KEYWORDS = {
    "maple leafs", "bruins", "canadiens", "senators", "sabres", "red wings",
    "panthers", "lightning", "hurricanes", "capitals", "rangers", "islanders",
    "devils", "flyers", "penguins", "blue jackets", "blackhawks", "predators",
    "blues", "wild", "jets", "oilers", "flames", "canucks", "ducks", "kings",
    "sharks", "golden knights", "kraken", "avalanche", "stars", "coyotes",
    "canes", "caps", "leafs", "habs",
    "tor", "bos", "mtl", "ott", "buf", "det", "fla", "tbl", "car", "wsh",
    "nyr", "nyi", "njd", "phi", "pit", "cbj", "chi", "nsh", "stl", "min",
    "wpg", "edm", "cgy", "van", "ana", "lak", "sjs", "vgk", "sea", "col",
    "dal", "ari", "uta",
}

# Referentie-odds voor auto-gegenereerde props (geen Linemate)
_REF_ODDS = {
    "shots":       1.85,
    "anytime":     2.50,
    "points":      1.85,
    "rebounds":    1.85,
    "assists":     1.90,
    "threes":      2.00,
    "hits":        1.80,
    "total_bases": 1.85,
    "strikeouts":  1.90,
}

# ─── Scenario configuratie ────────────────────────────────────────────────────

SCENARIO_LABELS = {
    1: "📊 Analyse op basis van historische data (geen Linemate)",
    2: "📊 Analyse op basis van Linemate + historische data (gecombineerd)",
    3: "📊 Analyse op basis van Linemate data",
}

SCENARIO_WEIGHTS = {
    1: (0.00, 0.70),   # (linemate_weight, season_weight)
    2: (0.42, 0.28),
    3: (0.35, 0.35),
}

# ─── Claude model ─────────────────────────────────────────────────────────────

EXTRACT_MODEL = "claude-haiku-4-5"

# ─── Extractie prompt ─────────────────────────────────────────────────────────

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

   BELANGRIJK: Extraheer ALLE spelers en props die zichtbaar zijn in de afbeelding,
   ook onderaan de lijst. Scroll mentaal door de hele afbeelding. Mis geen enkele prop.

2. "matches": ALLE wedstrijden zichtbaar in de screenshot. Elk item:
   - "home_team": volledige naam thuisploeg (string), bijv. "Florida Panthers"
   - "away_team": volledige naam uitploeg (string), bijv. "Minnesota Wild"
   - "sport": "NHL", "NBA", "MLB", of voetbalcompetitie zoals "EPL"
   - "competition": competitienaam indien zichtbaar (bijv. "NHL", "Premier League"), of null
   - "time": aanvangstijd indien zichtbaar (bijv. "00:00" of "01:00"), of null
   - "date": datum indien zichtbaar (bijv. "2025-03-25"), of null
   - "status": "gepland", "bezig" of "afgelopen"
   - "score": score indien zichtbaar (bijv. "2-1"), of null
   - "screenshot_odds": drie odds zichtbaar naast de wedstrijd als object
       {"home": 3.20, "draw": 4.20, "away": 1.95} of null als niet zichtbaar
   - "home_form": laatste 5 resultaten thuisploeg (bijv. "WWDLW"), of null
   - "away_form": idem voor uitploeg, of null
   - "h2h": korte H2H samenvatting indien zichtbaar (bijv. "Arsenal won 3/5"), of null

   LET OP: Een Flashscore NHL-overzichtsscherm toont MEERDERE wedstrijden met teamlogos
   en drie odds ernaast. Extraheer ELKE wedstrijd als apart item. Mis geen enkele wedstrijd.

Als er geen Linemate screenshots zijn, geef dan een lege array voor "bets".
Als er geen Flashscore screenshots zijn, geef dan een lege array voor "matches".
Geef ALLEEN het JSON object terug, geen andere tekst.
"""

# ─── Flashscore / analyse prompts ─────────────────────────────────────────────

FLASHSCORE_PROMPT = """
Je bent een expert sportsbetting analist. Analyseer de volgende wedstrijden en props in het Nederlands.

## WEDSTRIJDEN (Flashscore — verrijkt met API-data waar beschikbaar)
{matches_json}

## PROPS (Linemate — al gescoord)
{bets_json}

## INSTRUCTIES
- Als "home_form" of "away_form" gevuld zijn (bijv. "WWDLL"): gebruik deze data.
- Als form data null is maar je wel teamnamen hebt: geef aan "Beperkte data beschikbaar"
  en analyseer op basis van competitiecontext en bekende teamprestaties.
- Schrijf NOOIT "GEEN DATA" — altijd een redenering geven, ook bij beperkte data.
- Wees specifiek: noem altijd beide teamnamen en competitie.

## STAP 2 — FLASHSCORE ANALYSE
Geef een scoretabel voor de wedstrijden:
| Wedstrijd | Thuis vorm | Uit vorm | H2H | Advies |
|---|---|---|---|---|

Daarna: **Top 3 wedstrijden** om op te focussen, met 1-zin uitleg per wedstrijd.
Bij beperkte data: markeer met ⚠️ en geef een contextuele redenering.

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
