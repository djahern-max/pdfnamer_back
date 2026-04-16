"""
routers/organizer.py  –  PDF Organizer (multi-tenant)
------------------------------------------------------
Reads confirmed PDF namings for a tenant, groups them by vendor,
and moves the actual files on disk into vendor sub-directories.

Route:
  POST /api/organizer/organize   Move files into vendor folders
  GET  /api/organizer/preview    Preview what would be moved (dry-run)

File location is resolved from DOWNLOADS_DIR env var (default: ~/Downloads).
Only confirmed PDFs that actually exist on disk are moved.
"""

import os
import re
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from auth import require_tenant
from models.tenant import PdfNaming, Tenant

router = APIRouter(prefix="/api/organizer", tags=["Organizer"])

DOWNLOADS_DIR = os.path.expanduser(
    os.getenv("DOWNLOADS_DIR", "~/Downloads")
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_dirname(vendor: str) -> str:
    """Convert a vendor name into a safe directory name."""
    # Replace characters that are problematic in directory names
    name = re.sub(r'[<>:"/\\|?*]', '', vendor)
    name = name.strip('. ')
    return name or "Unknown Vendor"


def _find_file(confirmed_name: str) -> str | None:
    """
    Look for the file in Downloads.
    Tries both the exact confirmed_name and with spaces replaced by underscores.
    """
    candidates = [
        os.path.join(DOWNLOADS_DIR, confirmed_name + ".pdf"),
        os.path.join(DOWNLOADS_DIR, confirmed_name.replace(" ", "_") + ".pdf"),
        os.path.join(DOWNLOADS_DIR, confirmed_name.replace("_", " ") + ".pdf"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ─── Schemas ──────────────────────────────────────────────────────────────────

class MoveResult(BaseModel):
    vendor: str
    filename: str
    destination: str
    status: str   # "moved" | "already_there" | "not_found"


class OrganizeResponse(BaseModel):
    results: list[MoveResult]
    moved: int
    already_there: int
    not_found: int
    vendors_created: int


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/preview", response_model=OrganizeResponse)
async def preview_organize(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Dry-run — shows what would be moved without touching any files."""
    return await _run(tenant, db, dry_run=True)


@router.post("/organize", response_model=OrganizeResponse)
async def organize(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Move confirmed PDFs into vendor sub-directories inside Downloads."""
    return await _run(tenant, db, dry_run=False)


async def _run(tenant: Tenant, db: AsyncSession, dry_run: bool) -> OrganizeResponse:
    result = await db.execute(
        select(PdfNaming).where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            PdfNaming.vendor.isnot(None),
        )
    )
    records = result.scalars().all()

    if not records:
        raise HTTPException(404, "No confirmed PDFs found for this tenant.")

    results: list[MoveResult] = []
    vendors_created: set[str] = set()
    moved = already_there = not_found = 0

    for r in records:
        vendor_dir = _safe_dirname(r.vendor)
        dest_folder = os.path.join(DOWNLOADS_DIR, vendor_dir)
        filename = (r.confirmed_name or "") + ".pdf"
        dest_path = os.path.join(dest_folder, filename)

        src_path = _find_file(r.confirmed_name)

        # File is already in the vendor sub-folder
        if src_path and os.path.dirname(src_path) == dest_folder:
            results.append(MoveResult(
                vendor=r.vendor,
                filename=filename,
                destination=dest_folder,
                status="already_there",
            ))
            already_there += 1
            continue

        # File not found on disk at all
        if not src_path:
            results.append(MoveResult(
                vendor=r.vendor,
                filename=filename,
                destination=dest_folder,
                status="not_found",
            ))
            not_found += 1
            continue

        # Move the file
        if not dry_run:
            os.makedirs(dest_folder, exist_ok=True)
            shutil.move(src_path, dest_path)

        vendors_created.add(vendor_dir)
        results.append(MoveResult(
            vendor=r.vendor,
            filename=filename,
            destination=dest_folder,
            status="moved",
        ))
        moved += 1

    # Sort results: moved first, then already_there, then not_found
    order = {"moved": 0, "already_there": 1, "not_found": 2}
    results.sort(key=lambda x: (order[x.status], x.vendor))

    return OrganizeResponse(
        results=results,
        moved=moved,
        already_there=already_there,
        not_found=not_found,
        vendors_created=len(vendors_created),
    )
