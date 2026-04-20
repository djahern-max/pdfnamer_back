"""
routers/usage_stats.py  –  Claude API Usage Stats
--------------------------------------------------
Returns aggregate token usage and estimated cost for this tenant.

Haiku 4.5 pricing (as of 2026):
  Input:  $1.00 / 1M tokens
  Output: $5.00 / 1M tokens

Routes:
  GET /api/usage-stats   Return token totals + estimated cost
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from auth import require_tenant
from models.tenant import PdfNaming, Tenant

router = APIRouter(prefix="/api/usage-stats", tags=["Usage Stats"])

# Haiku 4.5 pricing per million tokens
INPUT_COST_PER_M = 1.00
OUTPUT_COST_PER_M = 5.00


class UsageStatsResponse(BaseModel):
    total_pdfs_analyzed: int
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float


@router.get("", response_model=UsageStatsResponse)
async def get_usage_stats(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            func.count(PdfNaming.id).label("total_pdfs"),
            func.coalesce(func.sum(PdfNaming.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(PdfNaming.output_tokens), 0).label("output_tokens"),
        ).where(PdfNaming.tenant_id == tenant.id)
    )
    row = result.one()

    input_cost = (row.input_tokens / 1_000_000) * INPUT_COST_PER_M
    output_cost = (row.output_tokens / 1_000_000) * OUTPUT_COST_PER_M
    total_cost = round(input_cost + output_cost, 4)

    return UsageStatsResponse(
        total_pdfs_analyzed=row.total_pdfs,
        total_input_tokens=row.input_tokens,
        total_output_tokens=row.output_tokens,
        estimated_cost_usd=total_cost,
    )
