import mimetypes
from pathlib import Path
import json
import requests as _http
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from config import settings
from loguru import logger


_cached_creds: Credentials | None = None


def _get_creds() -> Credentials:
    """Return valid Google credentials, refreshing only when expired."""
    global _cached_creds

    if _cached_creds and _cached_creds.valid:
        return _cached_creds

    if _cached_creds is None:
        token_json = settings.google_token_json
        token_info = token_json if isinstance(token_json, dict) else json.loads(token_json)
        _cached_creds = Credentials.from_authorized_user_info(token_info, [settings.google_cloud_scopes])

    if not _cached_creds.valid:
        if _cached_creds.expired and _cached_creds.refresh_token:
            logger.info("Refreshing expired Google credentials...")
            _cached_creds.refresh(Request())
            logger.info("Credentials refreshed successfully")
        else:
            logger.info("Starting OAuth flow for new credentials...")
            client_config = {
                "installed": {
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uris": settings.google_redirect_uris,
                    "auth_uri": settings.google_auth_uri,
                    "token_uri": settings.google_token_uri,
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, [settings.google_cloud_scopes])
            _cached_creds = flow.run_local_server(port=0)
            logger.info("OAuth flow completed successfully")

    return _cached_creds


def get_service():
    """Get authenticated Google Drive service."""
    try:
        service = build("drive", "v3", credentials=_get_creds())
        logger.info("Google Drive service initialized successfully")
        return service
    except Exception as e:
        logger.error(f"Failed to initialize Google Drive service: {e}")
        raise


def get_thumbnail_bytes(thumbnail_url: str) -> bytes | None:
    """Fetch thumbnail bytes server-side using Drive auth (thumbnailLinks require a Bearer token)."""
    try:
        token = _get_creds().token
        resp = _http.get(thumbnail_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        return resp.content if resp.ok else None
    except Exception as e:
        logger.warning(f"Failed to fetch thumbnail: {e}")
        return None

def ensure_folder(service, folder_name: str) -> str:
    """Find or create a folder by name in My Drive (root)."""
    try:
        safe = folder_name.replace("'", "\\'")
        q = f"mimeType='application/vnd.google-apps.folder' and name='{safe}' and trashed=false"
        
        logger.info(f"Searching for folder: {folder_name}")
        resp = (
            service.files()
            .list(q=q, spaces="drive", fields="files(id,name)", pageSize=10)
            .execute()
        )
        
        files = resp.get("files", [])
        if files:
            folder_id = files[0]["id"]
            logger.info(f"Found existing folder: {folder_name} (ID: {folder_id})")
            return folder_id
        
        logger.info(f"Creating new folder: {folder_name}")
        meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
        folder = service.files().create(body=meta, fields="id").execute()
        folder_id = folder["id"]
        logger.info(f"Created folder: {folder_name} (ID: {folder_id})")
        return folder_id
        
    except HttpError as e:
        logger.error(f"Google API error while ensuring folder: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while ensuring folder: {e}")
        raise

def set_public(service, file_id: str) -> None:
    """Make the file viewable by anyone with the link (needed for Markdown embedding)."""
    try:
        service.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}
        ).execute()
        logger.debug(f"Set public permissions for file: {file_id}")
    except HttpError as e:
        logger.warning(f"Failed to set public permissions for {file_id}: {e}")
        # Don't raise - file might still be accessible

def to_direct_view_link(webViewLink: str) -> str:
    """Convert webViewLink to a direct embeddable URL for Markdown."""
    try:
        fid = webViewLink.split("/d/")[1].split("/")[0]
        direct_link = f"https://drive.google.com/uc?export=view&id={fid}"
        logger.debug(f"Converted link: {webViewLink} -> {direct_link}")
        return direct_link
    except Exception as e:
        logger.warning(f"Failed to convert link {webViewLink}: {e}")
        return webViewLink

def upload_images_to_folder(image_paths: list[Path], folder_name: str) -> dict[str, str]:
    """
    Upload images to a Drive folder using OAuth credentials.json/token.json.
    Returns {filename: direct_view_url}.
    """
    if not image_paths:
        logger.info("No images to upload")
        return {}
    
    logger.info(f"Starting upload of {len(image_paths)} images to folder: {folder_name}")
    
    try:
        # Get service and folder
        service = get_service()
        folder_id = ensure_folder(service, folder_name)
        
        out: dict[str, str] = {}
        successful_uploads = 0
        
        for i, p in enumerate(image_paths, 1):
            try:
                logger.info(f"Uploading image {i}/{len(image_paths)}: {p.name}")
                
                # Check if file exists
                if not p.exists():
                    logger.error(f"File does not exist: {p}")
                    continue
                
                # Get MIME type
                mime, _ = mimetypes.guess_type(str(p))
                mime = mime or "application/octet-stream"
                logger.debug(f"Using MIME type: {mime}")
                
                # Prepare upload
                meta = {"name": p.name, "parents": [folder_id]}
                media = MediaFileUpload(str(p), mimetype=mime, resumable=True)
                
                # Upload file
                f = (
                    service.files()
                    .create(body=meta, media_body=media, fields="id,name,webViewLink")
                    .execute()
                )
                
                file_id = f["id"]
                web_view_link = f.get("webViewLink", "")
                
                logger.info(f"Upload successful: {p.name} -> {file_id}")
                
                # Set public permissions
                set_public(service, file_id)
                
                # Convert to direct link
                direct_link = to_direct_view_link(web_view_link)
                out[p.name] = direct_link
                successful_uploads += 1
                
                logger.success(f"✅ {p.name} -> {direct_link}")
                
            except HttpError as e:
                logger.error(f"Google API error uploading {p.name}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error uploading {p.name}: {e}")
        
        logger.info(f"Upload complete: {successful_uploads}/{len(image_paths)} files uploaded successfully")
        
        if not out:
            raise Exception(f"Failed to upload any images to Drive folder '{folder_name}'")
        
        return out
        
    except Exception as e:
        logger.error(f"Failed to upload images to Drive: {e}")
        raise

def list_files_in_folder(folder_name: str) -> list[dict]:
    """List all files in a Drive folder. Returns id, name, thumbnailLink, webViewLink, mimeType, modifiedTime."""
    service = get_service()
    folder_id = ensure_folder(service, folder_name)

    results = (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,thumbnailLink,webViewLink,mimeType,modifiedTime)",
            orderBy="name",
            pageSize=100,
        )
        .execute()
    )
    return results.get("files", [])


def replace_file(file_id: str, new_file_path: Path) -> str:
    """Replace file content in-place keeping the same file ID (and therefore the same URL)."""
    service = get_service()
    mime, _ = mimetypes.guess_type(str(new_file_path))
    mime = mime or "application/octet-stream"

    media = MediaFileUpload(str(new_file_path), mimetype=mime, resumable=True)
    updated = (
        service.files()
        .update(fileId=file_id, media_body=media, fields="id,webViewLink")
        .execute()
    )
    web_view_link = updated.get("webViewLink", "")
    return to_direct_view_link(web_view_link) if web_view_link else ""


def delete_file(file_id: str) -> None:
    """Permanently delete a file from Drive."""
    service = get_service()
    service.files().delete(fileId=file_id).execute()


def _ensure_subfolder(service, parent_id: str, name: str) -> str:
    """Find or create a subfolder inside a given parent folder by ID."""
    safe = name.replace("'", "\\'")
    q = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{safe}' "
        f"and '{parent_id}' in parents "
        f"and trashed=false"
    )
    resp = service.files().list(q=q, spaces="drive", fields="files(id)", pageSize=5).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    return service.files().create(body=meta, fields="id").execute()["id"]


def upload_guide_images(images: list[Path], parent_folder_name: str, guide_name: str) -> dict[str, str]:
    """Upload images into parent_folder/guide_name/ subfolder. Returns {filename: direct_url}."""
    if not images:
        return {}
    service = get_service()
    parent_id = ensure_folder(service, parent_folder_name)
    guide_folder_id = _ensure_subfolder(service, parent_id, guide_name)

    out: dict[str, str] = {}
    for p in images:
        if not p.exists():
            logger.warning(f"Image not found, skipping: {p}")
            continue
        mime, _ = mimetypes.guess_type(str(p))
        mime = mime or "application/octet-stream"

        # Replace existing file if present
        existing = service.files().list(
            q=f"name='{p.name}' and '{guide_folder_id}' in parents and trashed=false",
            fields="files(id)",
            pageSize=1,
        ).execute().get("files", [])

        media = MediaFileUpload(str(p), mimetype=mime, resumable=True)
        if existing:
            f = service.files().update(
                fileId=existing[0]["id"], media_body=media, fields="id,webViewLink"
            ).execute()
        else:
            meta = {"name": p.name, "parents": [guide_folder_id]}
            f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()

        file_id = f["id"]
        set_public(service, file_id)
        out[p.name] = to_direct_view_link(f.get("webViewLink", ""))
        logger.success(f"Uploaded {p.name} → guide '{guide_name}'")

    return out


def list_guides(parent_folder_name: str) -> list[dict]:
    """
    List guide subfolders inside the parent folder.
    Returns [{id, name, modifiedTime, files: [{id, name, thumbnailLink, modifiedTime}]}].
    """
    service = get_service()
    parent_id = ensure_folder(service, parent_folder_name)

    q = f"mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false"
    folders = service.files().list(
        q=q,
        fields="files(id,name,modifiedTime)",
        orderBy="name",
        pageSize=100,
    ).execute().get("files", [])

    guides = []
    for folder in folders:
        files_resp = service.files().list(
            q=f"'{folder['id']}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
            fields="files(id,name,thumbnailLink,modifiedTime)",
            pageSize=200,
        ).execute()
        guides.append({
            "id": folder["id"],
            "name": folder["name"],
            "modifiedTime": folder.get("modifiedTime", ""),
            "files": files_resp.get("files", []),
        })
    return guides


def delete_guide_folder(folder_id: str) -> None:
    """Permanently delete a guide folder and all its contents."""
    service = get_service()
    service.files().delete(fileId=folder_id).execute()
    logger.info(f"Deleted guide folder {folder_id}")


def test_drive_connection():
    """Test function to verify Drive API connectivity"""
    try:
        logger.info("Testing Google Drive connection...")
        service = get_service()
        
        # Try to list some files to test connection
        results = service.files().list(pageSize=1, fields="files(id,name)").execute()
        files = results.get('files', [])
        
        logger.success("✅ Google Drive connection successful")
        logger.info(f"Found {len(files)} file(s) in Drive")
        return True
        
    except Exception as e:
        logger.error(f"❌ Google Drive connection failed: {e}")
        return False