#!/usr/bin/env python3
"""
dump_page.py — show what pdfplumber actually extracts from a statement page.

v2: handles this PDF's two quirks —
    * spaces dropped inside text runs ("AccountNumber:", "AccountAtAGlance")
    * (cid:NN) escapes, where NN is the ASCII codepoint

Usage:
    python dump_page.py <file.pdf>          # cover + first trade page
    python dump_page.py <file.pdf> 7        # a specific page
    python dump_page.py <file.pdf> --map    # list all section headings
"""

import re
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pip install pdfplumber")

RULE = "=" * 72
CID = re.compile(r"\(cid:(\d+)\)")

# Match headings regardless of dropped/extra spaces.
TRADE_HEAD = re.compile(r"SECURITIES\s*PURCHASED\s*OR\s*SOLD", re.I)
HEADING = re.compile(
    r"(SECURITIES\s*PURCHASED\s*OR\s*SOLD"
    r"|TRANSACTION\s*HISTORY"
    r"|ACCOUNT\s*HOLDINGS"
    r"|WITHDRAWALS\s*&?\s*DEPOSITS"
    r"|DIVIDENDS?\s*(AND|&)?\s*INTEREST"
    r"|ACCOUNT\s*SUMMARY"
    r"|OPTIONS?\b)", re.I)


def decode(text):
    """Turn (cid:NN) escapes into characters. NN is an ASCII codepoint here."""
    if not text:
        return ""
    return CID.sub(lambda m: chr(int(m.group(1))), text)


def squash(text):
    """Whitespace-free form, for matching headings with dropped spaces."""
    return re.sub(r"\s+", "", text or "")


def looks_like_trades(text):
    text = decode(text)
    return bool(TRADE_HEAD.search(text)) or \
        "SECURITIESPURCHASEDORSOLD" in squash(text).upper()


def dump(page, label):
    print(f"\n{RULE}\n{label}  (page {page.page_number})\n{RULE}")

    plain = decode(page.extract_text() or "")
    print("\n--- extract_text() first 30 lines (cid-decoded) ---")
    for line in plain.splitlines()[:30]:
        print(repr(line))

    lay = decode(page.extract_text(layout=True) or "")
    print("\n--- extract_text(layout=True) first 30 non-blank lines ---")
    shown = 0
    for line in lay.splitlines():
        if not line.strip():
            continue
        print(repr(line[:170]))
        shown += 1
        if shown >= 30:
            break

    words = page.extract_words()
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"]), []).append(w)

    print("\n--- word x-positions, 3 sample data rows ---")
    shown = 0
    for top in sorted(rows):
        row = sorted(rows[top], key=lambda w: w["x0"])
        joined = decode(" ".join(w["text"] for w in row))
        if not re.search(r"(Bought|Sold|Account\s*Number)", joined, re.I):
            continue
        print(f"\n  top={top}: {joined[:130]}")
        for w in row:
            print(f"    x0={w['x0']:7.1f} x1={w['x1']:7.1f}  "
                  f"{decode(w['text'])!r}")
        shown += 1
        if shown >= 3:
            break
    if not shown:
        print("  (no matching rows on this page)")


def map_headings(pdf):
    print(f"\n{RULE}\nSECTION HEADINGS BY PAGE\n{RULE}")
    for page in pdf.pages:
        text = decode(page.extract_text() or "")
        hits = {m.group(0).strip() for m in HEADING.finditer(text)}
        flat = squash(text).upper()
        for probe in ["SECURITIESPURCHASEDORSOLD", "TRANSACTIONHISTORY",
                      "ACCOUNTHOLDINGS", "DIVIDENDSANDINTEREST"]:
            if probe in flat:
                hits.add(probe + " (space-dropped)")
        marker = "  <-- trades" if looks_like_trades(text) else ""
        print(f"  p{page.page_number:>3}: "
              f"{', '.join(sorted(hits)) or '(none matched)'}{marker}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1]).expanduser()
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    with pdfplumber.open(path) as pdf:
        print(f"{path.name}: {len(pdf.pages)} pages")

        if arg == "--map":
            map_headings(pdf)
            return

        if arg:
            dump(pdf.pages[int(arg) - 1], "REQUESTED PAGE")
            return

        dump(pdf.pages[0], "COVER PAGE")

        for page in pdf.pages:
            if looks_like_trades(page.extract_text()):
                dump(page, "FIRST TRADE PAGE")
                break
        else:
            print("\nStill no trade page matched. Heading inventory:")
            map_headings(pdf)


if __name__ == "__main__":
    main()
