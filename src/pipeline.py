"""
Financial Statement Pipeline — 2024-Q4
======================================

Agentic pipeline that ingests a trial balance, translates non-functional
currencies to USD, maps accounts to a chart of accounts with confidence
scoring, validates manual adjustments, and produces a Balance Sheet plus
a P&L with self-verification.

Design reference: docs/architecture.md
Defect reference: docs/defects.md

Run:
    python src/pipeline.py

Outputs:
    output/mappings.json
    output/rejected_adjustments.json
    output/quarantined_adjustments.json
    output/validation_findings.json
    output/balance_sheet.json
    output/profit_and_loss.json
    output/lineage.json
    output/run_log.txt
"""

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional


# ============================================================
# CONFIG
# ============================================================
PERIOD = "2024-Q4"
FUNCTIONAL_CURRENCY = "USD"
MATERIALITY_THRESHOLD = 0.01
CONFIDENCE_AUTO_MAP = 0.85
CONFIDENCE_SUGGEST = 0.50

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INPUT_DIR = os.path.join(ROOT, "input")
OUTPUT_DIR = os.path.join(ROOT, "output")


# ============================================================
# MODELS
# ============================================================
@dataclass
class COAAccount:
    code: str
    name: str
    type: str
    parent: Optional[str]
    statement: str
    cf_category: str
    normal_balance: str


@dataclass
class TBRow:
    account_code: str
    account_name: str
    currency: str
    debit: float
    credit: float
    source_row: int


@dataclass
class Lineage:
    cell_id: str
    value: float
    components: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# ============================================================
# INGEST
# ============================================================
def load_coa(path):
    coa = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            coa[row["account_code"]] = COAAccount(
                code=row["account_code"],
                name=row["account_name"],
                type=row["account_type"],
                parent=row["parent_code"] or None,
                statement=row["statement"],
                cf_category=row["cf_category"],
                normal_balance=row["normal_balance"],
            )
    return coa


def load_tb(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            rows.append(
                TBRow(
                    account_code=row["account_code"],
                    account_name=row["account_name"],
                    currency=row["currency"],
                    debit=float(row["debit"]),
                    credit=float(row["credit"]),
                    source_row=i,
                )
            )
    return rows


def load_fx(path):
    rates = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["currency"], row["rate_type"], row["period"])
            rates[key] = float(row["rate"])
    return rates


def load_adjustments(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# FX TRANSLATION
# ============================================================
def resolve_rate(fx_rates, currency, rate_type, period):
    key = (currency, rate_type, period)
    if key in fx_rates:
        return fx_rates[key], "exact", None

    if rate_type == "period_end":
        alt = (currency, "opening", period)
        if alt in fx_rates:
            return (
                fx_rates[alt],
                "fallback_opening",
                f"No period_end for {currency}; used opening rate",
            )

    for (c, rt, p), v in fx_rates.items():
        if c == currency:
            return v, "fallback_any", f"No {rate_type} for {currency}; used {rt}"

    return None, "missing", f"No rate found for {currency} {rate_type}"


def translate_tb(tb_rows, fx_rates):
    translated = []
    lineage = []
    for row in tb_rows:
        if row.currency == FUNCTIONAL_CURRENCY:
            translated.append(row)
            continue

        rate, status, note = resolve_rate(
            fx_rates, row.currency, "period_end", PERIOD
        )
        if rate is None:
            lineage.append(
                Lineage(
                    cell_id=f"FX-{row.account_code}-{row.currency}",
                    value=0,
                    notes=[f"FX GAP: {note} — row excluded, flagged for review"],
                )
            )
            continue

        translated.append(
            TBRow(
                account_code=row.account_code,
                account_name=row.account_name,
                currency=FUNCTIONAL_CURRENCY,
                debit=row.debit * rate,
                credit=row.credit * rate,
                source_row=row.source_row,
            )
        )
        lineage.append(
            Lineage(
                cell_id=f"FX-{row.account_code}-{row.currency}",
                value=row.debit * rate,
                components=[f"TB row {row.source_row}: {row.currency} {row.debit}"],
                notes=[f"Rate {rate} ({status}): {note or 'OK'}"],
            )
        )
    return translated, lineage


# ============================================================
# MAPPER AGENT
# ============================================================
def name_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def map_account(tb_code, tb_name, coa):
    if tb_code in coa:
        return {
            "account_code": tb_code,
            "suggested_coa_code": tb_code,
            "confidence": 1.0,
            "reasoning": "Exact code match in COA.",
            "alternatives": [],
            "requires_human": False,
        }

    candidates = []
    for code, acc in coa.items():
        if acc.type == "Header":
            continue
        score = name_similarity(tb_name, acc.name)
        if score > 0.3:
            candidates.append((code, acc.name, score))
    candidates.sort(key=lambda x: -x[2])

    if not candidates:
        return {
            "account_code": tb_code,
            "suggested_coa_code": None,
            "confidence": 0.0,
            "reasoning": f"No COA account semantically similar to '{tb_name}'. "
            f"This account is orphaned.",
            "alternatives": [],
            "requires_human": True,
        }

    top = candidates[0]
    return {
        "account_code": tb_code,
        "suggested_coa_code": top[0],
        "confidence": round(top[2], 3),
        "reasoning": f"'{tb_name}' best matches COA '{top[1]}' (code {top[0]}) "
        f"with name similarity {top[2]:.2f}.",
        "alternatives": [
            {"code": c, "name": n, "score": round(s, 3)}
            for c, n, s in candidates[1:4]
        ],
        "requires_human": top[2] < CONFIDENCE_AUTO_MAP,
    }


# ============================================================
# POSTER AGENT
# ============================================================
def validate_adjustment(je, coa):
    reasons = []
    status = "ACCEPTED"

    total_debit = sum(l.get("debit", 0) for l in je["lines"])
    total_credit = sum(l.get("credit", 0) for l in je["lines"])
    if abs(total_debit - total_credit) > 0.01:
        status = "REJECTED"
        reasons.append(
            f"Debits ({total_debit:,.2f}) != Credits ({total_credit:,.2f}). "
            f"Difference: {abs(total_debit - total_credit):,.2f}."
        )

    for line in je["lines"]:
        if line["account"] not in coa:
            status = "QUARANTINED"
            reasons.append(
                f"Account {line['account']} not in COA. Cannot post until mapped."
            )

    debited = {l["account"] for l in je["lines"] if l.get("debit", 0) > 0}
    credited = {l["account"] for l in je["lines"] if l.get("credit", 0) > 0}
    if debited == credited and len(debited) == 1:
        status = "REJECTED"
        reasons.append(
            f"Circular entry: account {list(debited)[0]} is both debited and "
            f"credited for the same amount. Net economic effect is zero."
        )

    for line in je["lines"]:
        if line["account"] not in coa:
            continue
        acc = coa[line["account"]]
        if acc.normal_balance == "Debit" and line.get("credit", 0) > 0:
            reasons.append(
                f"Note: {line['account']} ({acc.name}) has Debit normal balance "
                f"but is credited. Verify this is intentional."
            )
        if acc.normal_balance == "Credit" and line.get("debit", 0) > 0:
            reasons.append(
                f"Note: {line['account']} ({acc.name}) has Credit normal balance "
                f"but is debited. Verify this is intentional."
            )

    return status, reasons


def process_adjustments(adjustments, coa):
    accepted, rejected, quarantined = [], [], []
    for je in adjustments["entries"]:
        status, reasons = validate_adjustment(je, coa)
        record = {"je": je, "status": status, "reasons": reasons}
        if status == "ACCEPTED":
            accepted.append(record)
        elif status == "REJECTED":
            rejected.append(record)
        else:
            quarantined.append(record)
    return accepted, rejected, quarantined


# ============================================================
# POST ADJUSTMENTS
# ============================================================
def post_adjustments(tb_rows, accepted_jes):
    balances = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
    for row in tb_rows:
        balances[row.account_code]["debit"] += row.debit
        balances[row.account_code]["credit"] += row.credit

    for record in accepted_jes:
        for line in record["je"]["lines"]:
            balances[line["account"]]["debit"] += line.get("debit", 0)
            balances[line["account"]]["credit"] += line.get("credit", 0)

    return balances


# ============================================================
# STATEMENT BUILDERS
# ============================================================
def signed_balance(bal, acc):
    """
    Return the balance of an account in its natural sign convention.

    Credit-normal accounts (liabilities, equity, revenue): value is
    credit - debit.
    Debit-normal accounts (assets, expenses, contra-equity): value is
    debit - credit, but contra-accounts (Treasury Stock, Accumulated
    Depreciation, Allowance for Doubtful Accounts) are returned as
    NEGATIVE values because they reduce the parent category.
    """
    raw = bal["debit"] - bal["credit"]

    if acc.normal_balance == "Credit":
        return -raw  # positive when credit balance

    # Debit-normal. Check for contra-accounts by name / code convention.
    contra_codes = {"1121", "1211", "1221", "3400"}
    if acc.code in contra_codes:
        return -raw  # contra: a debit balance in this account reduces the parent

    return raw


def build_balance_sheet(balances, coa):
    bs = {"Assets": [], "Liabilities": [], "Equity": []}
    total_assets = 0.0
    total_liabilities = 0.0
    total_equity = 0.0

    for code, bal in balances.items():
        if code not in coa:
            continue
        acc = coa[code]
        if acc.statement != "BS" or acc.type == "Header":
            continue

        value = signed_balance(bal, acc)

        if acc.type == "Asset":
            bs["Assets"].append({"code": code, "name": acc.name, "value": value})
            total_assets += value
        elif acc.type == "Liability":
            bs["Liabilities"].append({"code": code, "name": acc.name, "value": value})
            total_liabilities += value
        elif acc.type == "Equity":
            bs["Equity"].append({"code": code, "name": acc.name, "value": value})
            total_equity += value

    return bs, total_assets, total_liabilities, total_equity


def build_pl(balances, coa):
    revenue = 0.0
    expenses = 0.0
    lines = []

    for code, bal in balances.items():
        if code not in coa:
            continue
        acc = coa[code]
        if acc.statement != "PL" or acc.type == "Header":
            continue

        if acc.type == "Revenue":
            value = bal["credit"] - bal["debit"]
            revenue += value
            lines.append({"code": code, "name": acc.name, "type": "Revenue", "value": value})
        elif acc.type == "Expense":
            value = bal["debit"] - bal["credit"]
            expenses += value
            lines.append({"code": code, "name": acc.name, "type": "Expense", "value": value})

    net_income = revenue - expenses
    return lines, revenue, expenses, net_income


# ============================================================
# AUDITOR
# ============================================================
def verify_statements(ta, tl, te, rev, exp, ni, balances, coa):
    findings = []

    lhs, rhs = round(ta, 2), round(tl + te, 2)
    delta = round(lhs - rhs, 2)
    if abs(delta) > 1.0:
        findings.append({
            "check": "Balance Sheet Identity",
            "status": "FAIL",
            "expected": lhs,
            "actual": rhs,
            "delta": delta,
            "severity": "HIGH",
            "suggested_action": (
                "Assets != Liabilities + Equity. This residual equals the "
                "unresolved TB imbalance after excluding orphan accounts and "
                "FX gaps. Investigate the row-level exceptions."
            ),
        })
    else:
        findings.append({
            "check": "Balance Sheet Identity",
            "status": "PASS",
            "value": lhs,
        })

    if abs(ni - (rev - exp)) > 0.01:
        findings.append({
            "check": "P&L Identity",
            "status": "FAIL",
            "severity": "HIGH",
        })
    else:
        findings.append({
            "check": "P&L Identity",
            "status": "PASS",
            "value": ni,
        })

    total_debit = sum(b["debit"] for b in balances.values())
    total_credit = sum(b["credit"] for b in balances.values())
    tb_delta = round(total_debit - total_credit, 2)
    if abs(tb_delta) > 1.0:
        findings.append({
            "check": "TB Debits = Credits",
            "status": "FAIL",
            "expected": round(total_debit, 2),
            "actual": round(total_credit, 2),
            "delta": tb_delta,
            "severity": "HIGH",
            "suggested_action": (
                "Trial balance out of balance after adjustments. Check rejected "
                "JEs, orphan accounts, and FX translation gaps."
            ),
        })
    else:
        findings.append({
            "check": "TB Debits = Credits",
            "status": "PASS",
            "value": total_debit,
        })

    return findings


# ============================================================
# MAIN PIPELINE
# ============================================================
def run_pipeline():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_lines = []

    def log(msg=""):
        print(msg)
        log_lines.append(msg)

    log("=" * 70)
    log(f"FINANCIAL STATEMENT PIPELINE — {PERIOD}")
    log("=" * 70)

    coa = load_coa(os.path.join(INPUT_DIR, "chart_of_accounts.csv"))
    tb_rows = load_tb(os.path.join(INPUT_DIR, "trial_balance.csv"))
    fx_rates = load_fx(os.path.join(INPUT_DIR, "fx_rates.csv"))
    adjustments = load_adjustments(os.path.join(INPUT_DIR, "manual_adjustments.json"))
    log(
        f"\nLoaded: {len(coa)} COA accounts, {len(tb_rows)} TB rows, "
        f"{len(fx_rates)} FX rates, {len(adjustments['entries'])} JEs"
    )

    # STEP 1 — COA Mapping
    log("\n--- STEP 1: COA Mapping ---")
    mappings = {}
    unmapped = []
    for row in tb_rows:
        if row.account_code in mappings:
            continue
        m = map_account(row.account_code, row.account_name, coa)
        mappings[row.account_code] = m
        if m["suggested_coa_code"] is None:
            unmapped.append(m)
            log(f"  ORPHAN: {row.account_code} '{row.account_name}' — no COA match")
        elif m["requires_human"]:
            log(
                f"  REVIEW: {row.account_code} -> {m['suggested_coa_code']} "
                f"(conf {m['confidence']})"
            )
        else:
            log(
                f"  MAPPED: {row.account_code} -> {m['suggested_coa_code']} "
                f"(conf {m['confidence']})"
            )

    # STEP 2 — FX Translation
    log("\n--- STEP 2: FX Translation ---")
    translated, fx_lineage = translate_tb(tb_rows, fx_rates)
    for lin in fx_lineage:
        log(f"  {lin.cell_id}: {' | '.join(lin.notes)}")

    # STEP 3 — Adjustment Validation
    log("\n--- STEP 3: Adjustment Validation ---")
    accepted, rejected, quarantined = process_adjustments(adjustments, coa)
    log(f"  Accepted: {len(accepted)}")
    log(f"  Rejected: {len(rejected)}")
    log(f"  Quarantined: {len(quarantined)}")

    for r in rejected:
        log(f"\n  REJECTED {r['je']['id']}: {r['je']['description']}")
        for reason in r["reasons"]:
            log(f"    -> {reason}")
    for r in quarantined:
        log(f"\n  QUARANTINED {r['je']['id']}: {r['je']['description']}")
        for reason in r["reasons"]:
            log(f"    -> {reason}")

    # STEP 4 — Post Adjustments
    log("\n--- STEP 4: Posting Adjustments ---")
    balances = post_adjustments(translated, accepted)

    # STEP 5 — Build Statements
    log("\n--- STEP 5: Building Statements ---")

    # Compute P&L first
    pl_lines, rev, exp, ni = build_pl(balances, coa)

    # Closing entry: zero out PL accounts, move net into Retained Earnings
    for code in list(balances.keys()):
        if code not in coa:
            continue
        acc = coa[code]
        if acc.statement == "PL" and acc.type != "Header":
            balances[code]["debit"] = 0.0
            balances[code]["credit"] = 0.0

    balances["3200"]["credit"] += ni

    # Build balance sheet from closed books
    bs, ta, tl, te = build_balance_sheet(balances, coa)

    log("\n  Balance Sheet:")
    log(f"    Total Assets:      {ta:>15,.2f}")
    log(f"    Total Liabilities: {tl:>15,.2f}")
    log(f"    Total Equity:      {te:>15,.2f}")
    log(f"    A - (L+E):         {ta - tl - te:>15,.2f}")

    log("\n  P&L:")
    log(f"    Revenue:     {rev:>15,.2f}")
    log(f"    Expenses:    {exp:>15,.2f}")
    log(f"    Net Income:  {ni:>15,.2f}")

    # STEP 6 — Verify
    log("\n--- STEP 6: Verification ---")
    findings = verify_statements(ta, tl, te, rev, exp, ni, balances, coa)
    for f in findings:
        icon = "[OK]" if f["status"] == "PASS" else "[!!]"
        log(f"  {icon} {f['check']}: {f['status']}")
        if f["status"] == "FAIL":
            log(f"       Delta: {f.get('delta', 'N/A')}")
            log(f"       Action: {f.get('suggested_action', '')}")

    # Write outputs
    with open(os.path.join(OUTPUT_DIR, "mappings.json"), "w", encoding="utf-8") as f:
        json.dump(mappings, f, indent=2, default=str)

    with open(os.path.join(OUTPUT_DIR, "rejected_adjustments.json"), "w", encoding="utf-8") as f:
        json.dump(rejected, f, indent=2, default=str)

    with open(os.path.join(OUTPUT_DIR, "quarantined_adjustments.json"), "w", encoding="utf-8") as f:
        json.dump(quarantined, f, indent=2, default=str)

    with open(os.path.join(OUTPUT_DIR, "validation_findings.json"), "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2, default=str)

    with open(os.path.join(OUTPUT_DIR, "balance_sheet.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "period": PERIOD,
                "currency": FUNCTIONAL_CURRENCY,
                "assets": bs["Assets"],
                "liabilities": bs["Liabilities"],
                "equity": bs["Equity"],
                "total_assets": ta,
                "total_liabilities": tl,
                "total_equity": te,
            },
            f,
            indent=2,
            default=str,
        )

    with open(os.path.join(OUTPUT_DIR, "profit_and_loss.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "period": PERIOD,
                "currency": FUNCTIONAL_CURRENCY,
                "lines": pl_lines,
                "total_revenue": rev,
                "total_expenses": exp,
                "net_income": ni,
            },
            f,
            indent=2,
            default=str,
        )

    with open(os.path.join(OUTPUT_DIR, "lineage.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "fx_translations": [
                    {"cell_id": l.cell_id, "value": l.value,
                     "components": l.components, "notes": l.notes}
                    for l in fx_lineage
                ]
            },
            f,
            indent=2,
        )

    with open(os.path.join(OUTPUT_DIR, "run_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    log(f"\nOutputs written to {OUTPUT_DIR}/")
    return {
        "totals": {"assets": ta, "liabilities": tl, "equity": te,
                   "revenue": rev, "expenses": exp, "net_income": ni},
        "findings": findings,
        "rejected": rejected,
        "quarantined": quarantined,
        "unmapped": unmapped,
    }


if __name__ == "__main__":
    run_pipeline()