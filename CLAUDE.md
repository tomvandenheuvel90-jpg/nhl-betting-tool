# BetAnalyzer — CLAUDE.md

## Project Overview
BetAnalyzer is een Streamlit-webapplicatie voor sportgok-analyse, gericht op MLB, NBA, NHL en voetbal.
De app helpt de gebruiker bij het analyseren van bets op basis van screenshots (Linemate, Flashscore),
berekent de Expected Value (EV) per prop, en beheert een bet slip / parlay builder.

**Eigenaar:** Tom van den Heuvel — niet-technisch, houd oplossingen eenvoudig en goed uitgelegd.

## Locatie
`/Users/tomvandenheuvel/Documents/BetAnalyzer/`

## Architecture

### Entry point
- **`streamlit_app.py`** (±1900 regels) — Enige entry point. Start met `streamlit run streamlit_app.py`.
  Bevat de volledige UI logica in 7 tabs:
  | Tab | Inhoud |
  |-----|--------|
  | 🏠 Dashboard | KPI-samenvatting, laatste analyse, snelkoppelingen |
  | 🔍 Analyse | Screenshot upload, Claude-analyse, prop-kaarten |
  | ⭐ Shortlist | Favorieten beheren, handmatig bet toevoegen |
  | 🎯 Parlay Builder | Props combineren, EV berekenen, parlays opslaan |
  | 📋 Geplaatste Bets | Overzicht per maand/week, uitkomst bijhouden, verwijderen |
  | 📊 Bankroll | P&L grafiek, per sport, Kelly Calculator, streaks |
  | 🗂️ Analyse Geschiedenis | Eerdere analyses, filteren op sport/categorie |

### Core analyse-modules
| Bestand | Doel |
|---------|------|
| **`analysis.py`** (777 r.) | Claude Vision API-calls (`extract_bets()`), HEIC→JPEG conversie, JSON-repair, auto-prop generatie per sport, Flashscore-integratie, Football-Data API form-fetching |
| **`match_analysis.py`** (±620 r.) | Sport-specifieke kansmodellen: NHL (Poisson), NBA (Normal dist.), MLB (Poisson + pitcher ERA), Soccer (Poisson + xG). Geeft EV + rating per optie terug. Bevat weighted blending, home/away splits, H2H-factor en soccer form multiplier (zie Win Probability Models). |
| **`scorer.py`** (276 r.) | Composite scorer: Linemate HR 35% + seizoen HR 35% + tegenstander 20% + betrouwbaarheid 10%. Functies: `composite_score()`, `ev()`, `rating()` |
| **`analyze_bets.py`** (200 r.) | CLI-tool voor standalone analyse (niet gebruikt in UI) |
| **`analyze_bets_v2.py`** (552 r.) | Verbeterde CLI-pipeline met Flashscore + opponent stats (niet gebruikt in UI) |

### Data / API-laag

**`sports/` map — sport-specifieke data-modules:**
| Module | API | Gratis? | Sleutel nodig? |
|--------|-----|---------|----------------|
| `sports/nhl.py` | NHL API + MoneyPuck CSV | Ja | Nee |
| `sports/nba.py` | nba_api (NBA.com) | Ja | Nee |
| `sports/mlb.py` | statsapi.mlb.com | Ja | Nee |
| `sports/soccer.py` | Football-Data.org | Gedeeltelijk | Ja (gratis tier: 10 req/min) |
| `sports/odds_api.py` | TheOddsAPI (Bet365 odds) | Nee (betaald) | Ja |
| `sports/cache.py` | In-memory cache met TTL | — | — |
| `sports/rate_limiter.py` | API rate-limiting per sport | — | — |
| `sports/moneypuck_local.py` | MoneyPuck lokale CSV-utilities, Poisson hit rate berekening | — | — |

**Toegevoegde functies per sports-module (recente uitbreidingen):**
| Module | Functie | Doel |
|--------|---------|------|
| `sports/nhl.py` | `get_team_split_last10_goals(abbrev, venue)` | Laatste 10 thuis- of uitwedstrijden, goals voor/tegen. Cache 2u. |
| `sports/nhl.py` | `get_h2h_results(home_abbrev, away_abbrev)` | Laatste 5 onderlinge duels (huidig + vorig seizoen). Cache 4u. |
| `sports/nba.py` | `get_team_split_last10_stats(team_name, venue)` | Laatste 10 thuis- of uitwedstrijden via TeamGameLog. Cache 2u. |
| `sports/nba.py` | `get_h2h_results(home_name, away_name)` | Laatste 5 H2H via TeamGameLog MATCHUP-kolom. Cache 4u. |
| `sports/mlb.py` | `_find_team_id(team_name)` | Zoekt team-ID op via standings API. |
| `sports/mlb.py` | `get_h2h_results(home_name, away_name)` | Laatste 5 H2H via schedule API. Cache 4u. |
| `sports/soccer.py` | `get_team_split_stats(team_id, venue)` | Laatste 10 thuis- of uitwedstrijden via `/teams/{id}/matches`. Cache 2u. |
| `sports/soccer.py` | `get_h2h_results(home_team_id, away_team_id)` | Laatste 5 H2H via matches-endpoint. Cache 4u. |

**Download-scripts (lokaal draaien om CSV's te vullen):**
- `download_mlb.py` → `mlb_data/{year}/hitters.csv`, `pitchers.csv`, `games.csv`
- `download_nba.py` → `nba_data/{year}/players.csv`, `games.csv`
- `download_moneypuck.py` → `moneypuck_data/` (per-game NHL stats)
- `download_football.py` → `football_data/` (JSON teamstats per competitie)

### Database-laag
**`db.py`** (±610 r.) — Abstractielaag: Supabase (primair) met lokale JSON-fallback.

| Tabel / bestand | Inhoud |
|-----------------|--------|
| `geschiedenis` | Analyse-sessies (top5, alle props JSON, parlay-suggesties) |
| `favorieten` | Bewaarde bets (speler, bet, odds, EV, sport, bet365_status) |
| `resultaten` | Geplaatste + afgeronde bets (inzet, uitkomst, P&L, datum) |
| `parlays` | Opgeslagen parlays (legs, gecombineerde odds, hit kans, EV) |
| `settings` | App-instellingen (bijv. startbankroll) — `settings.json` |
| `bankroll_mutations.json` | Opnames en stortingen buiten bets om (lokaal JSON) |

Lokale fallback-bestanden: `analyse_geschiedenis.json`, (favorieten/resultaten via Supabase of JSON).
Geschiedenis wordt gesnoeid na 7 dagen, tenzij gekoppeld aan een geplaatste weddenschap.

**Bankroll mutatie-functies (db.py):**
- `load_bankroll_mutations()` — lijst van opnames/stortingen
- `save_bankroll_mutation(bedrag, omschrijving, datum)` — voeg toe; bedrag > 0 = storting, < 0 = opname
- `delete_bankroll_mutation(id)` — verwijder op id
- `get_bankroll_mutations_total()` — netto som van alle mutaties

### UI / stijl-laag
| Bestand | Doel |
|---------|------|
| **`ui_components.py`** (499 r.) | Streamlit render-functies: `render_bet_card()`, `render_nhl_match_cards()`, `render_nba_match_cards()`, `render_mlb_match_cards()`, `render_soccer_match_cards()`, `render_top3()`, `render_flashscore()` |
| **`styles.py`** (328 r.) | Dark Pro CSS-thema (achtergrond #08081a, accent #7c3aed violet). Styling voor tabs, knoppen, inputs, dataframes, EV-kleuren |

### Prompts & configuratie
| Bestand | Doel |
|---------|------|
| **`prompts.py`** | Constanten: `EXTRACT_PROMPT` (Vision JSON-extractie), `FLASHSCORE_PROMPT` (NL-analyse), `SCENARIO_WEIGHTS` (3 analyse-scenario's), `_REF_ODDS` (referentie-odds per bet type), `SOCCER_COMPS`, `_NHL_TEAM_KEYWORDS` |
| **`prompts/analyse_prompt.txt`** | Legacy tekstprompt voor Flashscore (niet primair in gebruik) |
| **`moneypuck_app.py`** | Zelfstandige MoneyPuck viewer (los van de main app) |

### Assets & config
- `assets/banner.svg` — SVG-logo/banner voor de UI header
- `.streamlit/secrets.toml` — API-sleutels (Anthropic, Supabase, Football-Data, TheOddsAPI, Google Drive)
- `requirements.txt` — Afhankelijkheden: streamlit, anthropic, nba_api, pandas, pillow, pillow-heif, supabase, google-api-python-client, requests, flask

### Dataflow (vereenvoudigd)
```
Screenshot (Linemate / Flashscore)
        ↓
analysis.extract_bets()  →  Claude Vision API (claude-haiku-4-5)
        ↓
JSON: { bets: [...], matches: [...] }
        ↓
filter_and_rank_props()  →  enrich_bet()  →  scorer.composite_score()
        ↓
match_analysis.analyze_*()  →  Poisson / Normal distributie model
        ↓
ui_components.render_*()  →  Streamlit UI
        ↓
db.save_*()  →  Supabase (of lokale JSON-fallback)
```

---

## Sports Covered
- **NHL** (hockey) — Poisson model, MoneyPuck CSV, 3-way odds (regulation / OT / verlies)
- **NBA** (basketball) — Normal dist. model, nba_api, home court +3 pts
- **MLB** (baseball) — Poisson + pitcher ERA-weging, statsapi.mlb.com
- **Soccer / Voetbal** — Poisson + xG, Football-Data.org, EPL / La Liga / Bundesliga / Serie A / Ligue 1 / UCL

---

## Win Probability Models

### Weighted Blending (NHL + NBA)
Statistieken worden gecombineerd als gewogen gemiddelde van recente form en seizoensgemiddelden:

```
blended = last10 × 0.60 + season × 0.40
```

Fallback: als `last10` ontbreekt of 0 is, wordt uitsluitend het seizoensgemiddelde gebruikt.
Geïmplementeerd via de `_blend(season, last10)` helper in `match_analysis.py`.

### Home/Away Splits (NHL, NBA, Soccer)
Voor de thuisploeg worden de laatste 10 **thuis**wedstrijden gebruikt; voor de uitploeg de laatste 10
**uit**wedstrijden. Als er minder dan 5 venue-specifieke wedstrijden beschikbaar zijn, wordt de
algemene last-10 als fallback gebruikt.

```
_split_or_overall(split_dict, overall_dict, key)
→ split_dict[key]   als split_dict["last10_games"] >= 5
→ overall_dict[key] anders
```

MLB heeft geen splits (geen rolling per-game data beschikbaar via de gratis API).

### Head-to-Head Factor (alle 4 sporten)
Haalt de laatste 5 onderlinge duels op (huidig seizoen, aangevuld met vorig seizoen indien nodig).
Past de thuiswinst-kans aan op basis van het H2H win percentage:

| H2H win rate thuisploeg | Aanpassing |
|-------------------------|------------|
| > 60% | +3% op `p_home` |
| < 40% | −3% op `p_home` |
| 40–60% | geen aanpassing |

Minimaal 3 H2H wedstrijden vereist; bij minder wordt de factor overgeslagen.
Bij 3-way odds (NHL, Soccer): resterende kans wordt proportioneel herverdeeld over draw + away.
Bij 2-way odds (NBA, MLB): direct omgezet via `p_away = 1 - p_home`.

```python
_H2H_ADJ_PCT   = 0.03   # 3% maximale aanpassing
_H2H_MIN_GAMES = 3
```

### Soccer Form Multiplier
De recentste 5 wedstrijden (WWDLW-string) beïnvloeden de verwachte doelgemiddelden:

| Resultaat | Effect op gf_avg |
|-----------|-----------------|
| W | +0.04 |
| D | ±0.00 |
| L | −0.04 |

Gecombineerde aanpassing wordt afgetopt op ±0.12. Toegepast als multiplier:
`gf_avg *= (1 + form_multiplier)`

---

## Key Features Built
- MLB pitcher stat blending (seizoen CSV + huidige ERA, pitcher-gewicht 0.65)
- Dynamic NBA home court advantage (+3 verwacht puntverschil)
- Sidebar sport filters (per tab aan/uit-schakelbaar)
- NHL roster optimizations + 3-way odds
- EV-berekening met composite scorer (meerdere bug fixes toegepast)
- MLB run line corrections (±1.5 Poisson kans)
- **Weighted blend last-10 + seizoen** voor NHL en NBA: `last10 × 0.60 + season × 0.40`
- **Home/away splits**: thuisploeg gebruikt laatste 10 thuiswedstrijden, uitploeg laatste 10 uitwedstrijden (NHL, NBA, Soccer). Fallback naar overall last-10 bij <5 games.
- **Head-to-head factor**: ±3% aanpassing op thuiswinst-kans op basis van laatste 5 H2H resultaten, alle 4 sporten.
- **Soccer form string**: WWDLW-reeks beïnvloedt Poisson-model via gf_avg multiplier (W=+0.04, L=−0.04, cap ±0.12).
- **Player-lookup scoping**: `enrich_bet()` voert speler-API-calls **alleen** uit bij echte player props (shots, goals, hits, enz.). Team-niveau bets (Moneyline, Puck Line, Run Line, Spread, Totals, 1X2, enz.) triggeren géén roster- of spelersdata-opvraag — die analyse loopt uitsluitend via `analyze_*_matches()` met team-form data. Geldt voor alle vier sporten (NHL, NBA, MLB, Soccer).
- Parlay Builder: handmatig props toevoegen, hit rate optioneel (checkbox), form reset na toevoegen, sport onthouden
- Hit rate per parlay-leg is volledig optioneel — EV toont "—" als niet alle legs een HR hebben
- **Parlay settlement fix**: ✅ Gewonnen / ❌ Verloren op een parlay schrijft nu ook naar de `resultaten` tabel via `upsert_resultaat()` met id `parlay_{id}`. Hierdoor verschijnt de parlay in 📋 Geplaatste Bets en telt de P&L mee in de Bankroll.
- **Delete bet uit geschiedenis**: 🗑️ knop in 📋 Geplaatste Bets werkt voor alle bets (open, gewonnen, verloren). Bij verwijderen van een parlay-entry wordt de parlay in de `parlays` tabel automatisch teruggezet op status `"open"`, zodat hij opnieuw gesettled kan worden.
- **Handmatig bet invoerformulier — Bet type dropdown**: vrij tekstveld vervangen door dropdown (Player Prop / Match Result / Odds Boost / Other) met een vrij tekstveld "Details" eronder. Geldt voor zowel de ⭐ Shortlist als de 🎯 Parlay Builder invoerformulieren. Opgeslagen waarde: `"Player Prop — Anytime Goal Scorer"` (of alleen de categorie als Details leeg is).
- **Odds aanpasbaar bij bet plaatsen (Shortlist)**: inzet én odds staan naast elkaar bij het plaatsen van een bet vanuit de ⭐ Shortlist. Odds zijn vooringevuld vanuit de opgeslagen waarde maar kunnen worden aangepast. P&L berekening gebruikt de ingevoerde odds.
- **Per-screenshot extractie**: `extract_bets()` verwerkt elk geüpload screenshot in een aparte Claude API-call (was: alles in één call, leidde tot JSON-afkap bij >25 props). Resultaten worden gecombineerd en gedupliceert na afloop.
- **Blessure-check toggle**: `🩺 Blessure-check` checkbox in de Analyse-tab schakelt de NHL roster scan in/uit. Standaard **uitgeschakeld** (snellere analyse). Geïmplementeerd via `injuries_enabled` parameter in `generate_auto_props()` → `_nhl_auto_props()`.
- **Props transparantie**: na filtering toont de app hoeveel props zijn weggevallen op negatieve EV of klein sample, zodat de gebruiker begrijpt waarom niet alle geüploade props zichtbaar zijn.
- **Bankroll mutaties**: opnames en stortingen zijn registreerbaar via `💸 Opname of storting registreren` expander in de 📊 Bankroll tab. Opgeslagen in `bankroll_mutations.json`. Saldo-berekening is `start + mutaties + P&L`.

---

## Display Conventions
- **Altijd decimale odds** (Europees formaat, bijv. 1.85), nooit American moneyline
- **NHL altijd 3-way odds**: 🏠 Thuiswinst regulatie | 🔄 OT/gelijkspel | ✈️ Uitwinst regulatie
- **Ratings:** ✅ Sterk | ⚠️ Matig | ❌ Vermijd
- **EV display:** `+0.123` (positief) of `−0.456` (negatief), kleurgecodeerd

---

## Outstanding / TODO
- [ ] Bet type filter in Bankroll/Geplaatst tabs uitbreiden (bijv. filteren op Player Prop vs. Match Result)
- [ ] Parlay: leg-niveau odds aanpassen in opgeslagen parlays
- [ ] Automatisch duplicaten detecteren bij toevoegen van een bet
- [ ] Parlays die vóór de settlement-fix zijn opgeslagen staan nog niet in `resultaten` — eventueel handmatig herstellen via db.py
- [ ] Meerdere screenshots: props met negatieve EV worden gefilterd — overweeg optie "toon alles" zodat gebruiker ook negatieve EV props kan zien en zelf kan beslissen

---

## Development Notes
- **Eigenaar is niet-technisch** — houd oplossingen eenvoudig en goed uitgelegd
- **Breek geen bestaande functionaliteit** bij het toevoegen van features
- **Test EV-berekeningen zorgvuldig** — bugs zijn eerder opgetreden
- **Streamlit session state:** gebruik versioned widget-keys (`key=f"widget_{version}"`) om form-reset te forceren zonder page reload
- **Database:** db.py heeft altijd een lokale JSON-fallback; test bij wijzigingen beide paden
- **API-kosten:** TheOddsAPI is betaald en bijgehouden in `odds_api_usage.json`; Claude Haiku wordt gebruikt voor goedkope Vision-extractie
- **HEIC-ondersteuning:** pillow-heif is vereist voor iPhone-screenshots; zit in requirements.txt
- **Cache TTL:** splits/last-10 = 2u, H2H = 4u, seizoensdata = 6u — bewust kort gehouden zodat dagelijkse wedstrijden opgepikt worden
- **H2H fallback:** als er minder dan `_H2H_MIN_GAMES` (= 3) onderlinge duels zijn, wordt de H2H-factor stilletjes overgeslagen (geen error, geen aanpassing)
- **Split fallback:** als er minder dan 5 venue-specifieke wedstrijden beschikbaar zijn, gebruikt `_split_or_overall()` de algemene last-10; als die ook ontbreekt, valt `_blend()` terug op het seizoensgemiddelde
