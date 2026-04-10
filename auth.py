"""
auth.py  –  PDF Auto-Namer

FastAPI dependency that resolves an API key header → Tenant row.
Usage:
    @router.post("/analyze")
    async def analyze(tenant: Tenant = Depends(require_tenant), ...):
        ...
"""

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload
from datetime import datetime, timezone

from database import get_db
from models.tenant import ApiKey, Tenant

# Reads the key from the  X-API-Key  header
_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_tenant(
    raw_key: str | None = Security(_api_key_scheme),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """
    Resolve X-API-Key → active Tenant.
    Raises 401 if key is missing/invalid, 403 if tenant is suspended.
    """
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
        )

    key_hash = ApiKey.hash(raw_key)

    result = await db.execute(
        select(ApiKey)
        .options(joinedload(ApiKey.tenant))
        .where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)
    )
    api_key_row = result.scalar_one_or_none()

    if not api_key_row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
        )

    if not api_key_row.tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended. Contact support.",
        )

    # Stamp last_used_at (fire-and-forget — don't block the request)
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key_row.id)
        .values(last_used_at=datetime.now(timezone.utc))
    )
    await db.commit()

    return api_key_row.tenant
