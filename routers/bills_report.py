"""
routers/bills_report.py  –  Bills to Enter Report
--------------------------------------------------
Routes:
  GET /api/bills-report/pending          All bills needing QB entry (JSON)
  POST /api/bills-report/mark-entered    Mark a bill as entered in QB
  POST /api/bills-report/mark-unmark     Unmark a bill
  GET /api/bills-report/export.xlsx      Download as Excel workbook
"""

import io
import re
from datetime import datetime, date, timezone

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from database import get_db
from auth import require_tenant
from models.tenant import PdfNaming, Tenant

router = APIRouter(prefix="/api/bills-report", tags=["Bills Report"])

_SKIP_TYPES = {"cc_receipt", "check_receipt"}


def _fmt_date(raw: str | None) -> str:
    if not raw or not re.match(r"^\d{8}$", raw):
        return raw or ""
    return f"{raw[0:2]}/{raw[2:4]}/{raw[4:8]}"


class BillItem(BaseModel):
    confirmed_name: str
    vendor: str | None
    invoice_number: str | None
    amount: str | None
    doc_date: str | None
    doc_type: str | None
    job_name: str | None
    entered_at: datetime | None


class MarkEnteredRequest(BaseModel):
    confirmed_name: str


def _to_item(r: PdfNaming) -> BillItem:
    return BillItem(
        confirmed_name=r.confirmed_name,
        vendor=r.vendor,
        invoice_number=r.invoice_number,
        amount=r.amount,
        doc_date=_fmt_date(r.doc_date),
        doc_type=r.doc_type,
        job_name=r.job_name,
        entered_at=r.entered_at,
    )


@router.get("/pending", response_model=list[BillItem])
async def pending_bills(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
    since: date | None = Query(
        default=None,
        description="Filter to bills confirmed on or after this date (YYYY-MM-DD)",
    ),
):
    stmt = (
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            ~PdfNaming.doc_type.in_(_SKIP_TYPES),
        )
        .order_by(PdfNaming.created_at.desc())
    )
    if since:
        stmt = stmt.where(
            PdfNaming.created_at >= datetime.combine(since, datetime.min.time())
        )

    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_to_item(r) for r in rows]


@router.post("/mark-entered", response_model=BillItem)
async def mark_entered(
    body: MarkEnteredRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Stamp entered_at = now on the matching pdf_naming row."""
    result = await db.execute(
        select(PdfNaming).where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name == body.confirmed_name,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        from fastapi import HTTPException

        raise HTTPException(404, "Bill not found.")
    row.entered_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return _to_item(row)


@router.post("/unmark-entered", response_model=BillItem)
async def unmark_entered(
    body: MarkEnteredRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Clear entered_at — useful if you made an error."""
    result = await db.execute(
        select(PdfNaming).where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name == body.confirmed_name,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        from fastapi import HTTPException

        raise HTTPException(404, "Bill not found.")
    row.entered_at = None
    await db.commit()
    await db.refresh(row)
    return _to_item(row)


@router.get("/export.xlsx")
async def export_bills_excel(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
            ~PdfNaming.doc_type.in_(_SKIP_TYPES),
        )
        .order_by(PdfNaming.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    wb = openpyxl.Workbook()

    # ── Sheet 1: Needs Entry ───────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Needs Entry"
    _write_sheet(ws1, [r for r in rows if not r.entered_at], header_color="1E3A5F")

    # ── Sheet 2: Already Entered ───────────────────────────────────────────
    ws2 = wb.create_sheet("Already Entered")
    _write_sheet(ws2, [r for r in rows if r.entered_at], header_color="166534")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=bills_to_enter.xlsx"},
    )


def _write_sheet(ws, rows, header_color: str):
    headers = ["Vendor", "Bill Date", "Bill No.", "Amount", "Type", "Job", "Entered At"]
    hfont = Font(bold=True, color="FFFFFF", size=11)
    hfill = PatternFill("solid", fgColor=header_color)
    hborder = Border(bottom=Side(style="medium", color="CCCCCC"))

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill
        cell.border = hborder
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_idx, r in enumerate(rows, 2):
        entered = r.entered_at.strftime("%m/%d/%Y %H:%M") if r.entered_at else ""
        values = [
            r.vendor or "",
            _fmt_date(r.doc_date),
            r.invoice_number or "",
            float(r.amount) if r.amount else "",
            r.doc_type or "",
            r.job_name or "",
            entered,
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = Alignment(vertical="center")
            if col == 4 and isinstance(val, float):
                cell.number_format = '"$"#,##0.00'

    col_widths = [28, 12, 18, 12, 14, 20, 20]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 22
