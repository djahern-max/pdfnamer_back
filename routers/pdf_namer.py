"""
routers/pdf_namer.py  –  PDF Auto-Namer (multi-tenant)
-------------------------------------------------------
All routes require X-API-Key header → resolves to a Tenant.
Pattern learning is fully isolated per tenant.

Text extraction strategy:
  1. pdfplumber  — fast, works on text-based PDFs
  2. Google Cloud Vision OCR — fallback for scanned/image-based PDFs

Routes:
  POST   /api/pdf-namer/analyze            Upload PDF → suggested filename
  POST   /api/pdf-namer/confirm            Confirm naming → saves pattern
  GET    /api/pdf-namer/patterns           List this tenant's learned patterns
  DELETE /api/pdf-namer/patterns/{id}      Remove a bad pattern example
"""

import asyncio
import io
import os
import re
import json

import pdfplumber
from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

import anthropic
from google.cloud import vision
from google.oauth2 import service_account
from pdf2image import convert_from_bytes

from database import get_db
from auth import require_tenant
from models.tenant import PdfNaming, Tenant

router = APIRouter(prefix="/api/pdf-namer", tags=["PDF Namer"])

_claude = anthropic.AsyncAnthropic()

# ─── Google Cloud Vision client ───────────────────────────────────────────────

_GCP_CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "pdfreader_cloud_vision.json",
)

_vision_client: vision.ImageAnnotatorClient | None = None


def _get_vision_client() -> vision.ImageAnnotatorClient:
    global _vision_client
    if _vision_client is None:
        credentials = service_account.Credentials.from_service_account_file(
            _GCP_CREDENTIALS_FILE
        )
        _vision_client = vision.ImageAnnotatorClient(credentials=credentials)
    return _vision_client


# ─── Schemas ──────────────────────────────────────────────────────────────────


class AnalyzeResponse(BaseModel):
    suggested_name: str
    extracted_fields: dict
    confidence: str
    pattern_used: str | None
    session_id: str
    ocr_used: bool = False


class ConfirmRequest(BaseModel):
    session_id: str
    confirmed_name: str
    original_filename: str


class ConfirmResponse(BaseModel):
    message: str
    pattern_learned: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _extract_text_pdfplumber(file_bytes: bytes) -> str:
    """Fast text extraction for text-based PDFs."""
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
    return "\n".join(parts)[:6000]


def _extract_text_vision(file_bytes: bytes) -> str:
    """OCR fallback for scanned/image-based PDFs via Google Cloud Vision."""
    client = _get_vision_client()

    images = convert_from_bytes(file_bytes, dpi=300)
    parts = []

    for image in images:
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format="PNG")
        img_content = img_byte_arr.getvalue()

        vision_image = vision.Image(content=img_content)
        response = client.document_text_detection(image=vision_image)

        if response.error.message:
            raise RuntimeError(f"Vision API error: {response.error.message}")

        if response.full_text_annotation.text:
            parts.append(response.full_text_annotation.text)

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
- Keys: vendor, doc_date (MMDDYYYY), amount (digits+dot only, no $), doc_type, payment_method, suggested_name, confidence ("high"|"medium"|"low")
- For suggested_name: follow the pattern from examples above. If none exist, use: MMDDYYYY_Vendor_Amount
- Underscores only (no spaces). Strip $ from amounts.

Date rules:
- Use DUE DATE for doc_date if present.
- If no due date exists (e.g. receipt paid on the spot), use the transaction/invoice date.

Amount rules:
- If "Amount Due" is $0.00 but a payment was made, use the paid/charged amount instead.
- Always capture the actual money that changed hands.

doc_type rules — use exactly one of these values:
  invoice       -> has a due date, payment expected later
  statement     -> periodic account summary
  cc_receipt    -> paid at point of sale by credit or debit card
  check_receipt -> paid at point of sale by check or cash
  contract      -> rental agreement or service contract
  estimate      -> quote or proposal
  other         -> anything that doesn't fit above

payment_method rules:
- For cc_receipt: format as "Visa_XXXX", "Mastercard_XXXX", "Amex_XXXX" etc. using last 4 digits.
  If card type is unknown, use "Card_XXXX".
- For check_receipt: use "Check" or "Cash".
- For all other doc types: set to null.

Document text:
\"\"\"{pdf_text}\"\"\"
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
                "payment_method": r.payment_method,
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

    # ── Step 1: try pdfplumber ─────────────────────────────────────────────
    try:
        pdf_text = await asyncio.get_event_loop().run_in_executor(
            None, _extract_text_pdfplumber, file_bytes
        )
    except Exception as e:
        raise HTTPException(422, f"Could not read PDF: {e}")

    # ── Step 2: fall back to Cloud Vision OCR if no text found ────────────
    ocr_used = False
    if not pdf_text.strip():
        try:
            pdf_text = await asyncio.get_event_loop().run_in_executor(
                None, _extract_text_vision, file_bytes
            )
            ocr_used = True
        except Exception as e:
            raise HTTPException(422, f"OCR failed on scanned PDF: {e}")

    if not pdf_text.strip():
        raise HTTPException(
            422,
            "Could not extract text from PDF (pdfplumber and OCR both returned empty).",
        )

    examples = await _get_examples(db, tenant.id)
    prompt = _build_prompt(pdf_text, examples)

    try:
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
        payment_method=fields.get("payment_method"),
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
            "payment_method": fields.get("payment_method"),
        },
        confidence=fields.get("confidence", "medium"),
        pattern_used=record.pattern_used,
        session_id=str(record.id),
        ocr_used=ocr_used,
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
    if record.payment_method:
        template = template.replace(record.payment_method, "{PaymentMethod}")

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
            "doc_type": r.doc_type,
            "payment_method": r.payment_method,
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
