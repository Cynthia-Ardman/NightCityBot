"""Google Drive backup utility for NightCityBot.

Authenticates with Google Drive using a service account and provides
upload, download, list, and cleanup operations for backup files.
"""

import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

GDRIVE_FOLDER_ID = os.getenv("GDRIVE_BACKUP_FOLDER_ID", "")
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))


def _get_credentials():
    from google.oauth2 import service_account

    creds_json = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        raise RuntimeError(
            "GDRIVE_SERVICE_ACCOUNT_JSON environment variable is not set. "
            "See BACKUP_SETUP.md for instructions."
        )
    info = json.loads(creds_json)
    return service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)


def _build_service():
    from googleapiclient.discovery import build

    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(
    file_path: str,
    *,
    folder_id: str = "",
    filename_override: str = "",
    mime_type: str = "application/gzip",
) -> dict:
    from googleapiclient.http import MediaFileUpload

    service = _build_service()
    folder = folder_id or GDRIVE_FOLDER_ID
    if not folder:
        raise RuntimeError(
            "GDRIVE_BACKUP_FOLDER_ID environment variable is not set. "
            "See BACKUP_SETUP.md for instructions."
        )

    name = filename_override or os.path.basename(file_path)
    file_metadata = {"name": name, "parents": [folder]}
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    result = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id,name,size,webViewLink")
        .execute()
    )
    logger.info("Uploaded %s to Google Drive (id=%s)", name, result.get("id"))
    return result


def upload_bytes(
    data: bytes,
    filename: str,
    *,
    folder_id: str = "",
    mime_type: str = "application/gzip",
) -> dict:
    from googleapiclient.http import MediaIoBaseUpload

    service = _build_service()
    folder = folder_id or GDRIVE_FOLDER_ID
    if not folder:
        raise RuntimeError(
            "GDRIVE_BACKUP_FOLDER_ID environment variable is not set. "
            "See BACKUP_SETUP.md for instructions."
        )

    file_metadata = {"name": filename, "parents": [folder]}
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)

    result = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id,name,size,webViewLink")
        .execute()
    )
    logger.info("Uploaded %s to Google Drive (id=%s)", filename, result.get("id"))
    return result


def list_backups(folder_id: str = "", limit: int = 50) -> list[dict]:
    service = _build_service()
    folder = folder_id or GDRIVE_FOLDER_ID
    if not folder:
        logger.warning("GDRIVE_BACKUP_FOLDER_ID is not set — cannot list backups")
        return []

    query = f"'{folder}' in parents and trashed = false"
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id,name,size,createdTime,webViewLink)",
            orderBy="createdTime desc",
            pageSize=limit,
        )
        .execute()
    )
    return results.get("files", [])


def download_backup(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    service = _build_service()
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return buffer.read()


def delete_file(file_id: str) -> None:
    service = _build_service()
    service.files().delete(fileId=file_id).execute()
    logger.info("Deleted Google Drive file id=%s", file_id)


def rotate_old_backups(
    retention_days: int = 0, folder_id: str = ""
) -> list[str]:
    retention = retention_days or BACKUP_RETENTION_DAYS
    cutoff = datetime.now(timezone.utc).timestamp() - (retention * 86400)
    backups = list_backups(folder_id=folder_id, limit=200)
    deleted: list[str] = []

    for backup in backups:
        created_str = backup.get("createdTime", "")
        if not created_str:
            continue
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created_dt.timestamp() < cutoff:
            try:
                delete_file(backup["id"])
                deleted.append(backup["name"])
            except Exception:
                logger.warning(
                    "Failed to delete old backup %s (id=%s)",
                    backup["name"],
                    backup["id"],
                    exc_info=True,
                )

    if deleted:
        logger.info("Rotated %d old backup(s): %s", len(deleted), deleted)
    return deleted


def get_last_backup(folder_id: str = "") -> Optional[dict]:
    backups = list_backups(folder_id=folder_id, limit=1)
    return backups[0] if backups else None
