"""
routers/pdf_namer.py  –  PDF Auto-Namer (multi-tenant)
-------------------------------------------------------
All routes require X-API-Key header → resolves to a Tenant.
Pattern learning is fully isolated per tenant.

Routes:
  POST   /api/pdf-namer/analyze            Upload PDF → suggested filename
  POST   /api/pdf-namer/confirm            Confirm naming → saves pattern
  GET    /api/pdf-namer/patterns           List this tenant's learned patterns
  DELETE /api/pdf-namer/patterns/{id}      Remove a bad pattern example
"""

import asyncio
import io
import re
import json

import pdfplumber
from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

import anthropic

from database import get_db
from auth import require_tenant
from models.tenant import PdfNaming, Tenant

router = APIRouter(prefix="/api/pdf-namer", tags=["PDF Namer"])

# ✅ Fix 1: AsyncAnthropic instead of Anthropic
_claude = anthropic.AsyncAnthropic()


# ─── Schemas ──────────────────────────────────────────────────────────────────


class AnalyzeResponse(BaseModel):
    suggested_name: str
    extracted_fields: dict
    confidence: str
    pattern_used: str | None
    session_id: str


class ConfirmRequest(BaseModel):
    session_id: str
    confirmed_name: str
    original_filename: str


class ConfirmResponse(BaseModel):
    message: str
    pattern_learned: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _extract_text(file_bytes: bytes) -> str:
    """Synchronous PDF text extraction — always call via run_in_executor."""
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
    return "\n".join(parts)[:6000]


def _build_prompt(pdf_text: str, examples: list[dict]) -> str:
    examples_block = ""
    if examples:
        lines = [
            f'  • Fields: {json.dumps(ex["fields"])}  →  Filename: "{ex["confirmed_name"]}"'
            for ex in examples[-5:]
        ]
        examples_block = (
            "\n\nPrevious confirmed namings for this account (learn this pattern):\n"
            + "\n".join(lines)
        )

    return f"""You are a document-naming assistant.
Given the text of a financial/business document, extract key fields and suggest a filename.{examples_block}

Rules:
- Return ONLY a JSON object, no markdown fences, no prose.
- Keys: vendor, doc_date (MMDDYYYY), amount (digits+dot only, no $), doc_type, suggested_name, confidence ("high"|"medium"|"low")
- For suggested_name: follow the pattern from examples above. If none exist, use: MMDDYYYY_Vendor_Amount
- Underscores only (no spaces). Strip $ from amounts.
- Use DUE DATE for doc_date if present, else statement date.

Document text:
\"\"\"
{pdf_text}
\"\"\"
"""


async def _get_examples(db: AsyncSession, tenant_id: int) -> list[dict]:
    result = await db.execute(
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant_id,
            PdfNaming.confirmed_name.isnot(None),
        )
        .order_by(desc(PdfNaming.created_at))
        .limit(5)
    )
    rows = result.scalars().all()
    return [
        {
            "confirmed_name": r.confirmed_name,
            "fields": {
                "vendor": r.vendor,
                "doc_date": r.doc_date,
                "amount": r.amount,
                "doc_type": r.doc_type,
            },
        }
        for r in rows
    ]


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_pdf(
    file: UploadFile = File(...),
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")

    file_bytes = await file.read()
    if len(file_bytes) > 20 * 1024 * 1024:
        raise HTTPException(400, "File too large (20 MB max).")

    try:
        # ✅ Fix 2: offload blocking pdfplumber call to a thread
        pdf_text = await asyncio.get_event_loop().run_in_executor(
            None, _extract_text, file_bytes
        )
    except Exception as e:
        raise HTTPException(422, f"Could not read PDF: {e}")

    if not pdf_text.strip():
        raise HTTPException(422, "PDF has no extractable text (scanned image).")

    examples = await _get_examples(db, tenant.id)

    prompt = _build_prompt(pdf_text, examples)
    try:
        # ✅ Fix 1: await works correctly now that _claude is AsyncAnthropic
        response = await _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        fields = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(500, "AI returned unexpected format.")
    except Exception as e:
        raise HTTPException(500, f"AI extraction failed: {e}")

    record = PdfNaming(
        tenant_id=tenant.id,
        original_filename=file.filename,
        suggested_name=fields.get("suggested_name", ""),
        vendor=fields.get("vendor"),
        doc_date=fields.get("doc_date"),
        amount=fields.get("amount"),
        doc_type=fields.get("doc_type"),
        confidence=fields.get("confidence", "medium"),
        pattern_used=(
            f"Based on {len(examples)} confirmed example(s)"
            if examples
            else "Default pattern"
        ),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return AnalyzeResponse(
        suggested_name=fields.get("suggested_name", ""),
        extracted_fields={
            "vendor": fields.get("vendor"),
            "date": fields.get("doc_date"),
            "amount": fields.get("amount"),
            "doc_type": fields.get("doc_type"),
        },
        confidence=fields.get("confidence", "medium"),
        pattern_used=record.pattern_used,
        session_id=str(record.id),
    )


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm_naming(
    body: ConfirmRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PdfNaming).where(
            PdfNaming.id == int(body.session_id),
            PdfNaming.tenant_id == tenant.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(404, "Session not found.")

    record.confirmed_name = body.confirmed_name.replace(".pdf", "").strip()
    await db.commit()

    template = record.confirmed_name
    if record.doc_date:
        template = template.replace(record.doc_date, "{MMDDYYYY}")
    if record.vendor:
        template = template.replace(record.vendor, "{Vendor}")
    if record.amount:
        template = template.replace(record.amount, "{Amount}")

    return ConfirmResponse(
        message=f'Saved "{record.confirmed_name}.pdf" and updated your naming pattern.',
        pattern_learned=template,
    )


@router.get("/patterns")
async def list_patterns(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PdfNaming)
        .where(
            PdfNaming.tenant_id == tenant.id,
            PdfNaming.confirmed_name.isnot(None),
        )
        .order_by(desc(PdfNaming.created_at))
        .limit(20)
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "original": r.original_filename,
            "confirmed": r.confirmed_name,
            "vendor": r.vendor,
            "doc_date": r.doc_date,
            "amount": r.amount,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.delete("/patterns/{record_id}")
async def delete_pattern(
    record_id: int,
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
        raise HTTPException(404, "Pattern not found.")
    await db.delete(record)
    await db.commit()
    return {"message": f"Deleted pattern {record_id}."}
