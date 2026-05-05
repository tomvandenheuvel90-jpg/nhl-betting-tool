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
| `settings` | App-instellingen (bijv. startbankroll) — Supabase tabel `settings` (source of truth), `settings.json` als backup-cache |
| `bankroll_mutations` | Opnames en stortingen buiten bets om — Supabase tabel `bankroll_mutations` (source of truth), `bankroll_mutations.json` als backup-cache |

Lokale fallback-bestanden: `analyse_geschiedenis.json`, (favorieten/resultaten via Supabase of JSON).
Geschiedenis wordt gesnoeid na 7 dagen, tenzij gekoppeld aan een geplaatste weddenschap.

**Bankroll mutatie-functies (db.py):**
- `load_bankroll_mutations()` — Supabase eerst, lokale JSON als fallback. Synchroniseert Supabase-data naar de lokale JSON als backup-cache.
- `save_bankroll_mutation(bedrag, omschrijving, datum)` — schrijft naar Supabase + lokale JSON. Raised RuntimeError als beide falen. bedrag > 0 = storting, < 0 = opname.
- `delete_bankroll_mutation(id)` — verwijdert in Supabase + lokale JSON
- `get_bankroll_mutations_total()` — netto som van alle mutaties (defensief tegen None / niet-numerieke waarden)
- `_migrate_local_mutations_if_needed()` — kopieert eventuele lokale `bankroll_mutations.json`-rijen die nog niet in Supabase staan, eenmalig per sessie

**Supabase-tabellen die NODIG zijn voor de bankroll** (Streamlit Cloud-fix):
```sql
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS bankroll_mutations (
    id           TEXT PRIMARY KEY,
    datum        TEXT NOT NULL,
    bedrag       REAL NOT NULL,
    omschrijving TEXT
);
```
Zonder deze tabellen verdwijnen startbankroll en mutaties bij elke Streamlit-herstart, omdat Streamlit Cloud een ephemeral filesystem heeft. De UI in de Bankroll tab toont een rode banner als er settled bets zijn maar de startbankroll op €0 staat — dat is het signaal dat de tabellen nog niet bestaan.

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
- **Bankroll mutaties**: opnames en stortingen zijn registreerbaar via `💸 Opname of storting registreren` expander in de 📊 Bankroll tab. Sinds de Streamlit-Cloud fix: opgeslagen in Supabase tabel `bankroll_mutations` (source of truth), met `bankroll_mutations.json` als backup-cache. Saldo-berekening is `start + mutaties + P&L − open inzet`.
- **Bankroll persistentie-fix (Streamlit Cloud)**: settings + bankroll-mutaties worden nu altijd primair naar Supabase geschreven, zodat ze de ephemeral filesystem-resets van Streamlit Cloud overleven. `get_setting` heeft een process-cache zodat transient Supabase-fouten niet leiden tot een lege waarde. `set_setting` retourneert nu True/False zodat de UI fouten kan tonen. `save_bankroll_mutation` raised RuntimeError als noch Supabase noch lokale JSON werkt. De UI toont een rode banner met SQL-instructies als startbankroll op €0 staat terwijl er gesettlede bets zijn (= signaal dat de Supabase-tabellen `settings` of `bankroll_mutations` nog niet zijn aangemaakt).
- **Soccer Bet365 whitelist**: `is_soccer_bet365_market()` in `prompts.py`. Alleen markten op Bet365-voetbal worden doorgelaten: 1X2, Double Chance, BTTS, Over/Under, Handicap, Draw No Bet, Corners, Anytime Scorer, Multi Scorer (2+), Assist, Shots, Shots on Target, Keeper Saves. First Goalscorer en schoten (niet op goal) zijn **uitgesloten**.
- **SOCCER_COMPS uitgebreid**: van 8 naar ~30 competities inclusief Championship, EFL, Eredivisie, UCL, Conference League, etc. Gebruikt in `enrich_bet()` om voetbalbets correct te behandelen.
- **detect_sports_from_matches — 3-laags detectie** (commit `8b949d8`):
  1. Sport/competition veld: Championship, EFL, etc. → SOCCER. Onbekende gevulde waarde → ook SOCCER.
  2. Teamnaam-check: "FC", "United", "City", "Rovers", "Villa", etc. → SOCCER.
  3. Fallback `{"NHL","NBA","MLB"}` alleen als matches-lijst **leeg** is (was: altijd bij onherkende sport → triggerde NBA).
- **generate_auto_props veiligheidscheck**: lege sports-set → geen API-calls, melding tonen. Voorkomt NBA-spelerslookup bij voetbal-screenshots.
- **EXTRACT_PROMPT bets-sectie bijgewerkt**: voetbalcompetities (Championship, La Liga, etc.) als geldige sport-opties toegevoegd. Expliciet verbod op "NBA"/"NHL" voor voetbalspelers.
- **NBA hist_* veldnamen fix**: `sports/nba.py` geeft nu aliassen mee: `avg_points → hist_points_avg`, `avg_rebounds → hist_rebounds_avg`, etc. Scorer kon hiervoor geen seizoens-HR berekenen voor NBA props.
- **Combo stats NBA**: `scorer.py` herkent nu PRA (Points+Rebounds+Assists), RA, PA als combinatie-props. `_get_raw_and_line()` telt de losse waarden op; `_get_hist_lam()` gebruikt de juiste veldnaam.
- **Debug-sleutels fix**: `analysis.py` gebruikt `_dbg_raw`, `_dbg_traceback` etc.; `streamlit_app.py` gebruikt nu ook de `_dbg_` prefix (was zonder prefix → debug-info werd nooit getoond).
- **Feedback loop + Model Prestaties**: `db.py` slaat `rating` en `composite` op in zowel `favorieten` als `resultaten`. `analysis.py` geeft `opp_factor`, `reliability`, `no_season_data` mee in het verrijkte bet-object. `ui_components.py` toont een "Score opbouw" sectie in de prop-kaart. De 📊 Bankroll tab heeft een "🧠 Model Prestaties" sectie met EV-kalibratie, rating-tiers en per-sport bias.
- **Compacte prop-kaarten**: `render_bet_card()` in `ui_components.py` is volledig herschreven naar pure HTML. Geen `st.metric()` of `st.progress()` meer. Bevat: 4 stat-chips naast elkaar (LM HR / Sez HR / Odds / Sample), 4px composite-balk, score-opbouw pills (Linemate%, Seizoen%, Tegenstander, Betrouwbaarheid). Parameter `dimmed=bool` voor gedimd weergeven van gefilterde props.
- **Tijdzone fix**: `db.py` heeft een `_now_local()` helper die UTC+2 (CEST) gebruikt als fallback en een `TZ` env var ondersteunt. Alle `datetime.datetime.now()` calls vervangen.
- **EV-sortering fix**: sort-key `float(x.get("ev") or 0)` behandelde `ev=0.0` als None. Vervangen door `x.get("ev") if x.get("ev") is not None else -999.0` zodat de sortering altijd correct is.
- **Alle props tonen (ook negatieve EV)**: `_display_props = enriched` (was `enriched_ranked`). Props die de filter niet halen worden gedimd weergegeven (60% opacity). Zo zijn alle props van meerdere screenshots altijd globaal zichtbaar, gesorteerd van hoog naar laag EV.
- **Bet365 verificatie standaard uitgeschakeld**: checkbox `💰 Bet365 odds verificatie` in de Analyse-tab, standaard UIT. Linemate-odds worden gebruikt. API-code volledig intact. Schakel in voor eenmalige verificatie.
- **Odds API `sport=None` fix**: `(sport or "").upper()` in alle drie de functies in `sports/odds_api.py`. Voorkomt crash bij MLB/andere bets waarbij Claude Vision het sport-veld niet invult.
- **Parlay suggesties gesplitst per grootte**: `generate_parlay_suggestions()` in `analysis.py` retourneert nu maximaal 2 twee-leg parlays + 2 drie-leg parlays (was: top 3 gemengd). Elk parlay-object heeft `n_legs` en `same_team_warning` velden. UI toont ze gesplitst in blokken per grootte.
- **Same-team correlatie-penalty**: same-team combos werden voorheen volledig uitgesloten. Nu toegelaten maar met automatische odds-korting van −15% (`_SAME_TEAM_CORR_DISCOUNT = 0.85`), conform de SGP-penalty die bookmakers hanteren. UI toont een ⚠️ met uitleg.
- **Auto-parlay naar Parlay Builder**: de "Sla op" knop bij auto-parlay suggesties stuurt de legs nu naar de Parlay Builder tab (was: direct opslaan met hardcoded inzet €10). Gebruiker stelt inzet in en slaat op vanuit de Parlay Builder.
- **Open bet P&L fix**: `upsert_resultaat()` in `db.py` boekte bij `uitkomst="open"` een `winst_verlies = -inzet`. Nu is dat `0.0` — P&L wordt alleen geboekt bij gewonnen of verloren.
- **Shortlist P&L display fix**: caption in de Shortlist toont P&L alleen bij gesettlede bets (gewonnen/verloren). Bij open bets staat er "P&L: —".
- **ev_score None crash**: `float(_fav.get('ev_score', 0))` kan crashen als Supabase `null` retourneert. Vervangen door `float(_fav.get('ev_score') or 0)` op alle plekken in Shortlist en Dashboard.

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
- [x] Meerdere screenshots: alle props (incl. negatieve EV) worden nu getoond, gesorteerd op EV. Gefilterde props zijn gedimd weergegeven.
- [x] Soccer Bet365 whitelist: `is_soccer_bet365_market()` in `prompts.py`.
- [x] Bet365 verificatie standaard uitgeschakeld — Linemate-odds worden gebruikt.
- [x] Open bet P&L fix — `upsert_resultaat()` boekt nu `wl=0.0` voor open bets.
- [x] Auto-parlay stuurt nu naar Parlay Builder in plaats van direct opslaan met vaste inzet.
- [x] Parlay suggesties gesplitst in 2-leg en 3-leg, same-team correlatie-penalty −15%.
- [ ] **OPEN BUG — NBA-spelers bij voetbal-screenshots**: ondanks meerdere fixes (detect_sports_from_matches uitgebreid, fallback aangepast, teamnaam-detectie toegevoegd) blijft de app NBA-spelersdata ophalen bij Championship Flashscore screenshots. Nog niet volledig opgelost. Volgende stap: debug-output toevoegen zodat zichtbaar is welke sport/competition Claude Vision teruggeeft én wat detect_sports_from_matches retourneert. Mogelijk moet de Streamlit-app herstart worden na de laatste push.

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
- **Git push**: SSH naar github.com werkt niet vanuit de Claude-sandbox. Tom moet zelf pushen: `cd /Users/tomvandenheuvel/Documents/BetAnalyzer && git push origin main`. Na elke sessie controles via `git log --oneline -5`.
- **Streamlit herstarten na code-wijzigingen**: als de app al draait en er zijn nieuwe commits gepusht, moet Streamlit herstart worden (Ctrl+C → `streamlit run streamlit_app.py`) zodat de nieuwe Python-modules worden geladen.
- **NBA auto-props detectie**: `_nba_auto_props()` wordt alleen getriggerd als `detect_sports_from_matches()` "NBA" retourneert. Dit mag NOOIT gebeuren bij voetbal-screenshots. Zie open bug in Outstanding/TODO.
