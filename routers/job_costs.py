"""
routers/job_costs.py  –  Job Cost Classification & Reporting
------------------------------------------------------------
Routes:
  GET    /api/job-costs/unassigned         Confirmed invoices with no job classification
  PATCH  /api/job-costs/{record_id}        Assign a job or mark as overhead
  GET    /api/job-costs/unassigned.xlsx    Excel export for sharing with team
  GET    /api/job-costs/summary            Per-job cost totals
"""

import io
from datetime import datetime

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, or_, select

from database import get_db
from auth import require_tenant
from models.tenant import Job, PdfNaming, Tenant

router = APIRouter(prefix="/api/job-costs", tags=["Job Costs"])


def _fmt_date(raw: str | None) -> str:
    if not raw or len(raw) != 8:
        return raw or ""
    return f"{raw[0:2]}/{raw[2:4]}/{raw[4:8]}"


def _unassigned_filter():
    """Never classified OR marked job-cost but no job linked (legacy free-text flow)."""
    return or_(
        PdfNaming.is_job_cost.is_(None),
        and_(PdfNaming.is_job_cost.is_(True), PdfNaming.job_id.is_(None)),
    )


class ClassifyRequest(BaseModel):
    is_job_cost: bool
    job_id: int | None = None


class UnassignedItem(BaseModel):
    id: int
    vendor: str | None
    doc_date: str | None
    invoice_number: str | None
    amount: str | None
    doc_type: str | None
    confirmed_name: str | None
    existing_job_name: str | None


class JobCostSummaryItem(BaseModel):
    job_id: int | None
    job_name: str
    customer: str | None
    total: float
    count: int


@router.get("/unassigned/count")
async def unassigned_count(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(func.count())
        .select_from(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            _unassigned_filter(),
        )
    )
    result = await db.execute(stmt)
    return {"count": result.scalar_one()}


@router.get("/unassigned", response_model=list[UnassignedItem])
async def unassigned_invoices(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            _unassigned_filter(),
        )
        .order_by(PdfNaming.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        UnassignedItem(
            id=r.id,
            vendor=r.vendor,
            doc_date=_fmt_date(r.doc_date),
            invoice_number=r.invoice_number,
            amount=r.amount,
            doc_type=r.doc_type,
            confirmed_name=r.confirmed_name,
            existing_job_name=r.job_name if r.is_job_cost else None,
        )
        for r in rows
    ]


@router.patch("/{record_id}")
async def classify_invoice(
    record_id: int,
    body: ClassifyRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PdfNaming).where(
            PdfNaming.id == record_id,
            PdfNaming.tenant_id == tenant.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(404, "Invoice not found.")

    record.is_job_cost = body.is_job_cost

    if body.is_job_cost:
        if body.job_id is None:
            raise HTTPException(400, "job_id required when is_job_cost is True.")
        job_result = await db.execute(
            select(Job).where(Job.id == body.job_id, Job.tenant_id == tenant.id)
        )
        job = job_result.scalar_one_or_none()
        if not job:
            raise HTTPException(404, "Job not found.")
        record.job_id = job.id
        record.job_name = job.name
    else:
        record.job_id = None
        record.job_name = None

    await db.commit()
    return {"classified": record_id, "is_job_cost": body.is_job_cost}


@router.get("/summary", response_model=list[JobCostSummaryItem])
async def job_cost_summary(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            PdfNaming.is_job_cost.isnot(None),
        )
        .order_by(PdfNaming.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    jobs_result = await db.execute(select(Job).where(Job.tenant_id == tenant.id))
    jobs_map = {j.id: j for j in jobs_result.scalars().all()}

    _OVERHEAD = "__overhead__"
    buckets: dict = {}

    for r in rows:
        if r.is_job_cost:
            if r.job_id is not None:
                key = r.job_id
                if key not in buckets:
                    job = jobs_map.get(key)
                    buckets[key] = {
                        "job_id": key,
                        "job_name": job.name if job else (r.job_name or "Unknown Job"),
                        "customer": job.customer if job else None,
                        "total": 0.0,
                        "count": 0,
                    }
            else:
                # Legacy free-text job_name with no linked job_id
                name_key = f"name:{r.job_name or 'Unknown'}"
                key = name_key
                if key not in buckets:
                    buckets[key] = {
                        "job_id": None,
                        "job_name": r.job_name or "Unknown Job",
                        "customer": None,
                        "total": 0.0,
                        "count": 0,
                    }
        else:
            key = _OVERHEAD
            if key not in buckets:
                buckets[key] = {
                    "job_id": None,
                    "job_name": "Overhead",
                    "customer": None,
                    "total": 0.0,
                    "count": 0,
                }

        buckets[key]["total"] += float(r.amount or 0)
        buckets[key]["count"] += 1

    job_items = sorted(
        [v for k, v in buckets.items() if k != _OVERHEAD],
        key=lambda x: x["job_name"].lower(),
    )
    result_list = job_items + ([buckets[_OVERHEAD]] if _OVERHEAD in buckets else [])
    return [JobCostSummaryItem(**item) for item in result_list]


@router.get("/unassigned.xlsx")
async def export_unassigned_excel(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            _unassigned_filter(),
        )
        .order_by(PdfNaming.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Needs Job Assignment"

    headers = ["Vendor", "Bill Date", "Invoice #", "Amount", "Type", "Filename", "Job (fill in)"]
    hfont = Font(bold=True, color="FFFFFF", size=11)
    hfill = PatternFill("solid", fgColor="1E3A5F")
    hborder = Border(bottom=Side(style="medium", color="CCCCCC"))

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill
        cell.border = hborder
        cell.alignment = Alignment(horizontal="center", vertical="center")

    total_amount = 0.0
    for row_idx, r in enumerate(rows, 2):
        amt = float(r.amount) if r.amount else None
        if amt:
            total_amount += amt
        values = [
            r.vendor or "",
            _fmt_date(r.doc_date),
            r.invoice_number or "",
            amt,
            r.doc_type or "",
            r.confirmed_name or "",
            "",
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = Alignment(vertical="center")
            if col == 4 and val is not None:
                cell.number_format = '"$"#,##0.00'

    total_row = len(rows) + 2
    ws.cell(row=total_row, column=1, value=f"Total ({len(rows)} invoices)").font = Font(bold=True)
    total_cell = ws.cell(row=total_row, column=4, value=total_amount)
    total_cell.font = Font(bold=True)
    total_cell.number_format = '"$"#,##0.00'

    col_widths = [28, 12, 18, 12, 14, 45, 22]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    today = datetime.now().strftime("%Y-%m-%d")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=unassigned_invoices_{today}.xlsx"
        },
    )
