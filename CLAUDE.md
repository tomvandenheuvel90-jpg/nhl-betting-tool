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
- **`streamlit_app.py`** (±1730 regels) — Enige entry point. Start met `streamlit run streamlit_app.py`.
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
| **`match_analysis.py`** (528 r.) | Sport-specifieke kansmodellen: NHL (Poisson), NBA (Normal dist.), MLB (Poisson + pitcher ERA), Soccer (Poisson + xG). Geeft EV + rating per optie terug. |
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

**Download-scripts (lokaal draaien om CSV's te vullen):**
- `download_mlb.py` → `mlb_data/{year}/hitters.csv`, `pitchers.csv`, `games.csv`
- `download_nba.py` → `nba_data/{year}/players.csv`, `games.csv`
- `download_moneypuck.py` → `moneypuck_data/` (per-game NHL stats)
- `download_football.py` → `football_data/` (JSON teamstats per competitie)

### Database-laag
**`db.py`** (553 r.) — Abstractielaag: Supabase (primair) met lokale JSON-fallback.

| Tabel / bestand | Inhoud |
|-----------------|--------|
| `geschiedenis` | Analyse-sessies (top5, alle props JSON, parlay-suggesties) |
| `favorieten` | Bewaarde bets (speler, bet, odds, EV, sport, bet365_status) |
| `resultaten` | Geplaatste + afgeronde bets (inzet, uitkomst, P&L, datum) |
| `parlays` | Opgeslagen parlays (legs, gecombineerde odds, hit kans, EV) |
| `settings` | App-instellingen (bijv. startbankroll) |

Lokale fallback-bestanden: `analyse_geschiedenis.json`, (favorieten/resultaten via Supabase of JSON).
Geschiedenis wordt gesnoeid na 7 dagen, tenzij gekoppeld aan een geplaatste weddenschap.

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
| **`scorer.py`** | Zie boven |

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

## Key Features Built
- MLB pitcher stat blending (seizoen CSV + huidige ERA, pitcher-gewicht 0.65)
- Dynamic NBA home court advantage (+3 verwacht puntverschil)
- Sidebar sport filters (per tab aan/uit-schakelbaar)
- NHL roster optimizations + 3-way odds
- EV-berekening met composite scorer (meerdere bug fixes toegepast)
- MLB run line corrections (±1.5 Poisson kans)
- Player-lookup scoping: `enrich_bet()` voert speler-API-calls **alleen** uit bij echte player props (shots, goals, hits, enz.). Team-niveau bets (Moneyline, Puck Line, Run Line, Spread, Totals, 1X2, enz.) triggeren géén roster- of spelersdata-opvraag — die analyse loopt uitsluitend via `analyze_*_matches()` met team-form data. Geldt voor alle vier sporten (NHL, NBA, MLB, Soccer).
- Parlay Builder: handmatig props toevoegen, hit rate optioneel (checkbox), form reset na toevoegen, sport onthouden
- Hit rate per parlay-leg is volledig optioneel — EV toont "—" als niet alle legs een HR hebben
- Geplaatste Bets tab: verwijderknop (🗑️) per weddenschap voor het verwijderen van duplicaten

---

## Display Conventions
- **Altijd decimale odds** (Europees formaat, bijv. 1.85), nooit American moneyline
- **NHL altijd 3-way odds**: 🏠 Thuiswinst regulatie | 🔄 OT/gelijkspel | ✈️ Uitwinst regulatie
- **Ratings:** ✅ Sterk | ⚠️ Matig | ❌ Vermijd
- **EV display:** `+0.123` (positief) of `−0.456` (negatief), kleurgecodeerd

---

## Outstanding / TODO
- [ ] Bet type filter in Bankroll/Geplaatst tabs uitbreiden
- [ ] Parlay: leg-niveau odds aanpassen in opgeslagen parlays
- [ ] Automatisch duplicaten detecteren bij toevoegen van een bet

---

## Development Notes
- **Eigenaar is niet-technisch** — houd oplossingen eenvoudig en goed uitgelegd
- **Breek geen bestaande functionaliteit** bij het toevoegen van features
- **Test EV-berekeningen zorgvuldig** — bugs zijn eerder opgetreden
- **Streamlit session state:** gebruik versioned widget-keys (`key=f"widget_{version}"`) om form-reset te forceren zonder page reload
- **Database:** db.py heeft altijd een lokale JSON-fallback; test bij wijzigingen beide paden
- **API-kosten:** TheOddsAPI is betaald en bijgehouden in `odds_api_usage.json`; Claude Haiku wordt gebruikt voor goedkope Vision-extractie
- **HEIC-ondersteuning:** pillow-heif is vereist voor iPhone-screenshots; zit in requirements.txt
