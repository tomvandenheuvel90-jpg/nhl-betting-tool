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
| **`grade_parlay_legs.py`** | Standalone script (draait dagelijks via GitHub Actions, `.github/workflows/grade-parlay-legs.yml`) dat per-leg uitkomsten van opgeslagen parlays automatisch invult op basis van de sport-API's. Zie "Automatische parlay-leg beoordeling" onder Key Features Built. |

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

**Supabase-tabellen die NODIG zijn voor de bankroll** (Streamlit Cloud-fix). Draai dit volledige blok in de Supabase SQL Editor — het is idempotent (veilig om meerdere keren te draaien) en lost zowel het ontbreken van tabellen als RLS-blokkades definitief op:
```sql
-- 1. Tabellen aanmaken (idempotent)
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

-- 2. Row Level Security uitzetten (primaire fix tegen 42501-fout)
ALTER TABLE settings           DISABLE ROW LEVEL SECURITY;
ALTER TABLE bankroll_mutations DISABLE ROW LEVEL SECURITY;

-- 3. Permissive policy als VANGNET. Wordt alleen actief als RLS later
--    weer aan wordt gezet (bijv. via de Supabase Table Editor UI, die RLS
--    standaard inschakelt zodra je een tabel via '+ New table' aanmaakt
--    of bewerkt). Zorgt dat schrijven altijd mogelijk blijft voor anon.
DROP POLICY IF EXISTS "anon_full_access" ON settings;
CREATE POLICY "anon_full_access" ON settings
    FOR ALL TO anon, authenticated
    USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "anon_full_access" ON bankroll_mutations;
CREATE POLICY "anon_full_access" ON bankroll_mutations
    FOR ALL TO anon, authenticated
    USING (true) WITH CHECK (true);
```
Zonder deze SQL verdwijnen startbankroll en mutaties bij elke Streamlit-herstart, omdat Streamlit Cloud een ephemeral filesystem heeft. De UI in de Bankroll tab toont een rode banner als er settled bets zijn maar de startbankroll op €0 staat — dat is het signaal dat de tabellen ontbreken of RLS blokkeert.

**Verifiëren dat de fix gewerkt heeft**: open de 📊 Bankroll-tab → `⚙️ Bankroll instellingen` expander → klik op `🔬 Test Supabase verbinding`. Deze knop draait `db.supabase_probe_write("settings")` — een echte upsert + delete op de settings-tabel — en rapporteert exact welke stap faalt (verbinding, upsert, delete) plus de Postgres-foutcode. Bij 42501 is RLS nog steeds het probleem; bij 42P01 ontbreekt de tabel; bij `ok=True` is alles in orde.

**Belt + suspenders waarom**: het is bekend dat de Supabase Table Editor UI Row Level Security automatisch inschakelt zodra je een tabel aanmaakt of bewerkt. Alleen `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` draaien is daardoor fragiel: één UI-actie later staat het weer aan en is alle data onbereikbaar. De permissive policy in stap 3 zorgt dat schrijven blijft werken, zelfs als RLS later wordt geactiveerd.

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
- **Supabase RLS-fix (definitief)**: de schema-drift banner toont nu een complete SQL-fix die zowel `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` doet als een permissive policy aanmaakt (`anon_full_access` voor `anon, authenticated`). Achtergrond: de Supabase Table Editor UI zet RLS standaard AAN als je via '+ New table' een tabel maakt, en houdt dat ook bij bij latere edits. Alleen `DISABLE ROW LEVEL SECURITY` draaien lost het probleem niet permanent op; één UI-tweak later staat RLS weer aan en is alle data onbereikbaar (foutcode 42501: "new row violates row-level security policy"). De permissive policy is een vangnet voor exact dat scenario. Plus: nieuwe diagnose-knop `🔬 Test Supabase verbinding` in de Bankroll-tab (`⚙️ Bankroll instellingen` expander) roept `db.supabase_probe_write("settings")` aan — die doet een echte upsert + delete op de settings-tabel en rapporteert exact welke stap (verbinding/upsert/delete) faalt plus de Postgres SQLSTATE-code, met actionable hint per code.
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
- **Team-fix (commits `8418a98`/`8bc2e92`) teruggedraaid — root cause gevonden**: die fix maakte spelersnamen juist ónduidelijk op de prop-kaarten. Root cause: `EXTRACT_PROMPT` in `prompts.py` instrueerde Claude Vision dat het `"team"`-veld ook "de naam van de speler zelf" mocht bevatten — dit verwarde het Vision-model (vooral op drukke Linemate/Bet365-screenshots) over welk stukje tekst de speler was en welk het team, waardoor het `"player"`-veld zelf onbetrouwbaar werd. Extra bijwerking: de verplichte "⚠️ Team ontbreekt"-waarschuwing + tekstinvoer die op élke prop-kaart zonder team verscheen (vrijwel elke Linemate-prop) maakte de kaarten onoverzichtelijk. Fix: (1) `prompts.py` — `"team"` mag NOOIT de spelersnaam bevatten, `"player"` expliciet als leidend veld benoemd; `match_home`/`match_away` (de wedstrijd, al zichtbaar op de screenshot) is nu de primaire, betrouwbare context-bron. (2) `analysis.py enrich_bet()` — defensieve guard die een `team`-waarde die op de spelersnaam lijkt (gelijk aan of bevat, bij afkortingen >4 tekens) altijd negeert, zodat een extractiefout nooit doorsijpelt naar sport-API lookups. (3) `ui_components.py` — het verplichte team-invoerveld + waarschuwing volledig verwijderd (niet nodig, wedstrijd staat al in de caption); caption toont nu altijd `match_home vs match_away` zodra bekend, ook als team/opponent los niet bepaald zijn.
- **Automatische parlay-leg beoordeling**: parlays worden vaak in bulk goed-/afgekeurd (✅ Gewonnen / ❌ Verloren op het dashboard), waardoor de onderlinge legs zelden individueel beoordeeld werden — waardevolle data over succesrate per sport/odds-range ging zo verloren. Nieuw: `grade_parlay_legs.py` vult per leg automatisch `"geraakt"`/`"gemist"` in `legs_json` in, op basis van dezelfde sport-API's en bet-type parsing (`scorer._get_raw_and_line()`) als de EV-berekening. Expliciete scope-keuzes (bevestigd door Tom): (1) **alleen** individuele legs worden ingevuld — de hele parlay settlen (✅/❌ op het dashboard) blijft altijd een bewuste handmatige actie, dat vindt hij zelf leuk om te doen; (2) legs die niet automatisch te bepalen zijn (speler niet gevonden, onbekend bet-type, wedstrijd nog niet gespeeld, datum buiten opgehaald API-bereik) blijven op `"open"` staan — het script gokt nooit. Technisch: (a) elke parlay-leg krijgt nu een `game_date` bij aanmaak (drie plekken in `streamlit_app.py`: vanuit Shortlist via `_fav_game_date()`, handmatig formulier via nieuwe datepicker, quick-add vanuit analyse/geschiedenis); oudere legs zonder `game_date` vallen terug op de plaatsingsdatum van de parlay zelf. (b) `sports/nba.py`, `mlb.py`, `nhl.py`, `soccer.py` — `get_player_stats()` geeft nu ook een `"dates"`-lijst terug (ISO `YYYY-MM-DD`), index-aligned met de `raw_*`-lijsten, zodat het script de juiste game kan opzoeken. (c) Nieuw veld `legs_auto_json` in `db.py` (`load_parlays`/`save_parlay`/`update_parlay`) volgt exact hetzelfde Supabase schema-drift-tolerante patroon als `legs_json` (probeer volledige rij, val terug zonder de kolom bij fout) — houdt bij wanneer een leg automatisch beoordeeld is, getoond als 🤖-badge naast de leg in "Opgeslagen Parlays"; badge verdwijnt zodra Tom een leg handmatig overschrijft. **Bekende beperking**: voetbal-legs zijn alleen te beoordelen voor doelpunten-props (`raw_goals`) — shots/corners/etc. zijn niet beschikbaar via de gratis football-data.org tier, dus die blijven altijd op "open" staan voor handmatige review. **Supabase**: kolom `legs_auto_json` is toegevoegd aan de `parlays`-tabel (bevestigd door Tom, `ALTER TABLE parlays ADD COLUMN IF NOT EXISTS legs_auto_json TEXT;` succesvol gedraaid) — de 🤖-badge is dus persistent.
  - **Scheduling: GitHub Actions, niet de Cowork scheduled task.** Eerste opzet gebruikte een Cowork scheduled task (`grade-parlay-legs`, cron `0 8 * * *`). Bij het testen bleek de Cowork-sandbox **geen algemene internettoegang** te hebben — elke externe host (inclusief `google.com`) gaf een `403 Forbidden` terug van de proxy, dus Supabase en alle vier de sport-API's waren onbereikbaar. Die Cowork-taak is daarom **gepauzeerd** (niet verwijderd) in plaats van verwijderd, zodat de geschiedenis bewaard blijft. In plaats daarvan draait de dagelijkse beoordeling nu via **GitHub Actions**: `.github/workflows/grade-parlay-legs.yml`, cron `9 6 * * *` (06:09 UTC = 08:09 CEST; in de winter, CET, draait hij om 07:09 lokale tijd — bewust zo gelaten voor eenvoud, kost geen data). Ook handmatig te starten via de "Run workflow"-knop op de Actions-tab in GitHub (`workflow_dispatch`). Vereist drie repo-secrets in GitHub (Settings → Secrets and variables → Actions): `SUPABASE_URL`, `SUPABASE_KEY`, `FOOTBALL_DATA_TOKEN`. `grade_parlay_legs.py`'s `_load_secrets()` leest deze bij voorkeur uit environment variables (zoals GitHub Actions ze aanlevert); is er geen env var gezet, dan valt het script terug op `.streamlit/secrets.toml` (voor lokaal/handmatig draaien op zijn Mac) — beide paden zijn los getest.
- **Sitesnelheid — database-caching + parallelle screenshot-analyse**: Tom ervoer algehele traagheid (uploaden, screenshot-analyse, "verwerken"-klik). Root cause 1: Streamlit voert bij élke klik/interactie de volledige `streamlit_app.py` opnieuw uit, inclusief de body van élke tab (`st.tabs()` rendert alleen de zichtbare tab, maar voert de code van alle tabs uit) — en `db.load_favorieten()`/`load_resultaten()`/`load_parlays()` deden tot nu toe bij élke aanroep een live Supabase-round-trip, zonder caching. Resultaat: 10+ overbodige Supabase-calls per klik, waar dan ook in de app. Fix: nieuwe lichte TTL-cache in `db.py` (`_TTL_CACHE`, `_ttl_get()`/`_ttl_set()`/`_ttl_clear()`, 60 seconden) rond deze drie `load_*`-functies. Elke schrijffunctie die deze tabellen raakt (`add_favoriet`, `remove_favoriet`, `upsert_resultaat`, `remove_resultaat`, `save_parlay`, `update_parlay`, `delete_parlay`) roept `_ttl_clear()` aan op elk succes-pad, zodat eigen wijzigingen altijd direct zichtbaar zijn — alleen de overbodige *herhaal*-reads binnen 60 seconden worden overgeslagen. Root cause 2: `analysis.py extract_bets()` verwerkte geüploade screenshots sequentieel (één Claude Vision-call per screenshot, na elkaar) — bij bijv. 4 screenshots duurde de analyse dus 4x zo lang. Fix: screenshots worden nu gelijktijdig verwerkt via `ThreadPoolExecutor` (max 5 tegelijk), resultaten worden achteraf weer op oorspronkelijke volgorde gezet zodat de output identiek blijft aan de sequentiële versie. De per-screenshot logica zelf (splitsen bij hoge afbeeldingen, JSON-parse/repair, fallback op helften bij extreem lange screenshots) is ongewijzigd — dit was destijds bewust per-screenshot gemaakt om JSON-afkap bij >25 props te voorkomen, dat blijft zo; alleen de volgorde waarin de API-calls verstuurd worden is nu parallel i.p.v. sequentieel. Bij een fout in één screenshot faalt de hele batch nog steeds in zijn geheel (zelfde "alles-of-niets"-gedrag als voorheen, bewust niet veranderd). Functioneel getest (cache-hit/invalidatie op alle drie tabellen, parallelle volgorde-behoud, foutafhandeling) vóór oplevering.
- **Parlay-leg settlement — batchverwerking + open-legs zichtbaar**: Tom moest voorheen bij élke los aangepaste leg-status wachten op een page reload, en kon niet zien of een al-afgehandelde parlay nog een open ("nog niet beoordeeld") leg had zonder de rij open te klikken. Fix 1 (batch): de leg-statussen in "Opgeslagen Parlays" (`tab_parlay`) staan nu in een `st.form()` — alle wijzigingen worden pas opgeslagen (recompute odds + `db.update_parlay()` + auto-settle-cascade) na klikken op de nieuwe "✅ Verwerken"-knop, in plaats van direct bij elke los gewijzigde dropdown. Fix 2 (zichtbaarheid): nieuwe centrale helpers `_parlay_leg_status(leg, leg_status, idx)` en `_parlay_open_leg_count(legs, leg_status)` in `streamlit_app.py` (ondersteunen beide historische legs_json-sleutelformaten, `"i_player_bettype"` en `"player_bettype"`) tellen hoeveel legs nog op `"open"` staan. Een `⏳ N nog te beoordelen` (of `✅ alle legs beoordeeld`) badge verschijnt nu op twee plekken: het expander-label in "Opgeslagen Parlays" én — dit was de kern van Tom's vraag — direct naast de speler/team-titel van elke parlay-rij in 📋 Geplaatste Bets, óók als de parlay als geheel al gewonnen/verloren is gemarkeerd. Geen extra klik meer nodig om te zien dat er nog een leg wacht op beoordeling.
- **Bugfix "Verwerken lijkt niets te doen"**: root cause was dat `st.expander()` in "Opgeslagen Parlays" geen `expanded=`-state had — na de `st.rerun()` die op "✅ Verwerken" volgt klapte de rij altijd dicht, waardoor de bijgewerkte `⏳`-badge niet zichtbaar was en het leek alsof er niks gebeurd was (de data was wél degelijk opgeslagen). Fix: `st.session_state["_prl_expanded_id"]` onthoudt welke parlay-rij open staat en wordt gezet zodra op "✅ Verwerken" wordt geklikt; de rij blijft daardoor open na de rerun. Ook een `st.toast()`-bevestiging toegevoegd ("✅ Leg-statussen bijgewerkt." / "ℹ️ Geen wijzigingen om te verwerken.") zodat direct duidelijk is dat de klik geregistreerd is.
- **Auto-mark legs op "geraakt" bij goedkeuren als Gewonnen**: als een hele parlay wordt goedgekeurd als "Gewonnen" is het logisch dat alle (niet-void) legs zijn geraakt — dat hoeft niet meer los per leg bevestigd te worden. Nieuwe helper `_mark_all_legs_geraakt(legs, leg_status)` in `streamlit_app.py` zet alle legs op `"geraakt"`, behalve legs die al expliciet op `"void"` staan (die blijven void). Toegepast op alle drie de plekken waar een parlay als Gewonnen gemarkeerd kan worden: (1) 🏠 Dashboard "✅ Win"-knop bij open weddenschappen, (2) 🎯 Parlay Builder "✅ Gewonnen"-knop in Opgeslagen Parlays, (3) 📋 Geplaatste Bets inline bewerk-formulier (as Uitkomst op "gewonnen" wordt gezet). Bij "❌ Verloren"/"Loss" blijft per-leg beoordeling nodig (bewust — één leg kan gemist zijn terwijl de rest wél raak was, die data is waardevol voor de model-statistieken).
- **Eenmalige backfill-knop voor oude gewonnen parlays**: in "Opgeslagen Parlays" staat nu een 🔧-expander "Eenmalig: oude gewonnen parlays bijwerken naar 'geraakt'" met een "Bijwerken"-knop. Loopt alle parlays door met `uitkomst == "gewonnen"` (parlays van vandaag worden overgeslagen, op Tom's verzoek — geen tijd verspillen aan het met terugwerkende kracht opnieuw doorlopen van net-geteste data) en past `_mark_all_legs_geraakt()` toe. Toont na afloop hoeveel parlays zijn bijgewerkt.
- **Bugfix: backfill-knop meldde "39 parlays bijgewerkt" maar er veranderde zichtbaar niets**: root cause was een sleutel-mismatch in `legs_json`. Parlays aangemaakt via `screenshot_import.py` slaan leg-statussen op met een index-voorvoegsel (`"0_Speler_Bettype"`), terwijl `_mark_all_legs_geraakt()`, het handmatige leg-statusformulier in de Parlay Builder én `grade_parlay_legs.py` altijd naar de vlakke sleutel (`"Speler_Bettype"`) schreven. `_parlay_leg_status()` (die de ⏳-badge en leg-iconen vult) checkt de index-sleutel eerst — dus elke schrijfactie naar de vlakke sleutel werd door de UI genegeerd, terwijl de (ongewijzigde) index-sleutel bleef getoond. Vandaar: de backfill "lukte" op papier (`_bf_touched` telde op, er werd wél iets weggeschreven) maar zichtbaar veranderde er niets. Fix: nieuwe centrale helper `_parlay_leg_key(leg, legs_json, idx)` in `streamlit_app.py` is nu de enige plek die de sleutel bepaalt (index-vorm als die al bestaat, anders vlakke vorm), gebruikt door zowel lees- als schrijfpaden: `_parlay_leg_status()`, `_mark_all_legs_geraakt()`, het handmatige leg-statusformulier (incl. odds-herberekening en auto-settle-check), én `_find_leg_status()` in 📊 Bankroll → Model Prestaties. Equivalente `_leg_key()`-helper toegevoegd aan `grade_parlay_legs.py` — de dagelijkse GitHub Actions-job liep tegen hetzelfde probleem aan bij screenshot-geïmporteerde parlays. **Actie voor Tom**: de 🔧 backfill-knop nogmaals indrukken, dit keer schrijft hij naar de sleutel die de UI ook echt leest. Wees-geworden vlakke-sleutel-entries van de vorige mislukte run blijven onschuldig ongebruikt in `legs_json` staan (index-sleutel heeft altijd voorrang).
- **Performance-diagnose (nog niet geïmplementeerd)**: de resterende traagheid komt niet van onnodige database- of API-calls (die zijn al gecached/geparallelliseerd, zie hierboven) maar van Streamlit's architectuur: elke klik/interactie voert het volledige ~3500-regels-tellende script opnieuw uit, inclusief alle ~87 widgets (buttons, selectboxes, forms, expanders) over alle 7 tabs heen, ook al is er maar één tab zichtbaar en is er maar één widget gewijzigd. `streamlit>=1.35` (gepind in requirements.txt) ondersteunt `st.fragment()`, waarmee specifieke UI-secties (bijv. de "Opgeslagen Parlays"-lijst) los kunnen herladen zonder de rest van het script opnieuw te draaien — dit is nog niet toegepast. Bewust nog niet doorgevoerd deze sessie: een `st.fragment`-refactor raakt gedrag van forms/session-state door de hele app en verdient een aparte, voorzichtige sessie met testen per tab, in plaats van een haastige wijziging in een productie-app die dagelijks gebruikt wordt.
- **Bugfix (2e ronde): "Verwerken"/"Gewonnen" meldden succes maar leg-status bleef op "nog te beoordelen" staan — écht opgelost**: de sleutel-mismatch-fix hierboven loste het probleem bij oude, via `screenshot_import.py` aangemaakte parlays op, maar Tom meldde daarna hetzelfde symptoom op een NIEUWE, handmatig toegevoegde parlay (dus zonder sleutel-mismatch). Root cause was iets anders en zat in `db.update_parlay()` zelf: als de Supabase-update met `legs_json`/`legs_auto_json` een exception gooide (om welke reden dan ook), viel de functie stilzwijgend terug op een retry ZONDER die twee velden — bedoeld voor het geval de kolommen nog niet bestonden. Maar het leg-statusformulier ("✅ Verwerken") stuurt updates die **uitsluitend** uit `legs_json`/`legs_auto_json` bestaan; na het verwijderen van die velden bleef er een leeg dict `{}` over, dus de fallback deed helemaal niets — geen Supabase-call, geen fout, gewoon een no-op. De functie retourneerde daarna alsnog stilzwijgend "succes" (geen exception, geen foutmelding), waarna de UI "✅ Leg-statussen bijgewerkt" toonde en `st.rerun()` deed. Bij de rerun laadt de pagina de leg-status opnieuw uit de (ongewijzigde) database, dus de ⏳-badge bleef terecht op "nog te beoordelen" staan — maar de dropdown zelf toonde vaak alsnog de net-gekozen waarde, omdat Streamlit's `st.selectbox(..., key=...)` de laatst gekozen waarde in `session_state` onthoudt en die na de eerste keer altijd voorrang geeft boven de `index=`-parameter, ongeacht wat er echt in de database staat. Dat verklaart precies wat Tom zag: dropdown op "geraakt", parlay op "GEWONNEN", maar de teller zegt nog steeds "3 nog te beoordelen" — de databasewrite was nooit aangekomen, alleen het scherm "onthield" de keuze. Fix: `db.update_parlay()` retourneert nu altijd een status-dict `{"ok": bool, "error": str|None}` in plaats van niets. Alle plekken die `legs_json` wegschrijven (de "✅ Verwerken"-form, de "✅ Gewonnen"/"❌ Verloren"-knoppen in Opgeslagen Parlays, het inline bewerk-formulier in 📋 Geplaatste Bets, en de eenmalige backfill-knop) checken dit nu en tonen een rode `st.error()` met de daadwerkelijke Supabase-foutmelding zodra de leg-status NIET daadwerkelijk is opgeslagen, in plaats van altijd een groen succesbericht te tonen. Zo wordt een mislukte write voortaan zichtbaar in plaats van dat de app een schijnbare bevestiging geeft terwijl er niets gebeurt. **Nog open**: de onderliggende reden waaróm de Supabase-write soms een exception gooit is nog niet bekend — dat vereist de exacte foutmelding, die nu wél zichtbaar wordt in de UI zodra het weer misgaat. Zodra Tom die rode foutmelding een keer ziet, kan de échte Supabase-oorzaak (kolomtype, rechten, netwerktimeout, etc.) verder onderzocht worden.
- **Wedstrijd/team-context bij parlay-legs**: Tom vroeg waarom hij bij een Dashboard-parlaykaart (bijv. "Josh Lowe"/"Bryce Harper") niet kon zien in welke wedstrijd deze spelers actief waren. Root cause: `match_home`/`match_away`/`team` werden wél door `analysis.py` op het verrijkte bet-object gezet, maar gingen verloren zodra een prop werd gekopieerd naar `st.session_state.parlay_legs` of naar `favorieten` — meerdere `.append()`/`db.add_favoriet()`-plekken in `streamlit_app.py` namen expliciet maar een klein deel van de velden over (player, sport, bet_type, odds, hit_rate, game_date) en sloegen team/wedstrijd-context simpelweg over. Fix: alle vier plekken waar een leg aan `parlay_legs` wordt toegevoegd (Shortlist-knop, handmatig invoerformulier, "Props uit analyse"-quicklist, Analyse Geschiedenis "🎯 Parlay"-knop) en de bijbehorende `db.add_favoriet()`-aanroep in Analyse Geschiedenis nemen nu ook `team`/`match_home`/`match_away` mee. Het handmatige invoerformulier heeft een nieuw optioneel tekstveld "Wedstrijd (optioneel)" gekregen (bijv. "Tampa Bay Rays @ New York Yankees") dat wordt gesplitst op " @ "/" vs "/" v " naar match_home/match_away. Nieuwe helper `_leg_context_suffix(leg)` in `streamlit_app.py` toont bij voorkeur `match_home vs match_away`, anders het team-veld. Deze suffix wordt nu getoond op: de "🧩 Jouw Parlay"-preview, de leg-lijst in "Opgeslagen Parlays", de Dashboard-parlaykaart (als kleine grijze regel onder elke leg), en de leg-lijst in 📋 Geplaatste Bets (verving de oudere, beperktere team-only suffix-logica). **Bekende beperking, expliciet gemeld aan Tom**: dit werkt alleen voor legs die VANAF NU worden toegevoegd — al opgeslagen parlays (zoals de Josh Lowe/Bryce Harper-parlay die de vraag triggerde) hebben nooit match_home/match_away vastgelegd op het moment van toevoegen, dus die informatie is niet met terugwerkende kracht te herstellen zonder de oorspronkelijke analyse-sessie.

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
- [x] Automatische parlay-leg beoordeling: `grade_parlay_legs.py` draait dagelijks via GitHub Actions (`.github/workflows/grade-parlay-legs.yml`), vult alleen individuele leg-uitkomsten in, parlay-settlement blijft handmatig. Zie Key Features Built.
- [x] Supabase-kolom `legs_auto_json` toegevoegd aan de `parlays`-tabel — 🤖-badge is persistent.
- [ ] GitHub Actions repo-secrets nog toevoegen: `SUPABASE_URL`, `SUPABASE_KEY`, `FOOTBALL_DATA_TOKEN` (Settings → Secrets and variables → Actions). Zonder deze secrets faalt de dagelijkse workflow-run. Zie Key Features Built voor uitleg waarom GitHub Actions i.p.v. de Cowork scheduled task.
- [ ] **OPEN BUG — NBA-spelers bij voetbal-screenshots**: ondanks meerdere fixes (detect_sports_from_matches uitgebreid, fallback aangepast, teamnaam-detectie toegevoegd) blijft de app NBA-spelersdata ophalen bij Championship Flashscore screenshots. Nog niet volledig opgelost. Volgende stap: debug-output toevoegen zodat zichtbaar is welke sport/competition Claude Vision teruggeeft én wat detect_sports_from_matches retourneert. Mogelijk moet de Streamlit-app herstart worden na de laatste push.
- [x] Parlay-leg "Verwerken" leek niets te doen — root cause was een dichtklappende `st.expander()` na `st.rerun()`, opgelost via `st.session_state["_prl_expanded_id"]`. Zie Key Features Built.
- [x] Auto-mark alle legs op "geraakt" bij goedkeuren van een parlay als Gewonnen (Dashboard/Parlay Builder/Geplaatste Bets). Zie Key Features Built.
- [x] Eenmalige backfill-knop voor oude gewonnen parlays. Zie Key Features Built.
- [x] Backfill-knop meldde "39 bijgewerkt" maar niets veranderde zichtbaar — sleutel-mismatch (`idx_player_bettype` vs. `player_bettype` in `legs_json`) opgelost via centrale `_parlay_leg_key()`-helper, ook toegepast in `grade_parlay_legs.py`. Knop moet nog een keer ingedrukt worden om de al aanwezige oude data alsnog correct bij te werken. Zie Key Features Built.
- [x] **2e ronde**: zelfde symptoom bleef optreden op een NIEUWE, niet-legacy parlay na bovenstaande fix. Écht root cause: `db.update_parlay()` viel bij een Supabase-exception stil terug op een update zonder `legs_json`/`legs_auto_json` — als de update ALLEEN uit die velden bestond (zoals bij "✅ Verwerken") was die fallback een lege no-op die zonder foutmelding "succes" retourneerde. Fix: `update_parlay()` geeft nu `{"ok", "error"}` terug; alle schrijfplekken (Verwerken-form, Gewonnen/Verloren-knoppen, Geplaatste Bets inline-edit, backfill-knop) tonen nu een rode foutmelding als de leg-status niet echt is opgeslagen. De onderliggende Supabase-foutoorzaak is nog niet bekend — wordt nu wél zichtbaar zodra hij weer optreedt. Zie Key Features Built.
- [x] **Wedstrijd/team-context bij parlay-legs**: `team`/`match_home`/`match_away` gingen verloren bij het toevoegen van een prop aan een parlay of shortlist. Alle 4 `parlay_legs.append()`-plekken + de `db.add_favoriet()`-aanroep in Analyse Geschiedenis nemen deze velden nu mee; nieuwe helper `_leg_context_suffix()` toont de wedstrijd op alle relevante plekken (Jouw Parlay, Opgeslagen Parlays, Dashboard-kaart, Geplaatste Bets). Werkt alleen voor nieuw toegevoegde legs, niet met terugwerkende kracht. Zie Key Features Built.
- [ ] **Performance — st.fragment-refactor**: gediagnosticeerd maar nog niet geïmplementeerd. `st.fragment()` (beschikbaar sinds Streamlit 1.33, app gebruikt >=1.35) zou specifieke secties (bijv. "Opgeslagen Parlays") los kunnen laten herladen i.p.v. het hele ~3500-regel script bij elke klik. Bewust uitgesteld tot een aparte sessie met ruimte om per tab te testen — dit raakt form/session-state-gedrag door de hele app heen.

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
