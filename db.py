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
from pathlib import Path

# ─── Paden voor lokale JSON fallback ──────────────────────────────────────────

_BASE_DIR    = Path(__file__).parent
HISTORY_FILE = _BASE_DIR / "analyse_geschiedenis.json"
FAV_FILE     = _BASE_DIR / "favorieten.json"
RESULTS_FILE = _BASE_DIR / "resultaten.json"
HISTORY_DAYS = 7

# ─── Supabase state ───────────────────────────────────────────────────────────

_supabase     = None
_using_supabase = False


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


# ─── Geschiedenis ─────────────────────────────────────────────────────────────

def load_history() -> list:
    cutoff = _cutoff_date()
    if _using_supabase:
        try:
            resp = (
                _supabase.table("geschiedenis")
                .select("*")
                .gte("datum", cutoff)
                .order("datum", desc=True)
                .order("tijd", desc=True)
                .execute()
            )
            result = []
            for r in (resp.data or []):
                result.append({
                    "datum": r.get("datum", ""),
                    "tijd":  r.get("tijd", ""),
                    "top5": json.loads(r.get("top5_json", "[]")),
                })
            return result
        except Exception:
            pass  # fallback naar JSON

    if not HISTORY_FILE.exists():
        return []
    try:
        entries = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return [e for e in entries if e.get("datum", "") >= cutoff]
    except Exception:
        return []


def save_to_history(enriched: list) -> None:
    now   = datetime.datetime.now()
    datum = now.strftime("%Y-%m-%d")
    tijd  = now.strftime("%H:%M")
    top5  = enriched[:5]
    top5_data = [
        {
            "rank":     i + 1,
            "speler":   b["player"],
            "bet":      b["bet_type"],
            "odds":     str(b["odds"]),
            "ev_score": f"{b['ev']:+.3f}",
            "rating":   b["rating"],
        }
        for i, b in enumerate(top5)
    ]

    if _using_supabase:
        try:
            entry_id = f"{datum}_{tijd}_{uuid.uuid4().hex[:6]}"
            _supabase.table("geschiedenis").insert({
                "id":        entry_id,
                "datum":     datum,
                "tijd":      tijd,
                "top5_json": json.dumps(top5_data, ensure_ascii=False),
            }).execute()
            return
        except Exception:
            pass  # fallback naar JSON

    entry   = {"datum": datum, "tijd": tijd, "top5": top5_data}
    entries = load_history()
    entries.insert(0, entry)
    cutoff  = _cutoff_date()
    entries = [e for e in entries if e.get("datum", "") >= cutoff]
    try:
        HISTORY_FILE.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ─── Favorieten ───────────────────────────────────────────────────────────────

def load_favorieten() -> list:
    if _using_supabase:
        try:
            resp = (
                _supabase.table("favorieten")
                .select("*")
                .order("datum", desc=True)
                .execute()
            )
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


def add_favoriet(fav_id: str, bet: dict) -> None:
    datum = datetime.date.today().isoformat()
    row = {
        "id":            fav_id,
        "datum":         datum,
        "speler":        bet.get("player", ""),
        "bet":           bet.get("bet_type", ""),
        "odds":          round(float(bet.get("odds", 0)), 2),
        "ev_score":      round(float(bet.get("ev", 0)), 4),
        "sport":         bet.get("sport", ""),
        "bet365_status": bet.get("bet365", {}).get("status", "unknown"),
    }
    if _using_supabase:
        try:
            _supabase.table("favorieten").upsert(row).execute()
            return
        except Exception:
            pass

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
            resp = _supabase.table("resultaten").select("*").execute()
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
    row  = {
        "id":            fav_id,
        "datum":         fav.get("datum", datetime.date.today().isoformat()),
        "speler":        fav.get("speler", ""),
        "bet":           fav.get("bet", ""),
        "odds":          odds,
        "inzet":         round(inzet, 2),
        "uitkomst":      uitkomst,
        "winst_verlies": wl,
        "sport":         fav.get("sport", ""),
        "ev_score":      fav.get("ev_score", 0.0),
    }
    if _using_supabase:
        try:
            _supabase.table("resultaten").upsert(row).execute()
            return
        except Exception:
            pass

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
