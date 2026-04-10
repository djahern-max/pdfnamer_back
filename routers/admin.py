"""
routers/admin.py  –  PDF Auto-Namer

Internal admin endpoints for provisioning tenants and managing API keys.
Protect these behind your own admin auth middleware or network policy —
they are NOT exposed to end users.

Endpoints
---------
POST   /admin/tenants                  Create a new tenant
GET    /admin/tenants                  List all tenants
GET    /admin/tenants/{tenant_id}      Tenant detail + usage stats
PATCH  /admin/tenants/{tenant_id}      Enable / disable tenant
POST   /admin/tenants/{tenant_id}/keys Issue a new API key  (raw key shown ONCE)
GET    /admin/tenants/{tenant_id}/keys List API keys (hashes only, never raw)
DELETE /admin/keys/{key_id}            Revoke an API key
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from database import get_db
from models.tenant import ApiKey, PdfNaming, Tenant

router = APIRouter(prefix="/admin", tags=["Admin"])

# ── Simple shared-secret guard ────────────────────────────────────────────────
# Set ADMIN_SECRET in your environment.  Replace with proper auth if needed.
_ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me-in-production")

def _require_admin(x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret")):
    if x_admin_secret != _ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret.")


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateTenantRequest(BaseModel):
    name: str
    slug: str          # unique, URL-safe, e.g. "rye-beach"


class CreateTenantResponse(BaseModel):
    tenant_id: int
    name: str
    slug: str
    api_key: str       # raw key — shown ONCE, store it securely


class PatchTenantRequest(BaseModel):
    is_active: bool


class IssueKeyRequest(BaseModel):
    label: str = "default"


class IssueKeyResponse(BaseModel):
    key_id: int
    label: str
    api_key: str       # raw key — shown ONCE


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/tenants", response_model=CreateTenantResponse, dependencies=[Depends(_require_admin)])
async def create_tenant(body: CreateTenantRequest, db: AsyncSession = Depends(get_db)):
    """
    Provision a new tenant and issue their first API key.
    The raw API key is returned here and NEVER stored — save it immediately.
    """
    # Check slug uniqueness
    existing = await db.execute(select(Tenant).where(Tenant.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Slug '{body.slug}' already taken.")

    tenant = Tenant(name=body.name, slug=body.slug)
    db.add(tenant)
    await db.flush()   # get tenant.id before committing

    raw_key, key_hash = ApiKey.generate()
    key_row = ApiKey(tenant_id=tenant.id, label="default", key_hash=key_hash)
    db.add(key_row)
    await db.commit()

    return CreateTenantResponse(
        tenant_id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        api_key=raw_key,
    )


@router.get("/tenants", dependencies=[Depends(_require_admin)])
async def list_tenants(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tenant).order_by(desc(Tenant.created_at)))
    tenants = result.scalars().all()

    # Attach PDF count per tenant
    counts_result = await db.execute(
        select(PdfNaming.tenant_id, func.count(PdfNaming.id).label("total"))
        .group_by(PdfNaming.tenant_id)
    )
    counts = {row.tenant_id: row.total for row in counts_result}

    return [
        {
            "id": t.id,
            "name": t.name,
            "slug": t.slug,
            "is_active": t.is_active,
            "pdfs_analyzed": counts.get(t.id, 0),
            "created_at": t.created_at.isoformat(),
        }
        for t in tenants
    ]


@router.get("/tenants/{tenant_id}", dependencies=[Depends(_require_admin)])
async def get_tenant(tenant_id: int, db: AsyncSession = Depends(get_db)):
    tenant = await _get_or_404(db, tenant_id)

    # Usage stats
    pdf_count = await db.scalar(
        select(func.count(PdfNaming.id)).where(PdfNaming.tenant_id == tenant_id)
    )
    confirmed_count = await db.scalar(
        select(func.count(PdfNaming.id))
        .where(PdfNaming.tenant_id == tenant_id, PdfNaming.confirmed_name.isnot(None))
    )
    keys_result = await db.execute(select(ApiKey).where(ApiKey.tenant_id == tenant_id))
    keys = keys_result.scalars().all()

    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat(),
        "stats": {
            "pdfs_analyzed": pdf_count,
            "patterns_confirmed": confirmed_count,
        },
        "api_keys": [
            {
                "id": k.id,
                "label": k.label,
                "is_active": k.is_active,
                "created_at": k.created_at.isoformat(),
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            }
            for k in keys
        ],
    }


@router.patch("/tenants/{tenant_id}", dependencies=[Depends(_require_admin)])
async def patch_tenant(
    tenant_id: int, body: PatchTenantRequest, db: AsyncSession = Depends(get_db)
):
    tenant = await _get_or_404(db, tenant_id)
    tenant.is_active = body.is_active
    await db.commit()
    return {"id": tenant.id, "is_active": tenant.is_active}


@router.post("/tenants/{tenant_id}/keys", response_model=IssueKeyResponse, dependencies=[Depends(_require_admin)])
async def issue_api_key(
    tenant_id: int, body: IssueKeyRequest, db: AsyncSession = Depends(get_db)
):
    """Issue an additional API key for a tenant (e.g. for multiple apps)."""
    await _get_or_404(db, tenant_id)

    raw_key, key_hash = ApiKey.generate()
    key_row = ApiKey(tenant_id=tenant_id, label=body.label, key_hash=key_hash)
    db.add(key_row)
    await db.commit()
    await db.refresh(key_row)

    return IssueKeyResponse(key_id=key_row.id, label=key_row.label, api_key=raw_key)


@router.get("/tenants/{tenant_id}/keys", dependencies=[Depends(_require_admin)])
async def list_keys(tenant_id: int, db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, tenant_id)
    result = await db.execute(select(ApiKey).where(ApiKey.tenant_id == tenant_id))
    keys = result.scalars().all()
    return [
        {
            "id": k.id,
            "label": k.label,
            "is_active": k.is_active,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        }
        for k in keys
    ]


@router.delete("/keys/{key_id}", dependencies=[Depends(_require_admin)])
async def revoke_key(key_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(404, "API key not found.")
    key.is_active = False
    await db.commit()
    return {"message": f"Key {key_id} revoked."}


# ── Util ──────────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, tenant_id: int) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, "Tenant not found.")
    return tenant
