#!/usr/bin/env python3
"""
extract_trades.py — E*TRADE statement PDFs -> trades CSV.

Design notes:
  * Only SECURITIES PURCHASED OR SOLD is parsed. UNSETTLED TRADES is a
    preview of rows that reappear as settled in the following month's
    statement; parsing both double-counts. Use --include-unsettled for
    the FINAL statement of the account, which has no following month.
  * Each trade occupies two lines: data, then execution time.
  * Every row is checked against qty x price. Buys must match exactly;
    sells may come in slightly low (SEC/TAF fees). Failures are
    reported, not silently dropped.

Usage:
    python extract_trades.py ~/Downloads/etrade -o trades.csv
    python extract_trades.py <folder> --include-unsettled 2022-06
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pip install pdfplumber")

CID = re.compile(r"\(cid:(\d+)\)")

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

PERIOD_RE = re.compile(
    r"(" + "|".join(MONTHS) + r")\s+\d{1,2},\s*\d{4}\s*[-\u2013]\s*"
    r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})")
ACCT_RE = re.compile(r"Account\s*Number\s*:\s*([\dX*-]+)")

# Section headings, tolerant of dropped or doubled spaces.
SETTLED_HEAD = re.compile(r"SECURITIES\s*PURCHASED\s*OR\s*SOLD", re.I)
UNSETTLED_HEAD = re.compile(r"UNSETTLED\s*TRADES", re.I)
OTHER_HEAD = re.compile(
    r"(TRANSACTION\s*HISTORY|ACCOUNT\s*HOLDINGS|WITHDRAWALS|DIVIDENDS)", re.I)

# 05/11/21 05/13/21 BRUNSWICK CORP BC Sold -200 110.0280 22,005.46
TRADE_RE = re.compile(
    r"^(?P<trade>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<settle>\d{2}/\d{2}/\d{2})?\s*"
    r"(?P<desc>.+?)\s+"
    r"(?P<symbol>[A-Z][A-Z0-9.\-/]{0,14})\s+"
    r"(?P<side>Bought|Sold)\s+"
    r"(?P<qty>-?[\d,]+)\s+"
    r"(?P<price>[\d,]+\.\d{2,6})\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s*$")

TIME_RE = re.compile(r"^(\d{2}:\d{2})\b")


def decode(text):
    return CID.sub(lambda m: chr(int(m.group(1))), text or "")


def num(raw):
    try:
        return Decimal(raw.replace(",", "").replace("$", ""))
    except (InvalidOperation, AttributeError):
        return None


def statement_period(pdf):
    """(year, month, account) from the cover page."""
    text = decode(pdf.pages[0].extract_text() or "")
    period = PERIOD_RE.search(text)
    acct = ACCT_RE.search(text)
    if not period:
        return None
    _, end_month, _, end_year = period.groups()
    return int(end_year), MONTHS[end_month], acct.group(1) if acct else "?"


def iso(mmddyy):
    if not mmddyy:
        return ""
    return datetime.strptime(mmddyy, "%m/%d/%y").strftime("%Y-%m-%d")


def parse_pdf(path, include_unsettled=False):
    """Yield trade dicts plus a list of problem strings."""
    trades, problems = [], []

    with pdfplumber.open(path) as pdf:
        meta = statement_period(pdf)
        if not meta:
            return [], [f"{path.name}: could not read statement period"], None
        year, month, account = meta
        period = f"{year}-{month:02d}"

        section = None
        for page in pdf.pages:
            lines = decode(page.extract_text() or "").splitlines()

            for idx, line in enumerate(lines):
                # Track which table we're inside.
                if UNSETTLED_HEAD.search(line):
                    section = "unsettled"
                    continue
                if SETTLED_HEAD.search(line):
                    section = "settled"
                    continue
                if OTHER_HEAD.search(line):
                    section = "other"
                    continue

                if section == "unsettled" and not include_unsettled:
                    continue
                if section not in ("settled", "unsettled"):
                    continue

                m = TRADE_RE.match(line.strip())
                if not m:
                    continue

                qty = num(m.group("qty"))
                price = num(m.group("price"))
                amount = num(m.group("amount"))
                if qty is None or price is None or amount is None:
                    problems.append(f"{path.name} p{page.page_number}: "
                                    f"unparseable numbers: {line.strip()[:80]}")
                    continue

                side = m.group("side")
                # Execution time lives on the following line.
                time = ""
                if idx + 1 < len(lines):
                    t = TIME_RE.match(lines[idx + 1].strip())
                    if t:
                        time = t.group(1)

                gross = abs(qty) * price
                delta = gross - amount
                if side == "Bought" and abs(delta) > Decimal("0.02"):
                    problems.append(
                        f"{path.name} p{page.page_number}: buy math off by "
                        f"{delta:+.2f}: {line.strip()[:80]}")
                elif side == "Sold" and not (
                        Decimal("-0.02") <= delta <= Decimal("60.00")):
                    problems.append(
                        f"{path.name} p{page.page_number}: sell math off by "
                        f"{delta:+.2f}: {line.strip()[:80]}")

                trades.append({
                    "account": account,
                    "statement": period,
                    "trade_date": iso(m.group("trade")),
                    "trade_time": time,
                    "settle_date": iso(m.group("settle")),
                    "symbol": m.group("symbol"),
                    "description": " ".join(m.group("desc").split()),
                    "side": side,
                    "quantity": abs(qty),
                    "price": price,
                    "amount": amount,
                    "fees": delta if side == "Sold" else Decimal("0"),
                    "source_page": page.page_number,
                    "unsettled": section == "unsettled",
                })

    return trades, problems, period


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("trades.csv"))
    ap.add_argument("--include-unsettled", metavar="YYYY-MM", default=None,
                    help="also take UNSETTLED TRADES from this statement "
                         "(use for the final statement only)")
    args = ap.parse_args()

    files = sorted(args.folder.expanduser().glob("*.pdf"))
    if not files:
        sys.exit(f"no PDFs in {args.folder}")

    all_trades, all_problems = [], []

    for path in files:
        # Peek at the period so --include-unsettled can target one file.
        try:
            with pdfplumber.open(path) as pdf:
                meta = statement_period(pdf)
        except Exception as exc:
            all_problems.append(f"{path.name}: {exc}")
            continue
        period = f"{meta[0]}-{meta[1]:02d}" if meta else None
        want_unsettled = period == args.include_unsettled

        trades, problems, _ = parse_pdf(path, want_unsettled)
        all_trades.extend(trades)
        all_problems.extend(problems)

    # Cross-month duplicate guard.
    seen, deduped, dupes = set(), [], 0
    for t in all_trades:
        key = (t["trade_date"], t["trade_time"], t["symbol"],
               t["side"], t["quantity"], t["price"])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        deduped.append(t)

    deduped.sort(key=lambda t: (t["trade_date"], t["trade_time"], t["symbol"]))

    fields = ["account", "statement", "trade_date", "trade_time",
              "settle_date", "symbol", "description", "side", "quantity",
              "price", "amount", "fees", "source_page", "unsettled"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in deduped:
            w.writerow(t)

    # Monthly rollup.
    by_month = defaultdict(lambda: {"buys": 0, "sells": 0,
                                    "bought": Decimal(0), "sold": Decimal(0)})
    accounts = set()
    for t in deduped:
        b = by_month[t["statement"]]
        accounts.add(t["account"])
        if t["side"] == "Bought":
            b["buys"] += 1
            b["bought"] += t["amount"]
        else:
            b["sells"] += 1
            b["sold"] += t["amount"]

    print(f"\n{'month':<9} {'buys':>6} {'sells':>6} "
          f"{'purchased':>15} {'sold':>15} {'net':>14}")
    print("-" * 70)
    tot_b = tot_s = Decimal(0)
    for month in sorted(by_month):
        r = by_month[month]
        net = r["sold"] - r["bought"]
        tot_b += r["bought"]
        tot_s += r["sold"]
        print(f"{month:<9} {r['buys']:>6} {r['sells']:>6} "
              f"{r['bought']:>15,.2f} {r['sold']:>15,.2f} {net:>14,.2f}")
    print("-" * 70)
    print(f"{'TOTAL':<9} {'':>6} {'':>6} {tot_b:>15,.2f} {tot_s:>15,.2f} "
          f"{tot_s - tot_b:>14,.2f}")

    print(f"\n{len(deduped)} trades -> {args.out}")
    if dupes:
        print(f"{dupes} duplicate rows suppressed (unsettled/settled overlap)")
    print(f"accounts seen: {', '.join(sorted(accounts))}")

    if all_problems:
        print(f"\n{len(all_problems)} PROBLEMS:")
        for p in all_problems[:25]:
            print(f"  {p}")
        if len(all_problems) > 25:
            print(f"  ... and {len(all_problems) - 25} more")
    else:
        print("\nno arithmetic failures — every row reconciles to qty x price")


if __name__ == "__main__":
    main()
