#!/usr/bin/env python3
"""
extract_cash.py v2 — E*TRADE statements -> external cash flows.

Fixes over v1:
  * Column headers are found before section headings, and the amount
    labels must sit in the right half of the page (x0 > 450). Otherwise
    the section heading "WITHDRAWALS & DEPOSITS (Continued)" is mistaken
    for the column header row "DATE ... WITHDRAWALS DEPOSITS".
  * The NET WITHDRAWALS & DEPOSITS footer is read by column, so we learn
    its DIRECTION. E*TRADE prints it unsigned; which column it lands in
    is the only thing distinguishing $173k in from $173k out.
  * MARK TO MARKET / MARK TO MARKET SHORT POS are internal. They are
    margin adjustments on short positions, booked in both columns, and
    net to zero.

Internal vs external is the whole game here: cash<->margin journal
entries are an order of magnitude larger than the real flows.

Usage:
    python extract_cash.py ~/Downloads/etrade
"""

import argparse
import csv
import re
import sys
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
BEGIN_RE = re.compile(r"Beginning\s*Account\s*Value[^$]*\$\s*([\d,]+\.\d{2})", re.I)
END_RE = re.compile(r"Ending\s*Account\s*Value[^$]*\$\s*([\d,]+\.\d{2})", re.I)

CASH_HEAD = re.compile(r"WITHDRAWALS\s*&\s*DEPOSITS", re.I)
DIV_HEAD = re.compile(r"DIVIDENDS\s*&\s*INTEREST", re.I)
NET_ROW = re.compile(r"NET\s*WITHDRAWALS", re.I)
STOP_HEAD = re.compile(
    r"(SECURITIES\s*PURCHASED|UNSETTLED\s*TRADES|ACCOUNT\s*HOLDINGS"
    r"|EXTENDED\s*INSURANCE\s*SWEEP|ACCOUNT\s*VALUE\s*SUMMARY)", re.I)

DATE_W = re.compile(r"^\d{2}/\d{2}/\d{2}$")
AMT_W = re.compile(r"^-?\$?[\d,]+\.\d{2}$")

# Journal entries between sub-accounts. Booked twice, net to zero.
INTERNAL = re.compile(
    r"(TRANSFER\s*BAL|TFR\b|TRNSFR|CASH\s*TO\s*MARGIN|MARGIN\s*TO\s*CASH"
    r"|FROM\s*MARGIN|TO\s*MARGIN|FROM\s*CASH|TO\s*CASH"
    r"|MARK\s*TO\s*MARKET|MARK\s*TO\s*MKT)", re.I)
INTERNAL_TYPES = {"Adjustment", "Mark"}

MIN_LABEL_X = 450   # amount-column headers live in the right half


def decode(t):
    return CID.sub(lambda m: chr(int(m.group(1))), t or "")


def num(raw):
    try:
        return Decimal(raw.replace(",", "").replace("$", ""))
    except (InvalidOperation, AttributeError):
        return None


def iso(d):
    return datetime.strptime(d, "%m/%d/%y").strftime("%Y-%m-%d") if d else ""


def rows_of(page, tol=3):
    buckets = {}
    for w in page.extract_words():
        w = dict(w, text=decode(w["text"]))
        for top in buckets:
            if abs(top - w["top"]) <= tol:
                buckets[top].append(w)
                break
        else:
            buckets[w["top"]] = [w]
    return [sorted(ws, key=lambda x: x["x0"])
            for _, ws in sorted(buckets.items())]


def column_anchors(ws, left_label, right_label):
    """Right edges of the two amount columns, or None if not a header row."""
    left = right = None
    for w in ws:
        if w["x0"] < MIN_LABEL_X:
            continue
        t = w["text"].upper().strip(":")
        if t == left_label:
            left = w["x1"]
        elif t == right_label:
            right = w["x1"]
    return (left, right) if left and right else None


def split_amounts(ws, anchors):
    left_x, right_x = anchors
    left = right = None
    for w in ws:
        if not AMT_W.match(w["text"]):
            continue
        val = num(w["text"])
        if abs(w["x1"] - left_x) <= abs(w["x1"] - right_x):
            left = val
        else:
            right = val
    return left, right


def parse(path):
    out = {"cash": [], "div": [], "problems": [],
           "begin": None, "end": None, "net_stated": None, "period": None}

    with pdfplumber.open(path) as pdf:
        cover = decode(pdf.pages[0].extract_text() or "")
        period, acct = PERIOD_RE.search(cover), ACCT_RE.search(cover)
        if not period:
            out["problems"].append(f"{path.name}: no statement period")
            return out
        _, em, _, ey = period.groups()
        out["period"] = f"{int(ey)}-{MONTHS[em]:02d}"
        account = acct.group(1) if acct else "?"

        section, anchors = None, None

        for page in pdf.pages:
            flat = decode(page.extract_text() or "")
            if out["begin"] is None and BEGIN_RE.search(flat):
                out["begin"] = num(BEGIN_RE.search(flat).group(1))
            if END_RE.search(flat):
                out["end"] = num(END_RE.search(flat).group(1))

            for ws in rows_of(page):
                line = " ".join(w["text"] for w in ws)
                labels = (("WITHDRAWALS", "DEPOSITS") if section == "cash"
                          else ("DEBITED", "CREDITED"))

                # Column header first — it also matches the section regex.
                if section:
                    got = column_anchors(ws, *labels)
                    if got:
                        anchors = got
                        continue

                # The signed net footer.
                if section == "cash" and anchors and NET_ROW.search(line):
                    wd, dep = split_amounts(ws, anchors)
                    if dep is not None:
                        out["net_stated"] = dep
                    elif wd is not None:
                        out["net_stated"] = -wd
                    continue

                if CASH_HEAD.search(line):
                    section, anchors = "cash", None
                    continue
                if DIV_HEAD.search(line):
                    section, anchors = "div", None
                    continue
                if STOP_HEAD.search(line):
                    section, anchors = None, None
                    continue

                if section is None or not ws or not DATE_W.match(ws[0]["text"]):
                    continue
                if anchors is None:
                    out["problems"].append(
                        f"{path.name} p{page.page_number}: {section} row "
                        f"before headers: {line[:70]}")
                    continue

                left, right = split_amounts(ws, anchors)
                if left is None and right is None:
                    continue

                rest = [w["text"] for w in ws[1:] if not AMT_W.match(w["text"])]
                ttype = rest[0] if rest else ""
                desc = " ".join(rest[1:])
                row = {"account": account, "statement": out["period"],
                       "date": iso(ws[0]["text"]), "type": ttype,
                       "description": desc, "page": page.page_number}

                if section == "cash":
                    row.update(withdrawal=left or "", deposit=right or "",
                               internal=bool(INTERNAL.search(desc))
                               or ttype in INTERNAL_TYPES)
                    out["cash"].append(row)
                else:
                    row.update(debited=left or "", credited=right or "")
                    out["div"].append(row)

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    args = ap.parse_args()

    files = sorted(args.folder.expanduser().glob("*.pdf"))
    if not files:
        sys.exit(f"no PDFs in {args.folder}")

    months, cash, div, problems = {}, [], [], []
    for path in files:
        try:
            out = parse(path)
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        cash.extend(out["cash"])
        div.extend(out["div"])
        problems.extend(out["problems"])
        if out["period"]:
            months[out["period"]] = out

    for name, rows, fields in (
            ("cash_flows.csv", cash,
             ["account", "statement", "date", "type", "description",
              "withdrawal", "deposit", "internal", "page"]),
            ("dividends_interest.csv", div,
             ["account", "statement", "date", "type", "description",
              "debited", "credited", "page"])):
        with open(name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    print(f"\n{'month':<9} {'deposits':>13} {'withdrawals':>13} {'net':>13} "
          f"{'stated':>13}  {'begin':>12} {'end':>12}")
    print("-" * 96)

    tot_d = tot_w = Decimal(0)
    bad = 0
    for tag in sorted(months):
        m = months[tag]
        rows = [c for c in m["cash"] if not c["internal"]]
        dep = sum((c["deposit"] for c in rows if c["deposit"]), Decimal(0))
        wd = sum((c["withdrawal"] for c in rows if c["withdrawal"]), Decimal(0))
        net, stated = dep - wd, m["net_stated"]
        tot_d, tot_w = tot_d + dep, tot_w + wd
        flag = ""
        if stated is not None and abs(net - stated) > Decimal("0.01"):
            flag, bad = "  <-- MISMATCH", bad + 1
        print(f"{tag:<9} {dep:>13,.2f} {wd:>13,.2f} {net:>13,.2f} "
              f"{(stated if stated is not None else 0):>13,.2f}  "
              f"{(m['begin'] or 0):>12,.2f} {(m['end'] or 0):>12,.2f}{flag}")

    print("-" * 96)
    contributed = tot_d - tot_w
    print(f"{'TOTAL':<9} {tot_d:>13,.2f} {tot_w:>13,.2f} "
          f"{contributed:>13,.2f}")

    credited = sum((d["credited"] for d in div if d["credited"]), Decimal(0))
    debited = sum((d["debited"] for d in div if d["debited"]), Decimal(0))
    print(f"\ndividends/interest credited: {credited:>13,.2f}")
    print(f"interest/fees debited:       {debited:>13,.2f}")

    internal_ct = sum(1 for c in cash if c["internal"])
    print(f"\n{len(cash)} cash rows ({internal_ct} internal excluded), "
          f"{len(div)} dividend/interest rows")

    last = max(months) if months else None
    if last and months[last]["end"] is not None:
        final = months[last]["end"]
        print(f"\nfinal account value ({last}): {final:>14,.2f}")
        print(f"net capital contributed:      {contributed:>14,.2f}")
        print(f"ECONOMIC RESULT:              {final - contributed:>14,.2f}")
        print("\n  (deposits in, minus what you took back out, minus what's "
              "left)")

    if bad:
        print(f"\n{bad} month(s) disagree with the statement footer")
    if problems:
        print(f"\n{len(problems)} problems:")
        for p in problems[:20]:
            print(f"  {p}")


if __name__ == "__main__":
    main()
