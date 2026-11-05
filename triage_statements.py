#!/usr/bin/env python3
"""
triage_statements.py — first-pass tool for a pile of E*TRADE PDF statements.

Does two things:

1. RENAME. E*TRADE names downloads with the date you clicked, not the
   statement period. This reads the period out of page 1 and renames to
   <ACCT>_2021-05.pdf so the pile sorts correctly.

2. DIAGNOSE. Before building a real parser, we need to know whether the
   text layer in your original PDFs is intact. Prints, per file, whether
   the trade-table amount columns extract cleanly.

Usage:
    pip install pdfplumber
    python triage_statements.py ~/Downloads/etrade --apply
    (omit --apply for a dry run)
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pip install pdfplumber")

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

PERIOD_RE = re.compile(
    r"(" + "|".join(MONTHS) + r")\s+\d{1,2},\s+(\d{4})\s*[-–]\s*"
    r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})")
ACCT_RE = re.compile(r"Account Number:\s*([\dX*-]+)")

# A trade row: trade date, settlement date, ... side, qty, price, amount
TRADE_RE = re.compile(
    r"(\d{2}/\d{2}/\d{2})\D*?(\d{2}/\d{2}/\d{2}).*?\b(Bought|Sold)\b\s+"
    r"(-?[\d,]+)\s+([\d,]+\.\d{2,4})\s+([\d,]+\.\d{2})")


def read_period(text):
    """Return (year, month, end_day, account) from page-1 text."""
    period = PERIOD_RE.search(text)
    acct = ACCT_RE.search(text)
    if not period:
        return None
    _, _, end_month, end_day, end_year = period.groups()
    return int(end_year), MONTHS[end_month], int(end_day), (
        acct.group(1) if acct else "unknown")


def diagnose(pdf):
    """Count trade-like rows and how many have a complete amount column."""
    rows = complete = 0
    for page in pdf.pages:
        text = page.extract_text(layout=True) or ""
        if "SECURITIES PURCHASED OR SOLD" not in text:
            continue
        for line in text.splitlines():
            if re.search(r"\b(Bought|Sold)\b", line):
                rows += 1
                if TRADE_RE.search(line):
                    complete += 1
    return rows, complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="actually rename (default is a dry run)")
    args = ap.parse_args()

    files = sorted(args.folder.glob("*.pdf"))
    if not files:
        sys.exit(f"no PDFs in {args.folder}")

    print(f"{'file':<34} {'period':<9} {'rows':>6} {'clean':>6}  status")
    print("-" * 78)

    seen, total_rows, total_clean = {}, 0, 0

    for path in files:
        try:
            with pdfplumber.open(path) as pdf:
                first = pdf.pages[0].extract_text() or ""
                meta = read_period(first)
                rows, clean = diagnose(pdf)
        except Exception as exc:
            print(f"{path.name:<34} {'ERROR':<9} {'':>6} {'':>6}  {exc}")
            continue

        total_rows += rows
        total_clean += clean

        if not meta:
            print(f"{path.name:<34} {'?':<9} {rows:>6} {clean:>6}  "
                  "could not read period")
            continue

        year, month, _, acct = meta
        tag = f"{year}-{month:02d}"
        acct_tag = acct.replace("-", "")
        new_name = f"{acct_tag}_{tag}.pdf"

        if tag in seen:
            print(f"{path.name:<34} {tag:<9} {rows:>6} {clean:>6}  "
                  f"DUPLICATE of {seen[tag]}")
            continue
        seen[tag] = path.name

        pct = f"{100 * clean / rows:.0f}%" if rows else "n/a"
        note = f"-> {new_name}  ({pct} parsed)"
        print(f"{path.name:<34} {tag:<9} {rows:>6} {clean:>6}  {note}")

        if args.apply and path.name != new_name:
            shutil.move(path, path.with_name(new_name))

    print("-" * 78)
    if seen:
        months = sorted(seen)
        print(f"{len(seen)} statements, {months[0]} .. {months[-1]}")
        # report gaps
        want = []
        y, m = map(int, months[0].split("-"))
        ey, em = map(int, months[-1].split("-"))
        while (y, m) <= (ey, em):
            want.append(f"{y}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        missing = [t for t in want if t not in seen]
        print("MISSING:", ", ".join(missing) if missing else "none")

    if total_rows:
        print(f"trade rows: {total_rows}, fully parsed: {total_clean} "
              f"({100 * total_clean / total_rows:.1f}%)")
        if total_clean / total_rows < 0.95:
            print("NOTE: text layer is lossy — the parser will need "
                  "word-position extraction rather than line regex.")

    if not args.apply:
        print("\n(dry run — re-run with --apply to rename)")


if __name__ == "__main__":
    main()
