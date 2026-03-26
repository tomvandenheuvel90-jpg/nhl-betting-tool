#!/usr/bin/env python3
"""
Upload MoneyPuck filtered CSV bestanden naar Google Drive.

Gebruik:
  python3 upload_to_gdrive.py --credentials gdrive_credentials.json

Wat het doet:
  - Upload alle skaters_filtered.csv bestanden uit moneypuck_data/filtered/
  - Mappenstructuur in Drive: BetAnalyzer/moneypuck_data/filtered/{type}/{jaar}/
  - Slaat file IDs op in gdrive_file_ids.json (commit dit naar de repo)

Vereisten:
  pip install google-api-python-client google-auth
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2 import service_account
except ImportError:
    print("Installeer eerst: pip install google-api-python-client google-auth")
    sys.exit(1)

SCOPES        = ["https://www.googleapis.com/auth/drive"]
DATA_DIR      = Path(__file__).parent / "moneypuck_data" / "filtered"
OUTPUT_JSON   = Path(__file__).parent / "gdrive_file_ids.json"
DRIVE_ROOT    = "BetAnalyzer/moneypuck_data/filtered"


# ─── Drive helpers ────────────────────────────────────────────────────────────

def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Zoek map op naam onder parent_id, of maak aan als niet gevonden."""
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def _build_folder_tree(service, root_parent_id: str) -> dict:
    """Bouw mappenstructuur BetAnalyzer/moneypuck_data/filtered/{type}/{jaar}/."""
    parts = DRIVE_ROOT.split("/")
    current_id = root_parent_id
    for part in parts:
        current_id = _get_or_create_folder(service, part, current_id)
    base_id = current_id

    folder_ids = {}
    for season_type in ("regular", "playoffs"):
        type_id = _get_or_create_folder(service, season_type, base_id)
        for year_dir in sorted(DATA_DIR.glob(f"{season_type}/*")):
            if not year_dir.is_dir():
                continue
            year = year_dir.name
            year_id = _get_or_create_folder(service, year, type_id)
            folder_ids[f"{season_type}/{year}"] = year_id

    return folder_ids


def _upload_file(service, local_path: Path, folder_id: str) -> str:
    """Upload bestand naar folder_id. Overschrijft als al bestaat."""
    # Zoek of al bestaat
    query = f"name='{local_path.name}' and '{folder_id}' in parents and trashed=false"
    existing = service.files().list(q=query, fields="files(id)").execute().get("files", [])

    media = MediaFileUpload(str(local_path), mimetype="text/csv", resumable=False)

    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"  ↻ Bijgewerkt: {local_path.name}")
    else:
        meta = {"name": local_path.name, "parents": [folder_id]}
        result = service.files().create(body=meta, media_body=media, fields="id").execute()
        file_id = result["id"]
        print(f"  ↑ Geüpload:  {local_path.name}")

    return file_id


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Upload MoneyPuck data naar Google Drive")
    parser.add_argument(
        "--credentials",
        default="gdrive_credentials.json",
        help="Pad naar service account JSON key (default: gdrive_credentials.json)",
    )
    args = parser.parse_args()

    creds_path = Path(args.credentials)
    if not creds_path.exists():
        print(f"❌ Credentials bestand niet gevonden: {creds_path}")
        print("   Download het JSON key bestand van console.cloud.google.com")
        sys.exit(1)

    if not DATA_DIR.exists():
        print(f"❌ Data map niet gevonden: {DATA_DIR}")
        sys.exit(1)

    # Authenticeer
    print("🔑 Authenticeren met service account...")
    creds = service_account.Credentials.from_service_account_file(
        str(creds_path), scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds)

    # Eigen 'My Drive' root ophalen
    about = service.about().get(fields="user,storageQuota").execute()
    print(f"✅ Ingelogd als: {about.get('user', {}).get('emailAddress', 'onbekend')}")

    # Mappenstructuur aanmaken
    print("\n📁 Mappenstructuur aanmaken in Google Drive...")
    root_query = service.files().list(
        q="name='root' and 'root' in parents",
        fields="files(id)"
    ).execute()
    root_id = "root"
    folder_ids = _build_folder_tree(service, root_id)
    print(f"✅ {len(folder_ids)} mappen klaar")

    # Bestanden uploaden (alleen skaters_filtered.csv — gebruikt door scorer)
    print("\n⬆ Bestanden uploaden...")
    file_ids = {}
    csv_files = sorted(DATA_DIR.rglob("skaters_filtered.csv"))

    if not csv_files:
        print("❌ Geen skaters_filtered.csv bestanden gevonden in moneypuck_data/filtered/")
        sys.exit(1)

    for csv_path in csv_files:
        # Relatief pad t.o.v. DATA_DIR, bijv. "regular/2025/skaters_filtered.csv"
        rel = csv_path.relative_to(DATA_DIR)
        folder_key = str(rel.parent)  # bijv. "regular/2025"

        if folder_key not in folder_ids:
            print(f"  ⚠️ Map niet gevonden voor {rel}, sla over")
            continue

        file_id = _upload_file(service, csv_path, folder_ids[folder_key])
        file_ids[str(rel)] = file_id

    # Sla file IDs op
    OUTPUT_JSON.write_text(json.dumps(file_ids, indent=2))
    print(f"\n✅ {len(file_ids)} bestanden geüpload")
    print(f"📄 File IDs opgeslagen in: {OUTPUT_JSON}")
    print("\n⚠️  Vergeet niet gdrive_file_ids.json te committen naar de repo!")
    print("   git add gdrive_file_ids.json && git commit -m 'Voeg GDrive file IDs toe'")


if __name__ == "__main__":
    main()
