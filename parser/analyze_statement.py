#!/usr/bin/env python3
"""
analyze_statement.py
Turns the parsed transaction list into an audit/ITR-oriented analysis:
category-wise breakup, monthly cash flow, high-value/audit flags
(269ST, 194N, SFT-style cash thresholds), recurring salary/EMI/rent
detection, and top counterparty rollups. Renders a single
self-contained HTML report (no external JS/CSS dependencies) so it
still opens correctly if the auditor saves/emails just that one file.

This is a preliminary, automated read of the statement to speed up an
auditor's working papers -- not a substitute for professional judgement,
and all figures should be tied back to source documents before relying
on them for a filing. That caveat is also printed in the report itself.
"""

import re
import html
from collections import defaultdict
from datetime import date, datetime

# --------------------------------------------------------------------------
# Category rules, most specific first. First regex to match the narration
# wins. Deliberately keeps the generic UPI/NEFT/RTGS/IMPS "channel" catch-alls
# at the end so a specific purpose (salary, EMI, tax, rent...) is preferred
# over just naming the payment rail.
# --------------------------------------------------------------------------
CATEGORY_RULES = [
    ("Salary / Regular Income",        r"\bSAL(ARY)?\b|\bPAYROLL\b"),
    ("Interest Income",                r"\bINT\.?\s?PD\b|\bINTEREST\b|\bSB\s?INT\b|SAVINGS\s*INTEREST"),
    ("Dividend Income",                r"\bDIVIDEND\b|\bDIV\b"),
    ("Investment Maturity / Redemption", r"\bMATURITY\b|\bREDEMPTION\b|\bFD\s*CLOS|\bNSC\b|\bTD\s*CLOS"),
    ("Loan Disbursement",              r"\bLOAN\s*DISB|\bLOAN\s*CREDIT"),
    ("Cash Deposit",                   r"\bCASH\s*DEP|\bCDM\b|\bCASH\s*CREDIT|\bBY\s*CASH\b|\bSELF\s*DEP"),
    ("Cash Withdrawal",                r"\bATM\b|\bCASH\s*WD|\bSELF\b|\bCASH\s*WITHDRAWAL|\bCSH\s*WDL"),
    ("EMI / Loan Repayment",           r"\bEMI\b|\bLOAN\s*(REPAY|INST)|\bNACH\b|\bECS\b"),
    ("Investment / SIP / Insurance",   r"\bSIP\b|\bMUTUAL\s*FUND|\bMF\b|\bPREMIUM\b|\bLIC\b|\bINSURANCE|\bRD\s*INST"),
    ("Rent",                           r"\bRENT\b"),
    ("Utility / Bill Payment",         r"ELECTRICITY|\bBILL\s?PAY|RECHARGE|BROADBAND|\bDTH\b|GAS\s*BILL|WATER\s*BILL"),
    ("Bank Charges / Fees",            r"\bCHARGE|\bCHG\b|\bAMB\b|SMS\s*CHARGE|\bFEE\b|PENALTY|GST\s*ON"),
    ("Tax Payment (GST/TDS/Income Tax)", r"\bGST\b|\bTDS\b|INCOME\s*TAX|ADVANCE\s*TAX|SELF\s*ASSESSMENT|\bITR\b|CHALLAN"),
    ("Refund / Reversal",              r"REFUND|REVERSAL|REVERSED|\bFAILED\b|CHARGEBACK"),
    ("Card / POS Spend",                r"\bPOS\b|\bCARD\b|\bSWIPE\b"),
    ("Cheque Payment",                  r"\bCHQ\b|\bCHEQUE\b|\bCLG\b"),
    ("UPI Transfer",                    r"\bUPI\b"),
    ("NEFT/RTGS/IMPS Transfer",         r"\bNEFT\b|\bRTGS\b|\bIMPS\b|\bNFT\b"),
]
COMPILED_RULES = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in CATEGORY_RULES]

# Audit-relevant statutory thresholds (India). Kept as named constants so
# they're easy to update if limits change.
CASH_DEPOSIT_SINGLE_269ST = 200000       # Sec 269ST: cash receipt >= 2L in aggregate/single txn/event is restricted
CASH_DEPOSIT_AGGREGATE_SFT = 1000000     # SFT reporting: cash deposits aggregating 10L+ in a FY (savings a/c)
CASH_WITHDRAWAL_DAY_LIMIT = 100000       # commonly-referenced day-level review threshold for cash withdrawals
CASH_WITHDRAWAL_AGGREGATE_194N = 10000000  # Sec 194N: TDS on cash withdrawal > 1 Cr aggregate in FY
LARGE_TXN_THRESHOLD = 500000             # generic "large value, worth a second look" threshold
ROUND_FIGURE_MIN = 50000                 # round-number transactions worth a look, above this size


def classify(narration: str) -> str:
    for name, rx in COMPILED_RULES:
        if rx.search(narration):
            return name
    return "Other"


STOPWORDS = {
    "UPI", "UPIOUT", "NEFT", "RTGS", "IMPS", "NFT", "TFR", "RRN", "TO", "FROM",
    "PAY", "PAYMENT", "PAID", "VIA", "TRF", "IN", "OUT", "MOB", "MB", "POS",
    "ATM", "CASH", "CHQ", "CLG", "REF", "TXN", "ID", "AND", "THE", "FOR",
}


def guess_counterparty(narration: str) -> str:
    """Best-effort extraction of a human-readable counterparty label from
    narration text that varies wildly bank to bank. Not exact -- intended
    for grouping similar counterparties together, not as a legal name."""
    tokens = re.split(r"[\/\-\s@.,]+", narration.upper())
    words = [t for t in tokens
             if len(t) >= 3
             and t not in STOPWORDS
             and not re.match(r"^\d+$", t)
             and not re.match(r"^[0-9A-F]{8,}$", t)]  # drop long ref/id hexes
    if not words:
        return "Unidentified"
    return " ".join(words[:3]).title()


def parse_amount(s):
    if s in (None, ""):
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def month_label(key: str) -> str:
    y, m = key.split("-")
    return datetime(int(y), int(m), 1).strftime("%b %Y")


def build_analysis(transactions, bank_name=None):
    """transactions: list of dicts with keys date (python date or None),
    particulars, withdrawal, deposit, balance (numeric or None)."""

    rows = []
    for t in transactions:
        d = t.get("date")
        w = parse_amount(t.get("withdrawal"))
        dep = parse_amount(t.get("deposit"))
        bal = t.get("balance")
        bal = parse_amount(bal) if bal is not None else None
        narr = t.get("particulars") or ""
        cat = classify(narr)
        rows.append({
            "date": d, "narration": narr, "withdrawal": w, "deposit": dep,
            "balance": bal, "category": cat,
        })

    dated_rows = [r for r in rows if r["date"] is not None]
    dated_rows.sort(key=lambda r: r["date"])

    total_credit = sum(r["deposit"] for r in rows)
    total_debit = sum(r["withdrawal"] for r in rows)
    opening_balance = None
    closing_balance = None
    if dated_rows:
        first_with_bal = next((r for r in dated_rows if r["balance"] is not None), None)
        if first_with_bal:
            opening_balance = first_with_bal["balance"] + first_with_bal["withdrawal"] - first_with_bal["deposit"]
        last_with_bal = next((r for r in reversed(dated_rows) if r["balance"] is not None), None)
        if last_with_bal:
            closing_balance = last_with_bal["balance"]

    balances = [r["balance"] for r in dated_rows if r["balance"] is not None]
    highest_balance = max(balances) if balances else None
    lowest_balance = min(balances) if balances else None
    highest_bal_row = next((r for r in dated_rows if r["balance"] == highest_balance), None) if balances else None
    lowest_bal_row = next((r for r in dated_rows if r["balance"] == lowest_balance), None) if balances else None

    cash_deposit_total = sum(r["deposit"] for r in rows if r["category"] == "Cash Deposit")
    cash_withdrawal_total = sum(r["withdrawal"] for r in rows if r["category"] == "Cash Withdrawal")
    cash_deposit_count = sum(1 for r in rows if r["category"] == "Cash Deposit" and r["deposit"] > 0)
    cash_withdrawal_count = sum(1 for r in rows if r["category"] == "Cash Withdrawal" and r["withdrawal"] > 0)

    cash_credit_pct = (cash_deposit_total / total_credit * 100) if total_credit else 0.0
    cash_debit_pct = (cash_withdrawal_total / total_debit * 100) if total_debit else 0.0

    # ---------------- monthly cash flow ----------------
    months = defaultdict(lambda: {"credit": 0.0, "debit": 0.0, "count": 0,
                                   "first_bal": None, "last_bal": None,
                                   "bal_points": []})
    for r in dated_rows:
        mk = month_key(r["date"])
        m = months[mk]
        m["credit"] += r["deposit"]
        m["debit"] += r["withdrawal"]
        m["count"] += 1
        if r["balance"] is not None:
            if m["first_bal"] is None:
                m["first_bal"] = r["balance"]
            m["last_bal"] = r["balance"]
            m["bal_points"].append((r["date"], r["balance"]))

    monthly = []
    running_open = opening_balance
    for mk in sorted(months.keys()):
        m = months[mk]
        avg_bal = None
        if m["bal_points"]:
            avg_bal = sum(b for _, b in m["bal_points"]) / len(m["bal_points"])
        closing = m["last_bal"] if m["last_bal"] is not None else running_open
        monthly.append({
            "key": mk, "label": month_label(mk),
            "opening": running_open, "credit": m["credit"], "debit": m["debit"],
            "closing": closing, "net": m["credit"] - m["debit"],
            "count": m["count"], "avg_balance": avg_bal,
        })
        running_open = closing

    max_month_flow = max([max(m["credit"], m["debit"]) for m in monthly], default=1) or 1

    # ---------------- category breakup ----------------
    credit_cats = defaultdict(lambda: {"amount": 0.0, "count": 0})
    debit_cats = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for r in rows:
        if r["deposit"] > 0:
            credit_cats[r["category"]]["amount"] += r["deposit"]
            credit_cats[r["category"]]["count"] += 1
        if r["withdrawal"] > 0:
            debit_cats[r["category"]]["amount"] += r["withdrawal"]
            debit_cats[r["category"]]["count"] += 1

    def cats_to_list(cats, total):
        out = [{"category": k, "amount": v["amount"], "count": v["count"],
                "pct": (v["amount"] / total * 100) if total else 0.0}
               for k, v in cats.items()]
        out.sort(key=lambda x: -x["amount"])
        return out

    credit_categories = cats_to_list(credit_cats, total_credit)
    debit_categories = cats_to_list(debit_cats, total_debit)

    # ---------------- audit flags ----------------
    flags = []
    cash_dep_by_day = defaultdict(float)
    cash_wd_by_day = defaultdict(float)
    for r in dated_rows:
        if r["category"] == "Cash Deposit" and r["deposit"] > 0:
            cash_dep_by_day[r["date"]] += r["deposit"]
            if r["deposit"] >= CASH_DEPOSIT_SINGLE_269ST:
                flags.append({
                    "date": r["date"], "narration": r["narration"], "amount": r["deposit"],
                    "type": "Cash deposit \u2265 \u20b92,00,000 (Sec 269ST exposure)",
                    "severity": "high",
                })
        if r["category"] == "Cash Withdrawal" and r["withdrawal"] > 0:
            cash_wd_by_day[r["date"]] += r["withdrawal"]
        if r["deposit"] >= LARGE_TXN_THRESHOLD:
            flags.append({
                "date": r["date"], "narration": r["narration"], "amount": r["deposit"],
                "type": "Large value credit (\u2265 \u20b95,00,000)", "severity": "medium",
            })
        if r["withdrawal"] >= LARGE_TXN_THRESHOLD:
            flags.append({
                "date": r["date"], "narration": r["narration"], "amount": r["withdrawal"],
                "type": "Large value debit (\u2265 \u20b95,00,000)", "severity": "medium",
            })
        for side, amt in (("deposit", r["deposit"]), ("withdrawal", r["withdrawal"])):
            if amt >= ROUND_FIGURE_MIN and amt % 10000 == 0:
                flags.append({
                    "date": r["date"], "narration": r["narration"], "amount": amt,
                    "type": f"Round-figure {side} (possible informal/cash-linked entry)",
                    "severity": "low",
                })

    if cash_deposit_total >= CASH_DEPOSIT_AGGREGATE_SFT:
        flags.append({
            "date": None, "narration": "Aggregate for the statement period",
            "amount": cash_deposit_total,
            "type": "Aggregate cash deposits \u2265 \u20b910,00,000 (SFT reporting range)",
            "severity": "high",
        })
    if cash_withdrawal_total >= CASH_WITHDRAWAL_AGGREGATE_194N:
        flags.append({
            "date": None, "narration": "Aggregate for the statement period",
            "amount": cash_withdrawal_total,
            "type": "Aggregate cash withdrawals \u2265 \u20b91 Crore (Sec 194N TDS exposure)",
            "severity": "high",
        })
    for d, amt in cash_wd_by_day.items():
        if amt >= CASH_WITHDRAWAL_DAY_LIMIT:
            flags.append({
                "date": d, "narration": "Same-day cash withdrawal total",
                "amount": amt, "type": "Cash withdrawal \u2265 \u20b91,00,000 in a single day",
                "severity": "medium",
            })

    # same-day matching deposit/withdrawal pairs -- possible layering
    by_day = defaultdict(lambda: {"credit": 0.0, "debit": 0.0})
    for r in dated_rows:
        by_day[r["date"]]["credit"] += r["deposit"]
        by_day[r["date"]]["debit"] += r["withdrawal"]
    for d, v in by_day.items():
        if v["credit"] > 0 and v["debit"] > 0:
            smaller, larger = sorted([v["credit"], v["debit"]])
            if smaller >= 50000 and smaller / larger >= 0.9:
                flags.append({
                    "date": d, "narration": "Same-day deposit and withdrawal of similar size",
                    "amount": larger, "type": "Possible layering (matching same-day in/out)",
                    "severity": "medium",
                })

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: (severity_rank.get(f["severity"], 3), -(f["amount"] or 0)))

    # ---------------- recurring pattern detection ----------------
    def detect_recurring(rows, field):
        buckets = defaultdict(list)
        for r in dated_rows:
            amt = r[field]
            if amt <= 0:
                continue
            bucket_amt = round(amt / 100) * 100  # tolerate small paise/rounding differences
            buckets[(bucket_amt, r["category"])].append(r)
        patterns = []
        for (amt, cat), items in buckets.items():
            months_seen = {month_key(r["date"]) for r in items}
            if len(months_seen) >= 3:
                items_sorted = sorted(items, key=lambda r: r["date"])
                patterns.append({
                    "category": cat, "amount": amt, "occurrences": len(items),
                    "months": len(months_seen),
                    "first_date": items_sorted[0]["date"], "last_date": items_sorted[-1]["date"],
                    "sample_narration": items_sorted[-1]["narration"][:70],
                })
        patterns.sort(key=lambda p: -p["amount"])
        return patterns

    recurring_credits = detect_recurring(dated_rows, "deposit")
    recurring_debits = detect_recurring(dated_rows, "withdrawal")

    # ---------------- top counterparties ----------------
    payer_totals = defaultdict(lambda: {"amount": 0.0, "count": 0})
    payee_totals = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for r in rows:
        cp = guess_counterparty(r["narration"])
        if r["deposit"] > 0:
            payer_totals[cp]["amount"] += r["deposit"]
            payer_totals[cp]["count"] += 1
        if r["withdrawal"] > 0:
            payee_totals[cp]["amount"] += r["withdrawal"]
            payee_totals[cp]["count"] += 1

    def top_n(d, n=10):
        out = [{"name": k, "amount": v["amount"], "count": v["count"]} for k, v in d.items() if k != "Unidentified"]
        out.sort(key=lambda x: -x["amount"])
        return out[:n]

    top_payers = top_n(payer_totals)
    top_payees = top_n(payee_totals)

    # ---------------- top transactions ----------------
    top_credits = sorted([r for r in rows if r["deposit"] > 0], key=lambda r: -r["deposit"])[:10]
    top_debits = sorted([r for r in rows if r["withdrawal"] > 0], key=lambda r: -r["withdrawal"])[:10]

    period_start = dated_rows[0]["date"] if dated_rows else None
    period_end = dated_rows[-1]["date"] if dated_rows else None

    return {
        "bank_name": bank_name,
        "period_start": period_start, "period_end": period_end,
        "transaction_count": len(rows),
        "opening_balance": opening_balance, "closing_balance": closing_balance,
        "net_change": (closing_balance - opening_balance) if (opening_balance is not None and closing_balance is not None) else None,
        "total_credit": total_credit, "total_debit": total_debit,
        "highest_balance": highest_balance, "highest_bal_date": highest_bal_row["date"] if highest_bal_row else None,
        "lowest_balance": lowest_balance, "lowest_bal_date": lowest_bal_row["date"] if lowest_bal_row else None,
        "cash_deposit_total": cash_deposit_total, "cash_deposit_count": cash_deposit_count,
        "cash_withdrawal_total": cash_withdrawal_total, "cash_withdrawal_count": cash_withdrawal_count,
        "cash_credit_pct": cash_credit_pct, "cash_debit_pct": cash_debit_pct,
        "monthly": monthly, "max_month_flow": max_month_flow,
        "credit_categories": credit_categories, "debit_categories": debit_categories,
        "flags": flags,
        "recurring_credits": recurring_credits, "recurring_debits": recurring_debits,
        "top_payers": top_payers, "top_payees": top_payees,
        "top_credits": top_credits, "top_debits": top_debits,
    }


# ==========================================================================
# HTML report rendering — self-contained (no external JS libraries), so
# the file still works if the auditor saves or emails just this one file.
# ==========================================================================

PALETTE = ["#146356", "#1C8571", "#4B5C6B", "#9C6B12", "#9B3226",
           "#2E7D6B", "#6B8F87", "#B98A3E", "#7A4A3F", "#3E5C6B"]


def inr(n, allow_none=True):
    if n is None:
        return "—" if allow_none else "₹0.00"
    neg = n < 0
    n = abs(round(float(n), 2))
    s = f"{n:.2f}"
    int_part, dec_part = s.split(".")
    if len(int_part) > 3:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        int_part = ",".join(parts) + "," + last3
    return ("-" if neg else "") + "\u20b9" + int_part + "." + dec_part


def fdate(d):
    if not d:
        return "—"
    return d.strftime("%d-%b-%Y")


def esc(s):
    return html.escape(str(s) if s is not None else "")


def sev_label(sev):
    return {"high": "High", "medium": "Medium", "low": "Low"}.get(sev, sev)


def render_html(analysis, meta):
    a = analysis
    generated_at = meta.get("generated_at") or datetime.now().strftime("%d-%b-%Y %H:%M")
    source_file = esc(meta.get("source_file") or "Bank Statement")
    bank_name = esc(a.get("bank_name") or "Auto-detected")
    period = f'{fdate(a["period_start"])} to {fdate(a["period_end"])}' if a["period_start"] else "—"

    # ---- summary cards ----
    cards = [
        ("Opening Balance", inr(a["opening_balance"])),
        ("Closing Balance", inr(a["closing_balance"])),
        ("Net Change", inr(a["net_change"])),
        ("Total Credits", inr(a["total_credit"])),
        ("Total Debits", inr(a["total_debit"])),
        ("Transactions", f'{a["transaction_count"]:,}'),
        ("Highest Balance", f'{inr(a["highest_balance"])} <span class="card-sub">on {fdate(a["highest_bal_date"])}</span>'),
        ("Lowest Balance", f'{inr(a["lowest_balance"])} <span class="card-sub">on {fdate(a["lowest_bal_date"])}</span>'),
        ("Cash Deposits", f'{inr(a["cash_deposit_total"])} <span class="card-sub">{a["cash_credit_pct"]:.1f}% of credits &middot; {a["cash_deposit_count"]} txns</span>'),
        ("Cash Withdrawals", f'{inr(a["cash_withdrawal_total"])} <span class="card-sub">{a["cash_debit_pct"]:.1f}% of debits &middot; {a["cash_withdrawal_count"]} txns</span>'),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="card-label">{esc(label)}</div><div class="card-value">{value}</div></div>'
        for label, value in cards
    )

    # ---- monthly cash flow chart + table ----
    month_bars = []
    for m in a["monthly"]:
        c_h = (m["credit"] / a["max_month_flow"] * 100) if a["max_month_flow"] else 0
        d_h = (m["debit"] / a["max_month_flow"] * 100) if a["max_month_flow"] else 0
        month_bars.append(f'''
        <div class="mbar-col">
          <div class="mbar-pair">
            <div class="mbar credit" style="height:{c_h:.1f}%" title="Credit {inr(m["credit"])}"></div>
            <div class="mbar debit" style="height:{d_h:.1f}%" title="Debit {inr(m["debit"])}"></div>
          </div>
          <div class="mbar-label">{esc(m["label"])}</div>
        </div>''')
    month_chart_html = f'<div class="mbar-legend"><span><i class="dot credit"></i>Credit</span><span><i class="dot debit"></i>Debit</span></div><div class="mbar-chart">{"".join(month_bars)}</div>'

    month_rows = "".join(
        f'<tr><td>{esc(m["label"])}</td><td class="num">{inr(m["opening"])}</td>'
        f'<td class="num credit-text">{inr(m["credit"])}</td><td class="num debit-text">{inr(m["debit"])}</td>'
        f'<td class="num">{inr(m["closing"])}</td><td class="num">{inr(m["avg_balance"])}</td>'
        f'<td class="num">{m["count"]}</td></tr>'
        for m in a["monthly"]
    )
    monthly_table = f'''
    <table class="data-table csv-table" data-csv-title="Monthly Cash Flow">
      <thead><tr><th>Month</th><th>Opening Bal.</th><th>Total Credit</th><th>Total Debit</th>
      <th>Closing Bal.</th><th>Avg. Balance</th><th>Txns</th></tr></thead>
      <tbody>{month_rows}</tbody>
    </table>'''

    # ---- category donuts + tables ----
    def donut_and_table(categories, total, title, side_class):
        if not categories:
            return f'<div class="cat-block"><h3>{title}</h3><p class="muted">No {side_class} transactions found.</p></div>'
        stops = []
        cum = 0.0
        legend_items = []
        for i, c in enumerate(categories):
            color = PALETTE[i % len(PALETTE)]
            start = cum
            cum += c["pct"]
            stops.append(f'{color} {start:.2f}% {cum:.2f}%')
            legend_items.append(
                f'<div class="legend-row"><i class="dot" style="background:{color}"></i>'
                f'<span class="legend-name">{esc(c["category"])}</span>'
                f'<span class="legend-val">{inr(c["amount"])} <span class="muted">({c["pct"]:.1f}%)</span></span></div>'
            )
        gradient = ", ".join(stops)
        rows = "".join(
            f'<tr><td>{esc(c["category"])}</td><td class="num">{c["count"]}</td>'
            f'<td class="num">{inr(c["amount"])}</td><td class="num">{c["pct"]:.1f}%</td></tr>'
            for c in categories
        )
        return f'''
        <div class="cat-block">
          <h3>{title}</h3>
          <div class="cat-flex">
            <div class="donut" style="background:conic-gradient({gradient})"><div class="donut-hole">{inr(total)}</div></div>
            <div class="legend">{"".join(legend_items)}</div>
          </div>
          <table class="data-table csv-table" data-csv-title="{title}">
            <thead><tr><th>Category</th><th>Count</th><th>Amount</th><th>% of Total</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>'''

    credit_cat_html = donut_and_table(a["credit_categories"], a["total_credit"], "Credit Break-up", "credit")
    debit_cat_html = donut_and_table(a["debit_categories"], a["total_debit"], "Debit Break-up", "debit")

    # ---- audit flags ----
    if a["flags"]:
        flag_rows = "".join(
            f'<tr><td>{fdate(f["date"])}</td><td>{esc(f["type"])}</td>'
            f'<td class="narr">{esc(f["narration"])}</td><td class="num">{inr(f["amount"])}</td>'
            f'<td><span class="chip {f["severity"]}">{sev_label(f["severity"])}</span></td></tr>'
            for f in a["flags"]
        )
    else:
        flag_rows = '<tr><td colspan="5" class="muted">No threshold-based flags triggered on this statement.</td></tr>'
    flags_table = f'''
    <table class="data-table csv-table" data-csv-title="Audit Flags">
      <thead><tr><th>Date</th><th>Flag</th><th>Narration</th><th>Amount</th><th>Severity</th></tr></thead>
      <tbody>{flag_rows}</tbody>
    </table>'''

    # ---- recurring patterns ----
    def recurring_table(patterns, title):
        if not patterns:
            return f'<div><h3>{title}</h3><p class="muted">No recurring pattern detected.</p></div>'
        rows = "".join(
            f'<tr><td>{esc(p["category"])}</td><td class="num">{inr(p["amount"])}</td>'
            f'<td class="num">{p["occurrences"]}</td><td class="num">{p["months"]}</td>'
            f'<td>{fdate(p["first_date"])} &rarr; {fdate(p["last_date"])}</td>'
            f'<td class="narr">{esc(p["sample_narration"])}</td></tr>'
            for p in patterns[:15]
        )
        return f'''
        <div>
          <h3>{title}</h3>
          <table class="data-table csv-table" data-csv-title="{title}">
            <thead><tr><th>Category</th><th>Approx. Amount</th><th>Occurrences</th><th>Months</th><th>Span</th><th>Sample narration</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>'''

    recurring_credit_html = recurring_table(a["recurring_credits"], "Likely Recurring Income (Salary / Interest / Rent received etc.)")
    recurring_debit_html = recurring_table(a["recurring_debits"], "Likely Recurring Payments (EMI / SIP / Rent / Insurance etc.)")

    # ---- counterparties ----
    def counterparty_table(items, title):
        if not items:
            return f'<div><h3>{title}</h3><p class="muted">Not enough identifiable counterparties.</p></div>'
        rows = "".join(
            f'<tr><td>{esc(c["name"])}</td><td class="num">{c["count"]}</td><td class="num">{inr(c["amount"])}</td></tr>'
            for c in items
        )
        return f'''
        <div>
          <h3>{title}</h3>
          <table class="data-table csv-table" data-csv-title="{title}">
            <thead><tr><th>Counterparty (best-effort)</th><th>Txns</th><th>Total Amount</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>'''

    top_payers_html = counterparty_table(a["top_payers"], "Top 10 Payers (Money Received From)")
    top_payees_html = counterparty_table(a["top_payees"], "Top 10 Payees (Money Paid To)")

    # ---- top transactions ----
    def top_txn_table(items, title, amount_key):
        rows = "".join(
            f'<tr><td>{fdate(r["date"])}</td><td class="narr">{esc(r["narration"])}</td>'
            f'<td class="num">{inr(r[amount_key])}</td></tr>'
            for r in items
        ) or '<tr><td colspan="3" class="muted">None found.</td></tr>'
        return f'''
        <div>
          <h3>{title}</h3>
          <table class="data-table csv-table" data-csv-title="{title}">
            <thead><tr><th>Date</th><th>Narration</th><th>Amount</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>'''

    top_credits_html = top_txn_table(a["top_credits"], "Top 10 Largest Credits", "deposit")
    top_debits_html = top_txn_table(a["top_debits"], "Top 10 Largest Debits", "withdrawal")

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bank Statement Analysis — {source_file}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;1,9..144,500&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #10243D; --ink-soft: #4B5C6B; --paper: #EEF2F0; --card: #FFFFFF;
    --rule: #D9E0DC; --ledger: #146356; --ledger-2: #1C8571; --ledger-dim: #E4EFEC;
    --amber: #9C6B12; --amber-bg: #FBF2E2; --brick: #9B3226; --brick-bg: #FBECEA;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Inter', system-ui, sans-serif; background: var(--paper); color: var(--ink);
    margin: 0; padding: 0 0 80px; -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 0 24px; }}
  .topbar {{
    position: sticky; top: 0; z-index: 20; background: rgba(238,242,240,0.94);
    backdrop-filter: blur(6px); border-bottom: 1px solid var(--rule);
    padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  }}
  .topbar-title {{ font-family: 'Fraunces', serif; font-weight: 600; font-size: 16px; }}
  .topbar-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .btn {{
    font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; letter-spacing: .04em; text-transform: uppercase;
    border-radius: 4px; padding: 9px 16px; cursor: pointer; border: 1px solid var(--ink); background: #fff; color: var(--ink);
    text-decoration: none; display: inline-flex; align-items: center; gap: 7px; transition: background .15s, color .15s;
  }}
  .btn:hover {{ background: var(--ink); color: #fff; }}
  .btn.primary {{ background: var(--ledger); border-color: var(--ledger); color: #fff; }}
  .btn.primary:hover {{ background: #0F4E43; }}

  header.report-head {{ padding: 34px 0 24px; }}
  .eyebrow {{
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--ledger); margin-bottom: 10px;
  }}
  h1 {{ font-family: 'Fraunces', serif; font-weight: 600; font-size: 28px; margin: 0 0 8px; }}
  .meta-line {{ color: var(--ink-soft); font-size: 13.5px; }}
  .meta-line b {{ color: var(--ink); }}

  h2 {{ font-family: 'Fraunces', serif; font-weight: 600; font-size: 19px; margin: 46px 0 16px; padding-top: 18px; border-top: 1px solid var(--rule); }}
  h3 {{ font-family: 'Inter', sans-serif; font-weight: 600; font-size: 13.5px; text-transform: uppercase; letter-spacing: .03em; color: var(--ink-soft); margin: 0 0 12px; }}

  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px,1fr)); gap: 12px; }}
  .card {{ background: var(--card); border: 1px solid var(--rule); border-radius: 5px; padding: 16px 18px; }}
  .card-label {{ font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-soft); margin-bottom: 8px; }}
  .card-value {{ font-family: 'IBM Plex Mono', monospace; font-size: 17px; font-weight: 600; }}
  .card-sub {{ display: block; font-size: 10.5px; font-weight: 400; color: var(--ink-soft); margin-top: 3px; text-transform: none; letter-spacing: 0; }}

  .mbar-legend {{ display: flex; gap: 18px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--ink-soft); margin-bottom: 10px; }}
  .mbar-legend .dot, .legend .dot {{ width: 8px; height: 8px; border-radius: 2px; display: inline-block; margin-right: 6px; }}
  .dot.credit {{ background: var(--ledger); }} .dot.debit {{ background: var(--brick); }}
  .mbar-chart {{ display: flex; align-items: flex-end; gap: 14px; height: 180px; background: var(--card); border: 1px solid var(--rule); border-radius: 5px; padding: 16px 16px 10px; overflow-x: auto; }}
  .mbar-col {{ display: flex; flex-direction: column; align-items: center; min-width: 46px; height: 100%; justify-content: flex-end; }}
  .mbar-pair {{ display: flex; align-items: flex-end; gap: 3px; height: 140px; }}
  .mbar {{ width: 12px; border-radius: 2px 2px 0 0; min-height: 2px; }}
  .mbar.credit {{ background: var(--ledger); }} .mbar.debit {{ background: var(--brick); }}
  .mbar-label {{ font-family: 'IBM Plex Mono', monospace; font-size: 9.5px; color: var(--ink-soft); margin-top: 8px; white-space: nowrap; }}

  .cat-flex {{ display: flex; gap: 28px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }}
  .donut {{ width: 150px; height: 150px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; }}
  .donut-hole {{ width: 96px; height: 96px; border-radius: 50%; background: var(--paper); display: flex; align-items: center; justify-content: center; font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; text-align: center; padding: 6px; }}
  .legend {{ flex: 1; min-width: 220px; }}
  .legend-row {{ display: flex; align-items: center; font-size: 12.5px; padding: 4px 0; gap: 8px; }}
  .legend-name {{ flex: 1; }}
  .legend-val {{ font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; white-space: nowrap; }}
  .cat-block {{ background: var(--card); border: 1px solid var(--rule); border-radius: 5px; padding: 20px; margin-bottom: 18px; }}

  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; background: var(--card); border: 1px solid var(--rule); border-radius: 5px; overflow: hidden; }}
  table.data-table th {{ text-align: left; font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; letter-spacing: .04em; text-transform: uppercase; color: var(--ink-soft); background: var(--ledger-dim); padding: 9px 12px; }}
  table.data-table td {{ padding: 8px 12px; border-top: 1px solid var(--rule); }}
  table.data-table td.num {{ font-family: 'IBM Plex Mono', monospace; text-align: right; white-space: nowrap; }}
  table.data-table td.narr {{ max-width: 380px; color: var(--ink-soft); }}
  table.data-table td.credit-text {{ color: var(--ledger); }}
  table.data-table td.debit-text {{ color: var(--brick); }}
  .muted {{ color: var(--ink-soft); font-size: 12.5px; }}

  .chip {{ font-family: 'IBM Plex Mono', monospace; font-size: 10px; letter-spacing: .04em; text-transform: uppercase; padding: 3px 9px; border-radius: 10px; }}
  .chip.high {{ background: var(--brick-bg); color: var(--brick); }}
  .chip.medium {{ background: var(--amber-bg); color: var(--amber); }}
  .chip.low {{ background: var(--ledger-dim); color: var(--ledger); }}

  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 820px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

  .note-box {{ background: var(--ledger-dim); border: 1px solid #BFDAD2; border-radius: 5px; padding: 18px 20px; font-size: 13px; line-height: 1.6; }}
  .note-box b {{ color: var(--ledger); }}

  footer.disclaimer {{ margin-top: 50px; padding-top: 18px; border-top: 1px solid var(--rule); font-size: 11.5px; color: var(--ink-soft); line-height: 1.6; }}

  @media print {{
    .topbar {{ display: none; }}
    body {{ padding: 0; }}
    h2 {{ break-before: page; }}
    h2:first-of-type {{ break-before: auto; }}
    table.data-table, .card, .cat-block {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-title">Bank Statement Analysis</div>
  <div class="topbar-actions">
    <button class="btn" onclick="exportCsv()">Export tables (CSV)</button>
    <button class="btn primary" onclick="window.print()">Print / Save as PDF</button>
  </div>
</div>

<div class="wrap">
  <header class="report-head">
    <div class="eyebrow">Audit &amp; ITR Working Paper</div>
    <h1>{source_file}</h1>
    <div class="meta-line">Bank: <b>{bank_name}</b> &middot; Period: <b>{period}</b> &middot; Generated: <b>{generated_at}</b></div>
  </header>

  <section>
    <h2>Executive Summary</h2>
    <div class="cards">{cards_html}</div>
  </section>

  <section>
    <h2>Monthly Cash Flow</h2>
    {month_chart_html}
    <div style="height:16px"></div>
    {monthly_table}
  </section>

  <section>
    <h2>Category-wise Break-up</h2>
    <div class="two-col">
      {credit_cat_html}
      {debit_cat_html}
    </div>
  </section>

  <section>
    <h2>Audit Flags &amp; High-Value Transactions</h2>
    <p class="muted" style="margin-bottom:12px;">Automated threshold checks referencing common ITR/audit trigger points (Sec 269ST, Sec 194N, SFT cash-deposit reporting range, large/round-figure entries). Verify each against source vouchers before relying on it.</p>
    {flags_table}
  </section>

  <section>
    <h2>Recurring Patterns</h2>
    <div class="two-col">
      {recurring_credit_html}
      {recurring_debit_html}
    </div>
  </section>

  <section>
    <h2>Top Counterparties</h2>
    <p class="muted" style="margin-bottom:12px;">Counterparty names are extracted heuristically from narration text (UPI/NEFT strings vary by bank) &mdash; useful for grouping, not a certified legal name.</p>
    <div class="two-col">
      {top_payers_html}
      {top_payees_html}
    </div>
  </section>

  <section>
    <h2>Largest Individual Transactions</h2>
    <div class="two-col">
      {top_credits_html}
      {top_debits_html}
    </div>
  </section>

  <section>
    <h2>Presumptive Taxation (Sec 44AD/44ADA) Helper</h2>
    <div class="note-box">
      Cash receipts are <b>{a["cash_credit_pct"]:.1f}%</b> of total credits and cash payments are <b>{a["cash_debit_pct"]:.1f}%</b> of total debits for this statement.
      Under Sec 44AD, the enhanced turnover limit of &#8377;3 crore (instead of &#8377;2 crore) applies only where cash receipts do not exceed 5% of turnover &mdash; the analogous professional limit under Sec 44ADA is &#8377;75 lakh instead of &#8377;50 lakh.
      Total credits shown above are the <b>gross inflow</b> for this account only; turnover for filing purposes should exclude non-business receipts such as loans received, refunds, and transfers from your own other accounts &mdash; please review the category and counterparty tables above and adjust manually.
    </div>
  </section>

  <footer class="disclaimer">
    This is an automated, preliminary analysis generated from the statement's own printed figures to speed up working-paper preparation.
    Category tagging, counterparty names, and recurring-pattern detection are heuristic (keyword/pattern based) and can misclassify unusual narrations &mdash;
    please verify all figures, flags, and classifications against source documents before relying on them for an audit conclusion or a return filing.
    Threshold references (Sec 269ST, Sec 194N, SFT reporting range, Sec 44AD/44ADA) reflect commonly applied limits at the time this tool was built and should be
    confirmed against the current law for the relevant assessment year.
  </footer>
</div>

<script>
function exportCsv() {{
  const tables = document.querySelectorAll('table.csv-table');
  let out = '';
  tables.forEach(t => {{
    const title = t.getAttribute('data-csv-title') || 'Table';
    out += '"' + title.replace(/"/g,'""') + '"\\n';
    const rows = t.querySelectorAll('tr');
    rows.forEach(r => {{
      const cells = Array.from(r.querySelectorAll('th,td')).map(c => {{
        let txt = c.innerText.replace(/\\s+/g,' ').trim();
        return '"' + txt.replace(/"/g,'""') + '"';
      }});
      out += cells.join(',') + '\\n';
    }});
    out += '\\n';
  }});
  const blob = new Blob([out], {{type: 'text/csv;charset=utf-8;'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'bank_statement_analysis.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}
</script>

</body>
</html>'''
