#!/usr/bin/env python3
"""
bulk_import.py — Parse confirmed PDF filenames from Downloads and insert into pdf_namings.
Usage: python3 bulk_import.py
"""

import os
import re
import psycopg2
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
DB_URL = "postgresql://ryze.ai:@localhost/pdfnamer"
TENANT_ID = 1  # <-- replace with your actual tenant ID
DOWNLOADS = os.path.expanduser("~/Downloads")

# ── Helpers ───────────────────────────────────────────────────────────────────
PAYMENT_RE = re.compile(r"^(Visa|Mastercard|Amex|Card|MC)_(\d{4})$", re.IGNORECASE)
AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")
DATE_RE = re.compile(r"^\d{8}$")


def parse_filename(stem: str):
    """
    Parse a confirmed PDF stem into fields.
    Format: MMDDYYYY_Vendor_Amount[_InvoiceOrPayment]
    Vendor may contain spaces or underscores.
    """
    parts = stem.replace(" ", "_").split("_")

    # Date — first 8-digit token
    if not parts or not DATE_RE.match(parts[0]):
        return None
    doc_date = parts[0]
    parts = parts[1:]

    # Amount — find the first float-like token
    amount_idx = next((i for i, p in enumerate(parts) if AMOUNT_RE.match(p)), None)
    if amount_idx is None:
        return None

    vendor = " ".join(parts[:amount_idx]).strip("_").strip()
    amount = parts[amount_idx]
    tail = parts[amount_idx + 1 :]

    # Payment method / invoice number
    payment_method = None
    invoice_number = None
    doc_type = "invoice"

    if tail:
        tail_str = "_".join(tail)
        m = PAYMENT_RE.match(tail_str)
        if m:
            card_type = m.group(1).capitalize()
            last4 = m.group(2)
            payment_method = f"{card_type}_{last4}"
            doc_type = "cc_receipt"
        else:
            invoice_number = tail_str

    return {
        "doc_date": doc_date,
        "vendor": vendor,
        "amount": amount,
        "payment_method": payment_method,
        "invoice_number": invoice_number,
        "doc_type": doc_type,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Collect PDFs — skip duplicates like "(1)" copies
    seen_stems = {}
    for fname in sorted(os.listdir(DOWNLOADS)):
        if not fname.lower().endswith(".pdf"):
            continue
        if " (1)" in fname or " (2)" in fname:
            continue  # skip duplicate downloads
        stem = fname[:-4]  # strip .pdf
        # Prefer underscored version over spaced version (normalise key)
        key = stem.replace(" ", "_")
        if key not in seen_stems:
            seen_stems[key] = stem

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # Fetch already-confirmed names so we don't double-insert
    cur.execute(
        "SELECT confirmed_name FROM pdf_namings WHERE tenant_id = %s AND confirmed_name IS NOT NULL",
        (TENANT_ID,),
    )
    existing = {row[0] for row in cur.fetchall()}

    inserted = skipped = errors = 0
    now = datetime.now(timezone.utc)

    for key, stem in seen_stems.items():
        if stem in existing or key in existing:
            skipped += 1
            continue

        fields = parse_filename(stem)
        if not fields:
            print(f"  ⚠ Could not parse: {stem}.pdf")
            errors += 1
            continue

        cur.execute(
            """
            INSERT INTO pdf_namings
              (tenant_id, original_filename, suggested_name, confirmed_name,
               vendor, doc_date, amount, doc_type, payment_method, invoice_number,
               confidence, pattern_used, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
            (
                TENANT_ID,
                stem + ".pdf",  # original_filename
                stem,  # suggested_name
                stem,  # confirmed_name
                fields["vendor"],
                fields["doc_date"],
                fields["amount"],
                fields["doc_type"],
                fields["payment_method"],
                fields["invoice_number"],
                "high",
                "Bulk import from filename",
                now,
            ),
        )
        print(f"  ✓ {stem}")
        inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    print(
        f"\nDone — {inserted} inserted, {skipped} already existed, {errors} unparseable."
    )


if __name__ == "__main__":
    main()
