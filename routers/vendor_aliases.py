"""
routers/vendor_aliases.py  –  Vendor Alias Management
------------------------------------------------------
Maps invoice vendor name variants to canonical QB vendor names.
Used by QB Checker (matching) and Drive Upload (folder naming).

Routes:
  GET    /api/vendor-aliases              List all aliases for this tenant
  POST   /api/vendor-aliases              Create or update an alias
  DELETE /api/vendor-aliases/{id}         Remove an alias
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from database import get_db
from auth import require_tenant
from models.tenant import VendorAlias, Tenant

router = APIRouter(prefix="/api/vendor-aliases", tags=["Vendor Aliases"])


class AliasRequest(BaseModel):
    invoice_name: str
    canonical_name: str


class AliasResponse(BaseModel):
    id: int
    invoice_name: str
    canonical_name: str


@router.get("", response_model=list[AliasResponse])
async def list_aliases(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VendorAlias)
        .where(VendorAlias.tenant_id == tenant.id)
        .order_by(VendorAlias.invoice_name)
    )
    return [
        AliasResponse(
            id=r.id, invoice_name=r.invoice_name, canonical_name=r.canonical_name
        )
        for r in result.scalars().all()
    ]


@router.post("", response_model=AliasResponse)
async def upsert_alias(
    body: AliasRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create or update alias for invoice_name (upsert by invoice_name per tenant)."""
    result = await db.execute(
        select(VendorAlias).where(
            VendorAlias.tenant_id == tenant.id,
            VendorAlias.invoice_name == body.invoice_name,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.canonical_name = body.canonical_name
    else:
        row = VendorAlias(
            tenant_id=tenant.id,
            invoice_name=body.invoice_name,
            canonical_name=body.canonical_name,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return AliasResponse(
        id=row.id, invoice_name=row.invoice_name, canonical_name=row.canonical_name
    )


@router.delete("/{alias_id}")
async def delete_alias(
    alias_id: int,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VendorAlias).where(
            VendorAlias.id == alias_id,
            VendorAlias.tenant_id == tenant.id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Alias not found.")
    await db.delete(row)
    await db.commit()
    return {"deleted": alias_id}
