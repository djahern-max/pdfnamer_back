"""
routers/drive_upload.py – Google Drive bulk upload to vendor subfolders
-----------------------------------------------------------------------
Routes:
  GET  /api/drive/vendor-folders          List existing vendor subfolders
  POST /api/drive/create-vendor-folder    Create a new vendor subfolder
  POST /api/drive/upload-file             Upload a single PDF (auto-creates vendor folder)
"""

import io
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from auth import require_tenant
from database import get_db
from models.tenant import Tenant, VendorAlias

router = APIRouter(prefix="/api/drive", tags=["Drive Upload"])

PARENT_FOLDER_ID = os.getenv(
    "DRIVE_VENDOR_FOLDER_ID",
    "1OdMFNliKHFeeRfnwiSqFv5MaUQM7CRH_",
)

_GCP_CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "pdfreader_cloud_vision.json",
)

SCOPES = ["https://www.googleapis.com/auth/drive"]

_drive_service = None


def _get_drive():
    global _drive_service
    if _drive_service is None:
        creds = service_account.Credentials.from_service_account_file(
            _GCP_CREDENTIALS_FILE, scopes=SCOPES
        )
        _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


async def _resolve_vendor_name(
    vendor_name: str, tenant_id: int, db: AsyncSession
) -> str:
    """
    Look up vendor_name in the alias table.
    Returns canonical_name if an alias exists, otherwise returns vendor_name unchanged.
    e.g. "Eliminator Systems Inc." → "Eliminator Systems Inc"
         "ConvenientMD"           → "Convenient MD LLC"
    """
    result = await db.execute(
        select(VendorAlias).where(
            VendorAlias.tenant_id == tenant_id,
            VendorAlias.invoice_name == vendor_name.strip().lower(),
        )
    )
    row = result.scalar_one_or_none()
    return row.canonical_name if row else vendor_name


def _get_or_create_vendor_folder(svc, vendor_name: str) -> str:
    """Return the Drive folder ID for vendor_name, creating it if needed."""
    results = (
        svc.files()
        .list(
            q=(
                f"'{PARENT_FOLDER_ID}' in parents"
                f" and name = '{vendor_name}'"
                " and mimeType='application/vnd.google-apps.folder'"
                " and trashed=false"
            ),
            fields="files(id, name)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        .execute()
    )
    folders = results.get("files", [])
    if folders:
        return folders[0]["id"]

    metadata = {
        "name": vendor_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [PARENT_FOLDER_ID],
    }
    folder = (
        svc.files()
        .create(
            body=metadata,
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return folder["id"]


class CreateFolderRequest(BaseModel):
    vendor_name: str


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
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
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
        folder = (
            svc.files()
            .create(
                body=metadata,
                fields="id, name",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {"id": folder["id"], "name": folder["name"]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    vendor_name: str = Form(...),
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Upload a PDF into the vendor's Drive folder, creating it if it doesn't exist.
    Resolves vendor_name through the alias table before folder lookup so that
    name variants (e.g. 'Eliminator Systems Inc.' vs 'Eliminator Systems Inc')
    always land in the same folder.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    try:
        svc = _get_drive()

        # Resolve alias before folder lookup — this is the duplicate-folder fix
        resolved_vendor = await _resolve_vendor_name(vendor_name, tenant.id, db)

        folder_id = _get_or_create_vendor_folder(svc, resolved_vendor)
        content = await file.read()
        metadata = {"name": file.filename, "parents": [folder_id]}
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/pdf")
        uploaded = (
            svc.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {
            "id": uploaded["id"],
            "name": uploaded["name"],
            "link": uploaded.get("webViewLink", ""),
            "vendor_folder": resolved_vendor,  # useful for the frontend to confirm which folder was used
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
