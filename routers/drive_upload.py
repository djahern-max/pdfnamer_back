"""
routers/drive_upload.py – Google Drive bulk upload to vendor subfolders
-----------------------------------------------------------------------
Routes:
  GET  /api/drive/vendor-folders          List existing vendor subfolders
  POST /api/drive/create-vendor-folder    Create a new vendor subfolder
  POST /api/drive/upload-file             Upload a single PDF to a folder
"""

import io
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from auth import require_tenant
from models.tenant import Tenant

router = APIRouter(prefix="/api/drive", tags=["Drive Upload"])

# ── Config ────────────────────────────────────────────────────────────────────
# The folder ID is the long string at the end of your Drive folder URL:
# https://drive.google.com/drive/u/0/folders/<FOLDER_ID>
PARENT_FOLDER_ID = os.getenv(
    "DRIVE_VENDOR_FOLDER_ID",
    "1WJwaq-L4rC9QKdUeQvnS9AWa1bZqgnDt",  # your VENDORS_Scanned_and_in_QB folder
)

_GCP_CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "pdfreader_cloud_vision.json",
)

# Drive needs a broader scope than Vision; add this to your service account
SCOPES = ["https://www.googleapis.com/auth/drive"]


# ── Drive client (lazy singleton) ─────────────────────────────────────────────

_drive_service = None


def _get_drive():
    global _drive_service
    if _drive_service is None:
        creds = service_account.Credentials.from_service_account_file(
            _GCP_CREDENTIALS_FILE, scopes=SCOPES
        )
        _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


# ── Schemas ───────────────────────────────────────────────────────────────────


class CreateFolderRequest(BaseModel):
    vendor_name: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/vendor-folders")
async def list_vendor_folders(tenant: Tenant = Depends(require_tenant)):
    """Return all vendor subfolders inside the configured parent Drive folder."""
    try:
        svc = _get_drive()
        results = (
            svc.files()
            .list(
                q=(
                    f"'{PARENT_FOLDER_ID}' in parents"
                    " and mimeType='application/vnd.google-apps.folder'"
                    " and trashed=false"
                ),
                fields="files(id, name)",
                pageSize=500,
            )
            .execute()
        )
        return {"folders": results.get("files", [])}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/create-vendor-folder")
async def create_vendor_folder(
    req: CreateFolderRequest, tenant: Tenant = Depends(require_tenant)
):
    """Create a new vendor subfolder inside the parent Drive folder."""
    try:
        svc = _get_drive()
        metadata = {
            "name": req.vendor_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [PARENT_FOLDER_ID],
        }
        folder = svc.files().create(body=metadata, fields="id, name").execute()
        return {"id": folder["id"], "name": folder["name"]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    folder_id: str = Form(...),
    tenant: Tenant = Depends(require_tenant),
):
    """Upload a PDF into the given Drive folder (by folder_id)."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    try:
        svc = _get_drive()
        content = await file.read()
        metadata = {"name": file.filename, "parents": [folder_id]}
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/pdf")
        uploaded = (
            svc.files()
            .create(body=metadata, media_body=media, fields="id, name, webViewLink")
            .execute()
        )
        return {
            "id": uploaded["id"],
            "name": uploaded["name"],
            "link": uploaded.get("webViewLink", ""),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
