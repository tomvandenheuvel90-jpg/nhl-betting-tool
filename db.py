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
"""

import json
import hashlib
import datetime
import uuid
import time
from pathlib import Path
from typing import Optional, List, Dict

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
    now        = datetime.datetime.now()
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


def add_favoriet(fav_id: str, bet: dict, source_session_id: str = "") -> None:
    datum = datetime.date.today().isoformat()
    row = {
        "id":                fav_id,
        "datum":             datum,
        "speler":            bet.get("player", ""),
        "bet":               bet.get("bet_type", ""),
        "odds":              round(float(bet.get("odds", 0)), 2),
        "ev_score":          round(float(bet.get("ev", 0)), 4),
        "sport":             bet.get("sport", ""),
        "bet365_status":     bet.get("bet365", {}).get("status", "unknown"),
        "source_session_id": source_session_id,
    }
    if _using_supabase:
        try:
            _supabase.table("favorieten").upsert(row).execute()
            return
        except Exception:
            # Kolom bestaat mogelijk nog niet — probeer zonder source_session_id
            try:
                row_basic = {k: v for k, v in row.items() if k != "source_session_id"}
                _supabase.table("favorieten").upsert(row_basic).execute()
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
    wl   = round(inzet * (odds - 1), 2) if uitkomst == "gewonnen" else round(-inzet, 2)
    _is_parlay = str(fav_id).startswith("parlay_")
    row  = {
        "id":                fav_id,
        "datum":             fav.get("datum", datetime.date.today().isoformat()),
        "speler":            fav.get("speler", ""),
        "bet":               fav.get("bet", ""),
        "odds":              odds,
        "inzet":             round(inzet, 2),
        "uitkomst":          uitkomst,
        "winst_verlies":     wl,
        "sport":             fav.get("sport", ""),
        "ev_score":          fav.get("ev_score", 0.0),
        "is_parlay":         _is_parlay,
        "source_session_id": fav.get("source_session_id", ""),
    }
    if _using_supabase:
        try:
            _supabase.table("resultaten").upsert(row).execute()
            return
        except Exception:
            # Kolom bestaat mogelijk nog niet — probeer zonder optionele kolommen
            try:
                row_basic = {k: v for k, v in row.items()
                             if k not in ("source_session_id", "is_parlay")}
                _supabase.table("resultaten").upsert(row_basic).execute()
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
        parlay["datum"] = _dt.datetime.now().isoformat()
    if _using_supabase:
        try:
            row = dict(parlay)
            for field in ("props_json", "legs_json"):
                if isinstance(row.get(field), (list, dict)):
                    row[field] = json.dumps(row[field], ensure_ascii=False)
            _supabase.table("parlays").upsert(row).execute()
            return
        except Exception as e:
            import logging; logging.warning(f"Supabase save_parlay: {e}")
    existing = load_parlays()
    existing = [p for p in existing if p.get("id") != parlay["id"]]
    existing.insert(0, parlay)
    _local_path("parlays.json").write_text(json.dumps(existing, indent=2, ensure_ascii=False))


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
            import logging; logging.warning(f"Supabase update_parlay: {e}")
    existing = load_parlays()
    for p in existing:
        if p.get("id") == parlay_id:
            p.update(updates)
    _local_path("parlays.json").write_text(json.dumps(existing, indent=2, ensure_ascii=False))


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

_SETTINGS_FILE = _BASE_DIR / "settings.json"


def load_settings() -> dict:
    """Laad app-instellingen (startbankroll etc.)."""
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(settings: dict) -> None:
    """Sla app-instellingen op."""
    try:
        _SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def get_setting(key: str, default=None):
    """
    Laad een instelling. Probeert Supabase eerst (tabel 'settings'),
    daalt terug op lokale settings.json als Supabase niet beschikbaar is
    of de tabel niet bestaat.

    Supabase tabel aanmaken (éénmalig):
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """
    if _using_supabase:
        try:
            res = _supabase.table("settings").select("value").eq("key", key).limit(1).execute()
            if res.data:
                raw = res.data[0]["value"]
                # Probeer numerieke waarden te converteren
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return raw
        except Exception:
            pass  # Tabel bestaat niet of netwerk-fout → val terug op JSON
    return load_settings().get(key, default)


def set_setting(key: str, value) -> None:
    """
    Sla een instelling op in lokale JSON én in Supabase (als verbonden).
    Overschrijft ALLEEN als de gebruiker expliciet opslaat.
    """
    # Altijd lokaal opslaan als backup
    s = load_settings()
    s[key] = value
    save_settings(s)
    # Ook naar Supabase als die beschikbaar is
    if _using_supabase:
        try:
            _supabase.table("settings").upsert({"key": key, "value": str(value)}).execute()
        except Exception:
            pass  # Tabel bestaat niet → geen probleem, lokale JSON is backup


# ── Bankroll mutaties (opnames / stortingen) ──────────────────────────────────

_MUTATIONS_FILE = _BASE_DIR / "bankroll_mutations.json"


def load_bankroll_mutations() -> list:
    """Laad lijst van bankroll-mutaties (opnames en stortingen)."""
    if _MUTATIONS_FILE.exists():
        try:
            return json.loads(_MUTATIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_bankroll_mutation(bedrag: float, omschrijving: str, datum: str = "") -> str:
    """
    Voeg een bankroll-mutatie toe.
    bedrag > 0 = storting, bedrag < 0 = opname.
    Geeft het gegenereerde id terug.
    """
    import uuid as _uuid
    import datetime as _dt
    mutations = load_bankroll_mutations()
    mutation_id = str(_uuid.uuid4())[:8]
    mutations.append({
        "id":           mutation_id,
        "datum":        datum or _dt.datetime.now().strftime("%Y-%m-%d"),
        "bedrag":       float(bedrag),
        "omschrijving": omschrijving.strip() or ("Storting" if bedrag > 0 else "Opname"),
    })
    _MUTATIONS_FILE.write_text(
        json.dumps(mutations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return mutation_id


def delete_bankroll_mutation(mutation_id: str) -> bool:
    """Verwijder een bankroll-mutatie op id."""
    mutations = load_bankroll_mutations()
    new_list  = [m for m in mutations if m.get("id") != mutation_id]
    if len(new_list) == len(mutations):
        return False
    _MUTATIONS_FILE.write_text(
        json.dumps(new_list, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


def get_bankroll_mutations_total() -> float:
    """Geeft de netto som van alle mutaties (stortingen min opnames)."""
    return sum(float(m.get("bedrag", 0)) for m in load_bankroll_mutations())


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
