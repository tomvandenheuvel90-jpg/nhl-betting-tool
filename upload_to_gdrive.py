#!/usr/bin/env python3
"""
Upload MoneyPuck filtered CSV bestanden naar Google Drive.

VEREISTE SETUP (eenmalig):
  1. Maak een map aan in jouw eigen Google Drive, bijv. "BetAnalyzer"
  2. Rechtermuisklik op de map → Delen → voeg service account email toe als Editor
     (email staat in gdrive_credentials.json onder "client_email")
  3. Kopieer de map-ID uit de URL:
     https://drive.google.com/drive/folders/FOLDER_ID_HIER  ← dit stuk
  4. Gebruik dat als --folder-id argument:

Gebruik:
  python3 upload_to_gdrive.py --credentials gdrive_credentials.json --folder-id FOLDER_ID

Wat het doet:
  - Upload alle skaters_filtered.csv bestanden uit moneypuck_data/filtered/
  - Mappenstructuur in jouw Drive: {folder-id}/filtered/{type}/{jaar}/
  - Slaat file IDs op in gdrive_file_ids.json (commit dit naar de repo)

Vereisten:
  pip install google-api-python-client google-auth
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2 import service_account
except ImportError:
    print("Installeer eerst: pip install google-api-python-client google-auth")
    sys.exit(1)

SCOPES      = ["https://www.googleapis.com/auth/drive"]
DATA_DIR    = Path(__file__).parent / "moneypuck_data" / "filtered"
OUTPUT_JSON = Path(__file__).parent / "gdrive_file_ids.json"


# ─── Drive helpers ────────────────────────────────────────────────────────────

def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)",
                                   supportsAllDrives=True).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id",
                                    supportsAllDrives=True).execute()
    return folder["id"]


def _build_folder_tree(service, root_id: str) -> dict:
    """Bouw mappen filtered/{type}/{jaar}/ onder root_id."""
    base_id = _get_or_create_folder(service, "filtered", root_id)

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
    query = (f"name='{local_path.name}' and '{folder_id}' in parents "
             f"and trashed=false")
    existing = (service.files()
                .list(q=query, fields="files(id)", supportsAllDrives=True)
                .execute().get("files", []))

    media = MediaFileUpload(str(local_path), mimetype="text/csv", resumable=False)

    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media,
                               supportsAllDrives=True).execute()
        print(f"  ↻ Bijgewerkt: {local_path.name}")
    else:
        meta = {"name": local_path.name, "parents": [folder_id]}
        result = (service.files()
                  .create(body=meta, media_body=media, fields="id",
                          supportsAllDrives=True)
                  .execute())
        file_id = result["id"]
        print(f"  ↑ Geüpload:  {local_path.name}")

    return file_id


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Upload MoneyPuck data naar Google Drive")
    parser.add_argument("--credentials", default="gdrive_credentials.json",
                        help="Pad naar service account JSON key")
    parser.add_argument("--folder-id", required=True,
                        help="Google Drive map-ID (uit de URL van je gedeelde map)")
    args = parser.parse_args()

    creds_path = Path(args.credentials)
    if not creds_path.exists():
        print(f"❌ Credentials niet gevonden: {creds_path}")
        sys.exit(1)

    if not DATA_DIR.exists():
        print(f"❌ Data map niet gevonden: {DATA_DIR}")
        sys.exit(1)

    print("🔑 Authenticeren met service account...")
    creds = service_account.Credentials.from_service_account_file(
        str(creds_path), scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds)

    about = service.about().get(fields="user").execute()
    print(f"✅ Ingelogd als: {about.get('user', {}).get('emailAddress', 'onbekend')}")

    print("\n📁 Mappenstructuur aanmaken...")
    folder_ids = _build_folder_tree(service, args.folder_id)
    print(f"✅ {len(folder_ids)} mappen klaar")

    print("\n⬆ Bestanden uploaden...")
    file_ids = {}
    csv_files = sorted(DATA_DIR.rglob("skaters_filtered.csv"))

    if not csv_files:
        print("❌ Geen skaters_filtered.csv bestanden gevonden")
        sys.exit(1)

    for csv_path in csv_files:
        rel = csv_path.relative_to(DATA_DIR)
        folder_key = str(rel.parent)

        if folder_key not in folder_ids:
            print(f"  ⚠️ Map niet gevonden voor {rel}, sla over")
            continue

        file_id = _upload_file(service, csv_path, folder_ids[folder_key])
        file_ids[str(rel)] = file_id

    OUTPUT_JSON.write_text(json.dumps(file_ids, indent=2))
    print(f"\n✅ {len(file_ids)} bestanden geüpload")
    print(f"📄 File IDs opgeslagen in: {OUTPUT_JSON}")
    print("\n⚠️  Commit gdrive_file_ids.json naar de repo:")
    print("   git add gdrive_file_ids.json && git commit -m 'Voeg GDrive file IDs toe' && git push")


if __name__ == "__main__":
    main()
