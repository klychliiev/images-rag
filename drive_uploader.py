import mimetypes
from pathlib import Path
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from config import settings
from loguru import logger

def get_service():
    """Get authenticated Google Drive service"""
    try:
        creds = None
        token_json = settings.google_token_json
        
        if isinstance(token_json, str):
            token_info = json.loads(token_json)
        else:
            token_info = token_json
            
        creds = Credentials.from_authorized_user_info(token_info, [settings.google_cloud_scopes])
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Google credentials...")
                creds.refresh(Request())
                settings.google_token_json = creds.to_json()
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
                creds = flow.run_local_server(port=0)
                logger.info("OAuth flow completed successfully")
        
        service = build("drive", "v3", credentials=creds)
        logger.info("Google Drive service initialized successfully")
        return service
        
    except Exception as e:
        logger.error(f"Failed to initialize Google Drive service: {e}")
        raise

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