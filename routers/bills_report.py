"""
routers/bills_report.py  –  Bills to Enter Report
--------------------------------------------------
Returns all confirmed PDF namings that are not cc_receipt or check_receipt,
formatted for easy QuickBooks bill entry.

Routes:
  GET /api/bills-report/pending      All bills needing QB entry (JSON)
  GET /api/bills-report/export.xlsx  Download as Excel workbook
"""

import io
import re
from datetime import datetime, date

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from auth import require_tenant
from models.tenant import PdfNaming, Tenant

router = APIRouter(prefix="/api/bills-report", tags=["Bills Report"])

_SKIP_TYPES = {"cc_receipt", "check_receipt"}


def _fmt_date(raw: str | None) -> str:
    """Convert MMDDYYYY → MM/DD/YYYY for QuickBooks."""
    if not raw or not re.match(r"^\d{8}$", raw):
        return raw or ""
    return f"{raw[0:2]}/{raw[2:4]}/{raw[4:8]}"


class BillEntry(BaseModel):
    vendor: str | None
    bill_date: str | None
    bill_no: str | None
    amount: str | None
    doc_type: str | None
    job_name: str | None
    confirmed_name: str


class BillsReportResponse(BaseModel):
    bills: list[BillEntry]
    total: int
    total_amount: float


class BillItem(BaseModel):
    confirmed_name: str
    vendor: str | None
    invoice_number: str | None
    amount: str | None
    doc_date: str | None
    doc_type: str | None
    job_name: str | None


def _to_item(r: PdfNaming) -> BillItem:
    return BillItem(
        confirmed_name=r.confirmed_name,
        vendor=r.vendor,
        invoice_number=r.invoice_number,
        amount=r.amount,
        doc_date=_fmt_date(r.doc_date),
        doc_type=r.doc_type,
        job_name=r.job_name,
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


@router.get("/export.xlsx")
async def export_bills_excel(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Download all pending bills as a formatted Excel workbook."""
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

    bills = [_to_item(r) for r in rows]
    total_amount = sum(float(b.amount or 0) for b in bills)

    data = BillsReportResponse(
        bills=[
            BillEntry(
                vendor=b.vendor,
                bill_date=b.doc_date,
                bill_no=b.invoice_number,
                amount=b.amount,
                doc_type=b.doc_type,
                job_name=b.job_name,
                confirmed_name=b.confirmed_name,
            )
            for b in bills
        ],
        total=len(bills),
        total_amount=total_amount,
    )

    wb = _build_workbook(data)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=bills_to_enter.xlsx"},
    )


# ─── Excel builder ────────────────────────────────────────────────────────────


def _build_workbook(data: BillsReportResponse) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bills to Enter"

    # ── Styles ────────────────────────────────────────────────────────────
    accent = "2563EB"
    accent_light = "DBEAFE"
    vendor_bg = "EFF6FF"
    row_alt = "F9FAFB"
    header_font = Font(bold=True, color="FFFFFF", size=10)
    vendor_font = Font(bold=True, color=accent, size=10)
    total_font = Font(bold=True, size=10)

    thin = Side(style="thin", color="E5E7EB")
    cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def hdr_style(c):
        c.font = header_font
        c.fill = PatternFill("solid", fgColor=accent)
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = cell_border

    def vendor_style(c):
        c.font = vendor_font
        c.fill = PatternFill("solid", fgColor=vendor_bg)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        c.border = Border(
            left=Side(style="medium", color=accent_light),
            right=Side(style="medium", color=accent_light),
            top=Side(style="medium", color=accent_light),
            bottom=Side(style="thin", color=accent_light),
        )

    # ── Title row ─────────────────────────────────────────────────────────
    ws.merge_cells("A1:G1")
    title_cell = ws.cell(
        row=1, column=1, value="Bills to Enter — Rye Beach Landscaping"
    )
    title_cell.font = Font(bold=True, size=13, color="111827")
    title_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:G2")
    sub_cell = ws.cell(
        row=2,
        column=1,
        value=f"Generated {datetime.now().strftime('%B %d, %Y')}  ·  {data.total} bills  ·  Total: ${data.total_amount:,.2f}",
    )
    sub_cell.font = Font(size=9, color="6B7280")
    sub_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 16

    # ── Column headers ────────────────────────────────────────────────────
    headers = ["Vendor", "Bill Date", "Bill No.", "Amount", "Type", "Job", "File Name"]
    ws.row_dimensions[3].height = 22
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=3, column=col, value=h)
        hdr_style(c)

    # ── Column widths ─────────────────────────────────────────────────────
    col_widths = [28, 13, 20, 13, 13, 22, 46]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── Data rows grouped by vendor ───────────────────────────────────────
    groups: dict[str, list] = {}
    for b in data.bills:
        v = b.vendor or "Unknown"
        groups.setdefault(v, []).append(b)

    row = 4
    for vendor in sorted(groups.keys()):
        bills = groups[vendor]
        v_total = sum(float(b.amount or 0) for b in bills)

        # Vendor header row
        ws.merge_cells(f"A{row}:G{row}")
        vc = ws.cell(
            row=row,
            column=1,
            value=f"  {vendor}  —  {len(bills)} bill{'s' if len(bills)!=1 else ''}  ·  ${v_total:,.2f}",
        )
        vendor_style(vc)
        ws.row_dimensions[row].height = 18
        row += 1

        # Bill rows
        for i, b in enumerate(bills):
            fill = PatternFill("solid", fgColor=row_alt if i % 2 == 0 else "FFFFFF")
            amt = float(b.amount or 0)

            values = [
                b.vendor or "",
                b.bill_date or "",
                b.bill_no or "",
                amt,
                b.doc_type or "",
                b.job_name or "",
                b.confirmed_name + ".pdf",
            ]
            for col, val in enumerate(values, 1):
                c = ws.cell(row=row, column=col, value=val)
                c.fill = fill
                c.border = cell_border
                c.font = Font(size=10)

                if col == 4:  # Amount
                    c.number_format = '"$"#,##0.00'
                    c.alignment = Alignment(horizontal="right", vertical="center")
                elif col == 2:  # Date
                    c.alignment = Alignment(horizontal="center", vertical="center")
                elif col == 3:  # Bill No.
                    c.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    c.alignment = Alignment(
                        horizontal="left", vertical="center", indent=1
                    )

            ws.row_dimensions[row].height = 16
            row += 1

    # ── Grand total ───────────────────────────────────────────────────────
    ws.merge_cells(f"A{row}:C{row}")
    lc = ws.cell(row=row, column=1, value=f"  TOTAL  ·  {data.total} bills")
    lc.font = total_font
    lc.fill = PatternFill("solid", fgColor="F3F4F6")
    lc.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    lc.border = cell_border

    tc = ws.cell(row=row, column=4, value=data.total_amount)
    tc.font = Font(bold=True, size=11)
    tc.number_format = '"$"#,##0.00'
    tc.alignment = Alignment(horizontal="right", vertical="center")
    tc.fill = PatternFill("solid", fgColor="F3F4F6")
    tc.border = cell_border

    for col in [5, 6, 7]:
        c = ws.cell(row=row, column=col)
        c.fill = PatternFill("solid", fgColor="F3F4F6")
        c.border = cell_border

    ws.row_dimensions[row].height = 20

    ws.freeze_panes = "A4"

    return wb
