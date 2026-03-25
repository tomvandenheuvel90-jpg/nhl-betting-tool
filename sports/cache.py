"""JSON cache met TTL voor API responses en CSV downloads."""

import json
import datetime
from pathlib import Path

CACHE_FILE = Path(__file__).parent.parent / ".bet_cache.json"


def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entries": {}}


def _save(data: dict):
    CACHE_FILE.write_text(
        json.dumps(data, default=str, separators=(",", ":")),
        encoding="utf-8",
    )


def get(key: str):
    """Geeft gecachte waarde of None als niet aanwezig / verlopen."""
    data = _load()
    entry = data["entries"].get(key)
    if not entry:
        return None
    try:
        if datetime.datetime.fromisoformat(entry["expires_at"]) < datetime.datetime.now():
            return None
    except Exception:
        return None
    return entry["data"]


def set(key: str, value, ttl_hours: int = 24):
    """Sla waarde op met TTL in uren."""
    data = _load()
    data["entries"][key] = {
        "data": value,
        "expires_at": (
            datetime.datetime.now() + datetime.timedelta(hours=ttl_hours)
        ).isoformat(),
    }
    _save(data)


def clear_expired():
    """Verwijder verlopen entries om bestandsgrootte te beperken."""
    data = _load()
    now = datetime.datetime.now()
    data["entries"] = {
        k: v
        for k, v in data["entries"].items()
        if datetime.datetime.fromisoformat(v["expires_at"]) >= now
    }
    _save(data)
