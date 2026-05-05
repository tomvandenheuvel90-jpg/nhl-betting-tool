#!/usr/bin/env python3
"""
Database abstraction voor Bet Analyzer.

Primair  : Supabase (persistente cloud opslag)
Fallback : lokale JSON bestanden (data verdwijnt na Streamlit herstart)

Tabellen in Supabase (maak aan via SQL Editor):

    CREATE TABLE geschiedenis (
        id TEXT PRIMARY KEY,
        datum TEXT,
        tijd TEXT,
        top5_json TEXT
    );

    CREATE TABLE favorieten (
        id TEXT PRIMARY KEY,
        datum TEXT,
        speler TEXT,
        bet TEXT,
        odds REAL,
        ev_score REAL,
        sport TEXT,
        bet365_status TEXT
    );

    CREATE TABLE resultaten (
        id TEXT PRIMARY KEY,
        datum TEXT,
        speler TEXT,
        bet TEXT,
        odds REAL,
        inzet REAL,
        uitkomst TEXT,
        winst_verlies REAL,
        sport TEXT,
        ev_score REAL
    );

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE bankroll_mutations (
        id           TEXT PRIMARY KEY,
        datum        TEXT NOT NULL,
        bedrag       REAL NOT NULL,
        omschrijving TEXT
    );

BELANGRIJK — Streamlit Cloud heeft een ephemeral filesystem.
Alles wat alleen in lokale JSON wordt geschreven (settings.json,
bankroll_mutations.json) verdwijnt bij elke redeploy/herstart.
Bankroll-data MOET dus naar Supabase. De lokale JSON blijft als
read-only fallback voor offline/dev gebruik en als backup-cache.
"""

import json
import hashlib
import datetime
import uuid
import time
import os
from pathlib import Path
from typing import Optional, List, Dict

# ─── Tijdzone helper ──────────────────────────────────────────────────────────
# Streamlit Cloud draait op UTC. Gebruik de TZ-omgevingsvariabele als die is
# ingesteld (bijv. "Europe/Amsterdam"), anders val terug op UTC+2 (CEST).

def _now_local() -> datetime.datetime:
    """Geeft de huidige tijd in de lokale tijdzone van de gebruiker."""
    tz_name = os.environ.get("TZ", "")
    if tz_name:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_name)
            return datetime.datetime.now(tz)
        except Exception:
            pass
    # Fallback: UTC+2 (Nederland zomer / CEST)
    tz_offset = datetime.timezone(datetime.timedelta(hours=2))
    return datetime.datetime.now(tz_offset)

# ─── Paden voor lokale JSON fallback ──────────────────────────────────────────

_BASE_DIR    = Path(__file__).parent
HISTORY_FILE = _BASE_DIR / "analyse_geschiedenis.json"
FAV_FILE     = _BASE_DIR / "favorieten.json"
RESULTS_FILE = _BASE_DIR / "resultaten.json"
HISTORY_DAYS = 7

# ─── Supabase state ───────────────────────────────────────────────────────────

_supabase     = None
_using_supabase = False

_SB_RETRIES = 3
_SB_DELAY   = 1.0  # seconden tussen pogingen

# ─── Schema drift detectie ────────────────────────────────────────────────────
# Wanneer Supabase een upsert weigert omdat een kolom ontbreekt, vallen we
# terug op een beperktere `row_basic`. Die fallback is stil, waardoor
# schema-drift (de Python-code kent een kolom die nog niet in de database is
# aangemaakt) lastig te debuggen is — velden worden ogenschijnlijk opgeslagen
# maar komen nooit terug. We verzamelen meldingen in een module-level set
# zodat streamlit_app.py ze één keer per sessie kan tonen.

_schema_drift_notes: set = set()


def _note_schema_drift(table: str, dropped_cols: tuple, exc: Exception) -> None:
    """Leg vast dat een Supabase-upsert op `table` is mislukt of teruggevallen.

    Postgres-foutcodes worden gebruikt om de juiste oorzaak te tonen:
    - 42501  → Row Level Security blokkeert (geen policy voor anon)
    - 42P01  → tabel bestaat niet
    - 42703  → kolom bestaat niet (echte schema-drift)
    Andere fouten worden generiek getoond zonder valse claims.
    """
    # Probeer de Postgres-foutcode te bemachtigen (supabase-py exceptions
    # hebben vaak een .code attribuut, of een dict met 'code' in de message)
    code = ""
    try:
        code = getattr(exc, "code", "") or ""
        if not code and isinstance(getattr(exc, "args", [None])[0], dict):
            code = exc.args[0].get("code", "") or ""
    except Exception:
        pass
    code = str(code)

    if code == "42501":
        msg = (
            f"⚠️ Supabase-tabel '{table}': Row Level Security blokkeert het "
            f"schrijven. Draai dit in de Supabase SQL editor:\n\n"
            f"`ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;`\n\n"
            f"Originele fout: {exc}"
        )
    elif code == "42P01":
        msg = (
            f"⚠️ Supabase-tabel '{table}' bestaat nog niet. Maak hem aan "
            f"via de SQL in CLAUDE.md (zie 'Supabase-tabellen die NODIG zijn'). "
            f"Originele fout: {exc}"
        )
    elif code == "42703":
        msg = (
            f"⚠️ Supabase-tabel '{table}' mist één of meerdere kolommen uit "
            f"{list(dropped_cols)}. Die velden worden nu weggegooid bij opslag "
            f"(daardoor blijven ze leeg in de UI). "
            f"Voeg ze toe via `ALTER TABLE {table} ADD COLUMN <naam> <type>` "
            f"in de Supabase SQL editor. Originele fout: {exc}"
        )
    else:
        msg = (
            f"⚠️ Supabase-fout op tabel '{table}': opslag is mislukt. "
            f"Originele fout: {exc}"
        )
    _schema_drift_notes.add(msg)


def get_schema_drift_notes() -> list:
    """Geeft de lijst van schema-drift waarschuwingen uit deze sessie terug."""
    return sorted(_schema_drift_notes)


def _sb_call(fn, *args, **kwargs):
    """
    Voer een Supabase-aanroep uit met retry-logica.
    Probeert max _SB_RETRIES keer bij netwerk-/timeout-fouten.
    Gooit de laatste uitzondering door als alle pogingen mislukken.
    """
    last_exc = None
    for attempt in range(1, _SB_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < _SB_RETRIES:
                time.sleep(_SB_DELAY)
    raise last_exc


def init(url: str = "", key: str = "") -> bool:
    """
    Initialiseer Supabase verbinding.
    Geeft True terug als verbinding geslaagd, anders False (JSON fallback).
    """
    global _supabase, _using_supabase
    if not url or not key:
        _using_supabase = False
        return False
    try:
        from supabase import create_client
        client = create_client(url, key)
        # Verbinding testen met een minimale query
        client.table("favorieten").select("id").limit(1).execute()
        _supabase = client
        _using_supabase = True
        return True
    except Exception:
        _supabase = None
        _using_supabase = False
        return False


def is_cloud() -> bool:
    """True als Supabase verbinding actief is."""
    return _using_supabase


# ─── Hulpfuncties ─────────────────────────────────────────────────────────────

def make_fav_id(player: str, bet_type: str) -> str:
    """Stabiele 10-karakter ID op basis van speler + bet type."""
    return hashlib.md5(
        f"{player.strip().lower()}|{bet_type.strip().lower()}".encode()
    ).hexdigest()[:10]


def _cutoff_date() -> str:
    return (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()


def _get_used_session_ids() -> set:
    """Geeft alle session_ids terug die zijn gekoppeld aan een geplaatste weddenschap."""
    results = load_resultaten()
    return {r.get("source_session_id") for r in results if r.get("source_session_id")}


# ─── Geschiedenis ─────────────────────────────────────────────────────────────

def load_history() -> list:
    """Laad alle analyses: recent (< 7 dagen) OF gekoppeld aan een geplaatste weddenschap."""
    cutoff    = _cutoff_date()
    used_ids  = _get_used_session_ids()

    if _using_supabase:
        try:
            # Haal alles op; filter client-side op datum of gebruik
            resp = _sb_call(lambda: (
                _supabase.table("geschiedenis")
                .select("*")
                .order("datum", desc=True)
                .order("tijd", desc=True)
                .execute()
            ))
            result = []
            for r in (resp.data or []):
                sid = r.get("session_id", "")
                if r.get("datum", "") < cutoff and sid not in used_ids:
                    continue  # oud en ongebruikt → overslaan
                _ap = r.get("alle_props_json", "[]") or "[]"
                _ps = r.get("parlay_suggesties_json", "[]") or "[]"
                result.append({
                    "datum":             r.get("datum", ""),
                    "tijd":              r.get("tijd", ""),
                    "session_id":        sid,
                    "top5":              json.loads(r.get("top5_json", "[]")),
                    "alle_props_json":   json.loads(_ap),
                    "parlay_suggesties": json.loads(_ps),
                })
            return result
        except Exception:
            pass  # fallback naar JSON

    if not HISTORY_FILE.exists():
        return []
    try:
        entries = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return [
            e for e in entries
            if e.get("datum", "") >= cutoff or e.get("session_id", "") in used_ids
        ]
    except Exception:
        return []


def save_to_history(
    enriched: list,
    alle_props: Optional[list] = None,
    parlay_suggesties: Optional[list] = None,
) -> str:
    """Sla analyse op en geef de session_id terug."""
    now        = _now_local()
    datum      = now.strftime("%Y-%m-%d")
    tijd       = now.strftime("%H:%M")
    session_id = uuid.uuid4().hex[:12]
    top5  = enriched[:5]
    top5_data = [
        {
            "rank":     i + 1,
            "speler":   b["player"],
            "bet":      b["bet_type"],
            "odds":     str(b["odds"]),
            "ev_score": f"{b['ev']:+.3f}",
            "rating":   b["rating"],
            # extra velden voor geschiedenis-tab filtering
            "sport":    b.get("sport", ""),
            "composite": b.get("composite", 0),
            "player":   b.get("player", ""),
        }
        for i, b in enumerate(top5)
    ]

    # Alle positieve-EV props bewaren voor de geschiedenis-tab
    _alle = alle_props if alle_props is not None else [
        b for b in enriched if float(b.get("ev") or 0) > 0
    ]
    # Vereenvoudigd formaat zodat het niet te groot wordt
    _alle_compact = [
        {
            "player":    b.get("player", ""),
            "sport":     b.get("sport", ""),
            "bet_type":  b.get("bet_type", ""),
            "odds":      b.get("odds", ""),
            "ev":        round(float(b.get("ev") or 0), 4),
            "composite": round(float(b.get("composite") or 0), 4),
            "rating":    b.get("rating", ""),
        }
        for b in _alle
    ]

    if _using_supabase:
        try:
            entry_id = f"{datum}_{tijd}_{uuid.uuid4().hex[:6]}"
            _supabase.table("geschiedenis").insert({
                "id":                     entry_id,
                "datum":                  datum,
                "tijd":                   tijd,
                "session_id":             session_id,
                "top5_json":              json.dumps(top5_data, ensure_ascii=False),
                "alle_props_json":        json.dumps(_alle_compact, ensure_ascii=False),
                "parlay_suggesties_json": json.dumps(parlay_suggesties or [], ensure_ascii=False),
            }).execute()
            return session_id
        except Exception:
            # Nieuwe kolommen bestaan mogelijk nog niet — probeer met alleen originele kolommen
            try:
                _supabase.table("geschiedenis").insert({
                    "id":        entry_id,
                    "datum":     datum,
                    "tijd":      tijd,
                    "top5_json": json.dumps(top5_data, ensure_ascii=False),
                }).execute()
                return session_id
            except Exception:
                pass  # Supabase volledig onbeschikbaar → lokale fallback

    entry = {
        "datum":             datum,
        "tijd":              tijd,
        "session_id":        session_id,
        "top5":              top5_data,
        "alle_props_json":   _alle_compact,
        "parlay_suggesties": parlay_suggesties or [],
    }

    # Laad alle bestaande entries en prune slim:
    # bewaar alleen entries die recent zijn OF gekoppeld aan een geplaatste bet
    cutoff   = _cutoff_date()
    used_ids = _get_used_session_ids()
    try:
        if HISTORY_FILE.exists():
            all_entries = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        else:
            all_entries = []
    except Exception:
        all_entries = []

    all_entries.insert(0, entry)
    all_entries = [
        e for e in all_entries
        if e.get("datum", "") >= cutoff or e.get("session_id", "") in used_ids
    ]
    try:
        HISTORY_FILE.write_text(
            json.dumps(all_entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return session_id


# ─── Favorieten ───────────────────────────────────────────────────────────────

def load_favorieten() -> list:
    if _using_supabase:
        try:
            resp = _sb_call(lambda: (
                _supabase.table("favorieten")
                .select("*")
                .order("datum", desc=True)
                .execute()
            ))
            return resp.data or []
        except Exception:
            pass

    if not FAV_FILE.exists():
        return []
    try:
        return json.loads(FAV_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_favorieten(favs: list) -> None:
    """Alleen gebruikt als Supabase niet beschikbaar is."""
    try:
        FAV_FILE.write_text(
            json.dumps(favs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def add_favoriet(fav_id: str, bet: dict, source_session_id: str = "", game_date: str = "") -> None:
    datum = datetime.date.today().isoformat()
    row = {
        "id":                fav_id,
        "datum":             datum,
        "game_date":         game_date or datum,  # datum wedstrijd; default = vandaag
        "speler":            bet.get("player", ""),
        "team":              bet.get("team", ""),   # team van speler/bet, voor display in Shortlist
        "bet":               bet.get("bet_type", ""),
        "odds":              round(float(bet.get("odds", 0)), 2),
        "ev_score":          round(float(bet.get("ev", 0)), 4),
        "sport":             bet.get("sport", ""),
        "bet365_status":     bet.get("bet365", {}).get("status", "unknown"),
        "source_session_id": source_session_id,
        # Model-kalibratie velden — voor feedback loop in Bankroll tab
        "rating":            bet.get("rating", ""),
        "composite":         round(float(bet.get("composite") or 0), 4),
        # Screenshot import velden
        "import_method":     bet.get("import_method", ""),
        "bookmaker":         bet.get("bookmaker", ""),
    }
    if _using_supabase:
        try:
            _supabase.table("favorieten").upsert(row).execute()
            return
        except Exception as _exc_full:
            # Nieuwe kolommen bestaan mogelijk nog niet — probeer zonder optionele kolommen
            _optional_cols = ("source_session_id", "rating", "composite",
                              "game_date", "import_method", "bookmaker", "team")
            try:
                row_basic = {k: v for k, v in row.items() if k not in _optional_cols}
                _supabase.table("favorieten").upsert(row_basic).execute()
                _note_schema_drift("favorieten", _optional_cols, _exc_full)
                return
            except Exception:
                pass  # Supabase volledig onbeschikbaar → lokale fallback

    # JSON fallback — voeg alleen toe als nog niet aanwezig
    favs = load_favorieten()
    if any(f.get("id") == fav_id for f in favs):
        return
    favs.insert(0, row)
    save_favorieten(favs)


def remove_favoriet(fav_id: str) -> None:
    if _using_supabase:
        try:
            _supabase.table("favorieten").delete().eq("id", fav_id).execute()
            return
        except Exception:
            pass

    save_favorieten([f for f in load_favorieten() if f.get("id") != fav_id])


# ─── Resultaten ───────────────────────────────────────────────────────────────

def load_resultaten() -> list:
    if _using_supabase:
        try:
            resp = _sb_call(lambda: _supabase.table("resultaten").select("*").execute())
            return resp.data or []
        except Exception:
            pass

    if not RESULTS_FILE.exists():
        return []
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_resultaten(results: list) -> None:
    """Alleen gebruikt als Supabase niet beschikbaar is."""
    try:
        RESULTS_FILE.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def upsert_resultaat(fav_id: str, fav: dict, uitkomst: str, inzet: float) -> None:
    odds = float(fav.get("odds", 1.0))
    if uitkomst == "gewonnen":
        wl = round(inzet * (odds - 1), 2)
    elif uitkomst == "verloren":
        wl = round(-inzet, 2)
    elif uitkomst == "void":
        wl = 0.0  # inzet teruggestort, netto neutraal
    else:  # "open" — nog niet gesettled, geen P&L boeken
        wl = 0.0
    _is_parlay = str(fav_id).startswith("parlay_")

    # ── Datum bepalen ─────────────────────────────────────────────────────────
    # Bij een UPDATE (rij bestaat al, bijv. status gaat van open → gewonnen):
    # behoud de oorspronkelijke placement-datum.
    # Bij een NIEUWE rij: gebruik de meegegeven datum (uit bijv. screenshot
    # upload, waar de gebruiker de wedstrijddatum invult), anders vandaag.
    _existing_datum = ""
    try:
        _existing_datum = next(
            (r.get("datum", "") for r in load_resultaten() if r.get("id") == fav_id),
            "",
        )
    except Exception:
        pass
    if _existing_datum:
        _datum_val = _existing_datum
    else:
        _datum_val = fav.get("datum") or datetime.date.today().isoformat()

    row  = {
        "id":                fav_id,
        "datum":             _datum_val,
        "speler":            fav.get("speler") or fav.get("player", ""),
        "team":              fav.get("team", ""),   # team van speler/bet
        "bet":               fav.get("bet") or fav.get("bet_type", ""),
        "odds":              odds,
        "inzet":             round(inzet, 2),
        "uitkomst":          uitkomst,
        "winst_verlies":     wl,
        "sport":             fav.get("sport", ""),
        "ev_score":          fav.get("ev_score", 0.0),
        "is_parlay":         _is_parlay,
        "source_session_id": fav.get("source_session_id", ""),
        # Model-kalibratie velden — doorgegeven vanuit favoriet voor feedback loop
        "rating":            fav.get("rating", ""),
        "composite":         float(fav.get("composite") or 0),
        # Screenshot import velden
        "import_method":     fav.get("import_method", ""),
        "bookmaker":         fav.get("bookmaker", ""),
    }
    if _using_supabase:
        try:
            _supabase.table("resultaten").upsert(row).execute()
            return
        except Exception as _exc_full:
            # Kolom bestaat mogelijk nog niet — probeer zonder optionele kolommen
            _optional_cols = ("source_session_id", "is_parlay", "rating",
                              "composite", "import_method", "bookmaker", "team")
            try:
                row_basic = {k: v for k, v in row.items() if k not in _optional_cols}
                _supabase.table("resultaten").upsert(row_basic).execute()
                _note_schema_drift("resultaten", _optional_cols, _exc_full)
                return
            except Exception:
                pass  # Supabase volledig onbeschikbaar → lokale fallback

    results = [r for r in load_resultaten() if r.get("id") != fav_id]
    results.insert(0, row)
    save_resultaten(results)


def remove_resultaat(fav_id: str) -> None:
    if _using_supabase:
        try:
            _supabase.table("resultaten").delete().eq("id", fav_id).execute()
            return
        except Exception:
            pass

    save_resultaten([r for r in load_resultaten() if r.get("id") != fav_id])


# ── Parlays ───────────────────────────────────────────────────────────────────

def load_parlays() -> list:
    """Laad alle opgeslagen parlays."""
    if _using_supabase:
        try:
            res = _sb_call(lambda: _supabase.table("parlays").select("*").order("datum", desc=True).execute())
            rows = res.data or []
            for r in rows:
                for field in ("props_json", "legs_json"):
                    if r.get(field) and isinstance(r[field], str):
                        try:
                            r[field] = json.loads(r[field])
                        except Exception:
                            pass
            return rows
        except Exception as e:
            import logging; logging.warning(f"Supabase load_parlays: {e}")
    path = _local_path("parlays.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


def save_parlay(parlay: dict) -> None:
    """Sla een parlay op (upsert)."""
    import uuid as _uuid, datetime as _dt
    if "id" not in parlay:
        parlay["id"] = str(_uuid.uuid4())[:8]
    if "datum" not in parlay:
        parlay["datum"] = _now_local().isoformat()
    if _using_supabase:
        try:
            row = dict(parlay)
            for field in ("props_json", "legs_json"):
                if isinstance(row.get(field), (list, dict)):
                    row[field] = json.dumps(row[field], ensure_ascii=False)
            _supabase.table("parlays").upsert(row).execute()
            return
        except Exception as e:
            import logging; logging.warning(f"Supabase save_parlay (full): {e}")
            # Retry without legs_json — kolom bestaat mogelijk nog niet in Supabase
            try:
                row_no_legs = {k: v for k, v in row.items() if k != "legs_json"}
                _supabase.table("parlays").upsert(row_no_legs).execute()
                return
            except Exception as e2:
                import logging; logging.warning(f"Supabase save_parlay (fallback): {e2}")
    existing = load_parlays()
    existing = [p for p in existing if p.get("id") != parlay["id"]]
    existing.insert(0, parlay)
    try:
        _local_path("parlays.json").write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    except Exception:
        pass


def update_parlay(parlay_id: str, updates: dict) -> None:
    """Update velden van een parlay."""
    if _using_supabase:
        try:
            row = dict(updates)
            for field in ("props_json", "legs_json"):
                if isinstance(row.get(field), (list, dict)):
                    row[field] = json.dumps(row[field], ensure_ascii=False)
            _supabase.table("parlays").update(row).eq("id", parlay_id).execute()
            return
        except Exception as e:
            import logging; logging.warning(f"Supabase update_parlay (full): {e}")
            # Retry without legs_json — kolom bestaat mogelijk nog niet in Supabase
            try:
                row_no_legs = {k: v for k, v in row.items() if k != "legs_json"}
                if row_no_legs:
                    _supabase.table("parlays").update(row_no_legs).eq("id", parlay_id).execute()
                return
            except Exception as e2:
                import logging; logging.warning(f"Supabase update_parlay (fallback): {e2}")
    existing = load_parlays()
    for p in existing:
        if p.get("id") == parlay_id:
            p.update(updates)
    try:
        _local_path("parlays.json").write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    except Exception:
        pass


def delete_parlay(parlay_id: str) -> None:
    """Verwijder een parlay."""
    if _using_supabase:
        try:
            _supabase.table("parlays").delete().eq("id", parlay_id).execute()
            return
        except Exception as e:
            import logging; logging.warning(f"Supabase delete_parlay: {e}")
    existing = [p for p in load_parlays() if p.get("id") != parlay_id]
    _local_path("parlays.json").write_text(json.dumps(existing, indent=2, ensure_ascii=False))


def _local_path(filename: str) -> Path:
    """Hulpfunctie voor lokale bestandspaden."""
    return _BASE_DIR / filename


# ── Settings (eenvoudige key-value opslag) ────────────────────────────────────
#
# OPMERKING — Streamlit Cloud is ephemeral. settings.json wordt bij elke
# redeploy gewist. Daarom is Supabase de SOURCE OF TRUTH voor settings, en
# blijft de lokale JSON enkel een read-only backup-cache.

_SETTINGS_FILE = _BASE_DIR / "settings.json"

# Process-level cache zodat we transient Supabase-fouten kunnen overleven
# zonder direct terug te vallen op een lege/verouderde lokale JSON.
_settings_cache: dict = {}


def load_settings() -> dict:
    """Laad app-instellingen uit lokale JSON (backup/dev-fallback)."""
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(settings: dict) -> None:
    """Schrijf app-instellingen naar lokale JSON (alleen als backup-cache)."""
    try:
        _SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def get_setting(key: str, default=None):
    """
    Laad een instelling, in volgorde van betrouwbaarheid:

        1. Supabase tabel 'settings'  ← source of truth
        2. process-cache (laatst-bekende waarde uit deze sessie)
        3. lokale settings.json (laatste backup)
        4. default

    Belangrijk: zodra Supabase een waarde teruggeeft, wordt die ook in de
    process-cache én lokale JSON gezet, zodat een volgende request bij een
    transient Supabase-fout niet plotseling een lege waarde krijgt.

    Supabase tabel aanmaken (éénmalig):
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """
    if _using_supabase:
        try:
            res = _sb_call(lambda: (
                _supabase.table("settings").select("value").eq("key", key).limit(1).execute()
            ))
            if res.data:
                raw = res.data[0].get("value")
                # Probeer numerieke conversie waar mogelijk
                if raw is None or raw == "":
                    val = default
                else:
                    try:
                        val = float(raw)
                    except (TypeError, ValueError):
                        val = raw
                if val not in (None, default):
                    _settings_cache[key] = val
                    # Lokale backup ook bijwerken zodat we offline iets hebben
                    s = load_settings()
                    s[key] = val
                    save_settings(s)
                return val
            # Key bestaat niet in Supabase — val door naar cache/lokaal
        except Exception:
            # Supabase niet bereikbaar — gebruik cache als die er is
            if key in _settings_cache:
                return _settings_cache[key]
    # Process-cache eerst (de actieve sessie heeft mogelijk al iets gezien)
    if key in _settings_cache:
        return _settings_cache[key]
    # Lokale JSON als laatste redmiddel
    val = load_settings().get(key, None)
    if val is not None:
        _settings_cache[key] = val
        return val
    return default


def set_setting(key: str, value) -> bool:
    """
    Sla een instelling op. Probeert Supabase + lokale JSON + cache.

    Geeft True terug als opslaan minstens ergens lukte. Geeft False terug
    als noch Supabase noch lokale JSON konden schrijven — dat is een
    rampscenario dat de UI aan de gebruiker MOET tonen.

    Bij een Supabase-fout (bijv. tabel ontbreekt of value-kolom is verkeerd
    type) wordt een schema-drift-melding geregistreerd, die bovenaan de app
    als banner verschijnt.
    """
    saved_anywhere = False
    sb_exc = None
    if _using_supabase:
        try:
            _sb_call(lambda: (
                _supabase.table("settings").upsert({"key": key, "value": str(value)}).execute()
            ))
            saved_anywhere = True
        except Exception as exc:
            sb_exc = exc
            _note_schema_drift("settings", ("key", "value"), exc)
    # Altijd lokaal als backup
    s = load_settings()
    s[key] = value
    try:
        save_settings(s)
        saved_anywhere = True
    except Exception:
        pass
    # Cache bijwerken zodat huidige sessie meteen klopt
    _settings_cache[key] = value
    return saved_anywhere


# ── Bankroll mutaties (opnames / stortingen) ──────────────────────────────────
#
# OPMERKING — Op Streamlit Cloud is bankroll_mutations.json ephemeral. Daarom
# is Supabase tabel 'bankroll_mutations' de source of truth. De lokale JSON
# wordt alleen bijgehouden als backup-cache.

_MUTATIONS_FILE = _BASE_DIR / "bankroll_mutations.json"
_mutations_migrated: bool = False  # one-time copy lokale JSON → Supabase


def _read_local_mutations() -> list:
    if _MUTATIONS_FILE.exists():
        try:
            return json.loads(_MUTATIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _write_local_mutations(mutations: list) -> None:
    try:
        _MUTATIONS_FILE.write_text(
            json.dumps(mutations, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _migrate_local_mutations_if_needed() -> None:
    """
    One-time helper: als er lokale mutations zijn die nog NIET in Supabase
    staan, kopieer ze door. Idempotent — kan veilig elke sessie draaien.
    """
    global _mutations_migrated
    if _mutations_migrated or not _using_supabase:
        return
    _mutations_migrated = True
    local = _read_local_mutations()
    if not local:
        return
    try:
        existing = _sb_call(lambda: _supabase.table("bankroll_mutations").select("id").execute())
        existing_ids = {r.get("id") for r in (existing.data or [])}
    except Exception:
        # Tabel bestaat waarschijnlijk niet — laat schema-drift dat melden
        return
    for m in local:
        mid = m.get("id")
        if not mid or mid in existing_ids:
            continue
        row = {
            "id":           mid,
            "datum":        m.get("datum", ""),
            "bedrag":       float(m.get("bedrag", 0) or 0),
            "omschrijving": m.get("omschrijving", "") or "",
        }
        try:
            _sb_call(lambda r=row: _supabase.table("bankroll_mutations").upsert(r).execute())
        except Exception:
            pass  # individuele rij gefaald — andere proberen we toch


def load_bankroll_mutations() -> list:
    """
    Laad bankroll-mutaties. Supabase is leidend; lokale JSON is fallback
    bij netwerkstoring of als de Supabase-tabel nog niet is aangemaakt.

    Synchroniseert opgehaalde Supabase-data naar de lokale JSON, zodat
    we bij een transient outage niet plotseling 0 mutaties tonen.
    """
    if _using_supabase:
        _migrate_local_mutations_if_needed()
        try:
            res = _sb_call(lambda: _supabase.table("bankroll_mutations").select("*").execute())
            data = res.data or []
            # Lokale backup updaten zodat we bij netwerkfout iets hebben
            _write_local_mutations(data)
            return data
        except Exception as exc:
            _note_schema_drift("bankroll_mutations",
                               ("id", "datum", "bedrag", "omschrijving"), exc)
    return _read_local_mutations()


def save_bankroll_mutation(bedrag: float, omschrijving: str, datum: str = "") -> str:
    """
    Voeg een bankroll-mutatie toe. bedrag > 0 = storting, < 0 = opname.

    Schrijft naar Supabase EN naar lokale JSON. Geeft het mutation_id terug.
    Raised RuntimeError als opslaan nergens lukte (zeer zeldzaam — alleen
    als Supabase niet bereikbaar is EN het lokale filesystem read-only is).
    """
    import uuid as _uuid
    mutation_id = str(_uuid.uuid4())[:8]
    row = {
        "id":           mutation_id,
        "datum":        datum or _now_local().strftime("%Y-%m-%d"),
        "bedrag":       float(bedrag),
        "omschrijving": (omschrijving or "").strip() or ("Storting" if bedrag > 0 else "Opname"),
    }
    saved_anywhere = False
    sb_exc = None
    if _using_supabase:
        try:
            _sb_call(lambda: _supabase.table("bankroll_mutations").upsert(row).execute())
            saved_anywhere = True
        except Exception as exc:
            sb_exc = exc
            _note_schema_drift("bankroll_mutations",
                               ("id", "datum", "bedrag", "omschrijving"), exc)
    # Lokale backup bijwerken
    mutations = _read_local_mutations()
    if not any(m.get("id") == mutation_id for m in mutations):
        mutations.append(row)
        try:
            _write_local_mutations(mutations)
            saved_anywhere = True
        except Exception:
            pass
    if not saved_anywhere:
        raise RuntimeError(
            f"Mutatie kon nergens worden opgeslagen. Supabase fout: {sb_exc}"
        )
    return mutation_id


def delete_bankroll_mutation(mutation_id: str) -> bool:
    """Verwijder mutatie uit Supabase EN lokale JSON. Returns True als er
    daadwerkelijk iets verwijderd is."""
    deleted_anywhere = False
    if _using_supabase:
        try:
            _sb_call(lambda: (
                _supabase.table("bankroll_mutations").delete().eq("id", mutation_id).execute()
            ))
            deleted_anywhere = True
        except Exception:
            pass  # val terug op lokaal
    mutations = _read_local_mutations()
    new_list  = [m for m in mutations if m.get("id") != mutation_id]
    if len(new_list) != len(mutations):
        _write_local_mutations(new_list)
        deleted_anywhere = True
    return deleted_anywhere


def get_bankroll_mutations_total() -> float:
    """Netto som van alle mutaties (stortingen + opnames). Defensief tegen
    None, NaN, of niet-numerieke waarden die uit een corrupte rij kunnen komen."""
    import math
    total = 0.0
    for m in load_bankroll_mutations():
        raw = m.get("bedrag", 0)
        try:
            v = float(raw if raw not in (None, "") else 0)
            if math.isnan(v) or math.isinf(v):
                continue
            total += v
        except (TypeError, ValueError):
            continue
    return total


# ── Direct bet (gesloten weddenschap direct naar resultaten) ──────────────────

def add_direct_bet(speler: str, sport: str, bet_type: str, odds: float,
                   inzet: float, uitkomst: str, datum: str = "",
                   ev_score: float = 0.0) -> str:
    """Voeg een al gezette weddenschap direct toe aan resultaten, zonder favorieten."""
    import uuid as _uuid
    fav_id = f"direct_{_uuid.uuid4().hex[:10]}"
    fav = {
        "speler":    speler,
        "bet":       bet_type,
        "sport":     sport,
        "odds":      odds,
        "ev_score":  ev_score,
        "datum":     datum or datetime.date.today().isoformat(),
    }
    upsert_resultaat(fav_id, fav, uitkomst, inzet)
    return fav_id
