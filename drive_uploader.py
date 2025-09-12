import mimetypes
from pathlib import Path
import json 

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import settings


def _get_service():
    creds = None
    token_json = settings.google_token_json
    if isinstance(token_json, str):
        token_info = json.loads(token_json)
    else:
        token_info = token_json
        
    creds = Credentials.from_authorized_user_info(json.loads(token_info), [settings.google_cloud_scopes])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

            settings.google_token_json = creds.to_json()
        else:
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
            # If you don’t want a token file, you can keep creds only in env/secret store:
            # os.environ["GOOGLE_TOKEN_JSON"] = creds.to_json()

    return build("drive", "v3", credentials=creds)


def ensure_folder(service, folder_name: str) -> str:
    """Find or create a folder by name in My Drive (root)."""
    safe = folder_name.replace("'", "\\'")
    q = f"mimeType='application/vnd.google-apps.folder' and name='{safe}' and trashed=false"
    resp = (
        service.files()
        .list(q=q, spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
    )
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def set_public(service, file_id: str) -> None:
    """Make the file viewable by anyone with the link (needed for Markdown embedding)."""
    service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}
    ).execute()


def _to_direct_view_link(webViewLink: str) -> str:
    """Convert webViewLink to a direct embeddable URL for Markdown."""
    try:
        fid = webViewLink.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=view&id={fid}"
    except Exception:
        return webViewLink


def upload_images_to_folder(
    image_paths: list[Path], folder_name: str
) -> dict[str, str]:
    """
    Upload images to a Drive folder using OAuth credentials.json/token.json.
    Returns {filename: direct_view_url}.
    """
    if not image_paths:
        return {}

    service = _get_service()
    folder_id = ensure_folder(service, folder_name)

    out: dict[str, str] = {}
    for p in image_paths:
        mime, _ = mimetypes.guess_type(str(p))
        mime = mime or "application/octet-stream"
        meta = {"name": p.name, "parents": [folder_id]}
        media = MediaFileUpload(str(p), mimetype=mime, resumable=True)
        f = (
            service.files()
            .create(body=meta, media_body=media, fields="id,name,webViewLink")
            .execute()
        )
        set_public(service, f["id"])
        out[p.name] = _to_direct_view_link(f.get("webViewLink", ""))
    return out
