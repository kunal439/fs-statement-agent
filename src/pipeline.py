"""
Financial statement pipeline, 2024-Q4.

Prototype slice: P&L + Balance Sheet generator with a verifier that catches
its own errors before anything is issued. COA mapping and journal-entry
validation are the minimum upstream stages that slice needs.

    ingest -> map -> translate -> validate JEs -> post -> build -> verify -> publish

Every stage is deterministic. The only model call is an optional mapping
suggestion (src/llm.py, --llm), which can never change a number: suggestions
go to the review packet and only a reviewer's decision file changes balances.

Usage:
    python src/pipeline.py                                    # raw run
    python src/pipeline.py --review reviews/2024-Q4.json --output output/reviewed
    python src/trace.py 1110                                  # auditor trace of a line

Exit codes: 0 = CLEAN or REVIEW, 2 = BLOCKED, 1 = invalid input.
Design: docs/architecture.md   Defects: docs/defects.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ============================================================
# CONFIG
# ============================================================
PERIOD = "2024-Q4"
PERIOD_START, PERIOD_END = "2024-10-01", "2024-12-31"
FUNCTIONAL_CURRENCY = "USD"
MATERIALITY = Decimal("0.01")  # share of total assets in the translated TB, before adjustments
SUGGEST_MIN_SCORE = 0.60  # below this the string matcher offers no suggestion
NAME_DRIFT_MAX = 0.60  # exact code match but a name this different -> review
RETAINED_EARNINGS = "3200"
SUSPENSE_ACCOUNT = "9999"  # where an approved, immaterial TB difference is parked
FX_UNREALIZED_ACCOUNT = "7310"  # a manual JE hitting this + a translated account duplicates translation
FX_DIFFERENCE_OPTIONS = {
    "7310": "remeasurement of monetary items to P&L, right for a USD-functional entity",
    "3310": "translation of a foreign operation to the OCI reserve",
}
RATE_FALLBACK = {"period_end": "opening"}  # opening = last period's closing: a point-in-time rate
PL_SUBTOTALS = {"5000": "Gross profit", "6000": "Operating income", "7000": "Income before tax", "8000": "Net income"}

INPUT_FILES = {
    "coa": "chart_of_accounts.csv",
    "tb": "trial_balance.csv",
    "prior_tb": "prior_period_tb.csv",
    "fx": "fx_rates.csv",
    "adjustments": "manual_adjustments.json",
}
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
STATUS_TEXT = {
    "CLEAN": "every check passed or was signed off; statements are ready to issue.",
    "REVIEW": "statements are complete but not issuable until the items below are signed off.",
    "BLOCKED": "statements are drafts and must not be issued until the blocking items are resolved.",
}


class InputError(Exception):
    """Malformed input. Fail fast: never build statements from data we cannot parse."""


def q(x) -> Decimal:
    """Round to cents. `+ 0` turns -0.00 into 0.00."""
    return (Decimal(x) + 0).quantize(CENT, ROUND_HALF_UP)


def fmt(x) -> str:
    return f"{Decimal(x):,.2f}"


def money(value, where: str, allow_negative: bool = False) -> Decimal:
    try:
        d = Decimal(str(value).strip() or "0")
    except InvalidOperation:
        raise InputError(f"{where}: '{value}' is not a number") from None
    if not d.is_finite() or (d < 0 and not allow_negative):
        raise InputError(f"{where}: expected a non-negative amount, got '{value}'")
    return q(d)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ============================================================
# MODELS
# ============================================================
@dataclass(frozen=True)
class Account:
    code: str
    name: str
    type: str
    parent: str | None
    statement: str
    cf_category: str
    normal_balance: str


@dataclass(frozen=True)
class TBRow:
    file: str
    row: int
    code: str
    name: str
    currency: str
    debit: Decimal
    credit: Decimal

    @property
    def net(self) -> Decimal:  # debit-positive, in the row's currency
        return self.debit - self.credit


@dataclass
class Translated:
    row: TBRow
    usd: Decimal | None  # debit-positive USD; None when no rate exists
    fx: dict | None  # rate details for non-USD rows


# ============================================================
# INGEST — validate at the trust boundary
# ============================================================
def read_csv(path: Path, required: set) -> list:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise InputError(f"{path.name}: missing columns {sorted(missing)}")
        rows = []
        for i, r in enumerate(reader, start=2):
            if None in r:  # DictReader files surplus fields under the key None
                raise InputError(f"{path.name} row {i}: more fields than columns "
                                 f"(a thousands separator or an unquoted comma?)")
            rows.append((i, {k: (v or "").strip() for k, v in r.items()}))
        return rows


def load_coa(path: Path) -> dict:
    cols = {"account_code", "account_name", "account_type", "parent_code", "statement", "cf_category", "normal_balance"}
    coa = {}
    for i, r in read_csv(path, cols):
        if r["account_code"] in coa:
            raise InputError(f"{path.name} row {i}: duplicate account code {r['account_code']}")
        coa[r["account_code"]] = Account(
            r["account_code"], r["account_name"], r["account_type"], r["parent_code"] or None,
            r["statement"], r["cf_category"], r["normal_balance"],
        )
    for a in coa.values():
        if a.parent and a.parent not in coa:
            raise InputError(f"{path.name}: {a.code} has unknown parent {a.parent}")
    for a in coa.values():
        seen, parent = {a.code}, a.parent
        while parent:
            if parent in seen:
                raise InputError(f"{path.name}: parent cycle through {a.code}")
            seen.add(parent)
            parent = coa[parent].parent
    return coa


def load_tb(path: Path) -> list:
    rows = []
    for i, r in read_csv(path, {"account_code", "account_name", "currency", "debit", "credit"}):
        where = f"{path.name} row {i}"
        if len(r["currency"]) != 3 or not r["currency"].isalpha():
            raise InputError(f"{where}: bad currency '{r['currency']}'")
        rows.append(TBRow(path.name, i, r["account_code"], r["account_name"], r["currency"].upper(),
                          money(r["debit"], where), money(r["credit"], where)))
    return rows


def load_fx(path: Path) -> dict:
    rates = {}
    for i, r in read_csv(path, {"currency", "rate_type", "rate", "period"}):
        if r["period"] != PERIOD:
            continue
        key = (r["currency"].upper(), r["rate_type"])
        try:
            rate = Decimal(r["rate"])
        except InvalidOperation:
            raise InputError(f"{path.name} row {i}: rate '{r['rate']}' is not a number") from None
        if key in rates or not rate.is_finite() or rate <= 0:
            raise InputError(f"{path.name} row {i}: duplicate or non-positive rate for {key}")
        rates[key] = (rate, i)
    return rates


def load_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise InputError(f"{label}: invalid JSON ({e})") from None


def load_adjustments(path: Path):
    doc = load_json(path, path.name)
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise InputError(f"{path.name}: expected an object with an 'entries' list")
    jes = []
    for n, e in enumerate(doc["entries"], 1):
        where = f"{path.name} entry {n}"
        if not isinstance(e, dict) or not e.get("id") or not isinstance(e.get("lines"), list) or not e["lines"]:
            raise InputError(f"{where}: needs an id and at least one line")
        lines = []
        for k, ln in enumerate(e["lines"], 1):
            if not isinstance(ln, dict) or not str(ln.get("account", "")).strip():
                raise InputError(f"{where} line {k}: missing account")
            lines.append({
                "account": str(ln["account"]).strip(),
                # negative amounts are a JE-level rejection, not a malformed file
                "debit": money(ln.get("debit", 0), f"{where} line {k}", allow_negative=True),
                "credit": money(ln.get("credit", 0), f"{where} line {k}", allow_negative=True),
                "memo": str(ln.get("memo", "")),
            })
        jes.append({"id": str(e["id"]), "description": str(e.get("description", "")),
                    "date": str(e.get("date", "")), "source": str(e.get("source", "")), "lines": lines})
    header = {k: doc.get(k) for k in ("period", "functional_currency")}
    return header, jes


def load_review(path: Path, coa: dict, je_ids: set) -> dict:
    """Reviewer decisions: the only way a human changes a number. Validated strictly."""
    doc = load_json(path, f"review file {path.name}")
    label = f"review file {path.name}"
    if not isinstance(doc, dict) or not isinstance(doc.get("reviewer"), str) or not doc["reviewer"].strip():
        raise InputError(f"{label}: 'reviewer' is required")

    def section(key):
        s = doc.get(key, {})
        if not isinstance(s, dict) or not all(isinstance(v, dict) and str(v.get("reason", "")).strip() for v in s.values()):
            raise InputError(f"{label}: every entry in '{key}' needs a 'reason'")
        return s

    mappings, decisions, approvals = section("account_mappings"), section("je_decisions"), section("approved_entries")
    sign_offs = section("sign_offs")
    for code, m in mappings.items():
        target = coa.get(m["coa_code"]) if isinstance(m.get("coa_code"), str) else None
        if target is None or target.type == "Header":
            raise InputError(f"{label}: {code} is mapped to '{m.get('coa_code')}', which is not a postable COA account")
    for je_id, d in decisions.items():
        if je_id not in je_ids or d.get("action") != "reject":
            raise InputError(f"{label}: '{je_id}' must be a known entry with action 'reject'")
    for entry_id, a in approvals.items():
        if entry_id not in ("SYS-FX", "SYS-SUSPENSE"):
            raise InputError(f"{label}: unknown proposed entry '{entry_id}'")
        a["amount"] = money(a.get("amount"), f"{label} {entry_id}")
    return {"reviewer": doc["reviewer"].strip(), "reviewed_on": doc.get("reviewed_on"), "account_mappings": mappings,
            "je_decisions": decisions, "approved_entries": approvals, "sign_offs": sign_offs}


# ============================================================
# MAPPER
# ============================================================
def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def collect_accounts(tb: list, prior: list, jes: list) -> dict:
    """Every account code in any input, with a name, where it was seen, and context for the LLM."""
    seen = {}
    for r in tb + prior:
        s = seen.setdefault(r.code, {"name": r.name, "from_tb": True, "seen_in": [], "context": []})
        s["seen_in"].append(f"{r.file} row {r.row}")
        s["context"].append(f"{r.file} row {r.row}: '{r.name}', "
                            f"{'debit' if r.net >= 0 else 'credit'} balance {fmt(abs(r.net))} {r.currency}")
    for je in jes:
        for k, ln in enumerate(je["lines"], 1):
            s = seen.setdefault(ln["account"], {"name": ln["memo"] or je["description"], "from_tb": False,
                                                "seen_in": [], "context": []})
            s["seen_in"].append(f"{je['id']} line {k}")
            side = "debit" if ln["debit"] > 0 else "credit"
            s["context"].append(f"{je['id']} line {k}: {side} {fmt(ln['debit'] or ln['credit'])}, "
                                f"memo '{ln['memo']}', entry '{je['description']}'")
    return seen


def map_accounts(seen: dict, coa: dict, review: dict | None, llm=None) -> dict:
    """Exact code matches map automatically. Anything else gets a suggestion at most:
    only a reviewer mapping in the review file changes where a balance lands."""
    postable = {c: a for c, a in coa.items() if a.type != "Header"}
    overrides = review["account_mappings"] if review else {}
    out = {}
    for code in sorted(seen):
        info = seen[code]
        m = {"account": code, "name": info["name"], "seen_in": info["seen_in"], "status": "",
             "coa_code": None, "requires_human": False, "suggestion": None}
        if code in overrides:
            m.update(status="OVERRIDDEN", coa_code=overrides[code]["coa_code"],
                     approved_by=review["reviewer"], reason=overrides[code]["reason"])
        elif code in postable:
            drift = info["from_tb"] and similarity(info["name"], coa[code].name) < NAME_DRIFT_MAX
            m.update(status="EXACT_NAME_DRIFT" if drift else "EXACT", coa_code=code, requires_human=drift)
        else:
            ranked = sorted(((round(similarity(info["name"], a.name), 3), c) for c, a in postable.items()), reverse=True)
            m["alternatives"] = [{"coa_code": c, "coa_name": postable[c].name, "score": s} for s, c in ranked[:3]]
            if code in coa:
                m["note"] = f"{code} is a header in the COA and cannot hold a balance."
            score, best = ranked[0]
            if score >= SUGGEST_MIN_SCORE:
                m["suggestion"] = {"coa_code": best, "coa_name": postable[best].name, "confidence": score,
                                   "method": "name_similarity",
                                   "reasoning": f"Name '{info['name']}' is closest to '{postable[best].name}'."}
            if llm:
                answered, suggestion, note = llm(code, "\n".join(info["context"]), postable)
                if answered:  # the model's semantic answer, including "nothing fits", beats string distance
                    m["suggestion"] = suggestion
                m["llm_note"] = note
            m.update(status="SUGGESTED" if m["suggestion"] else "ORPHAN", requires_human=True)
        out[code] = m
    return out


# ============================================================
# FX TRANSLATION
# ============================================================
def resolve_rate(fx: dict, currency: str, wanted: str):
    for rate_type, status in ((wanted, "exact"), (RATE_FALLBACK.get(wanted), "fallback")):
        if rate_type and (currency, rate_type) in fx:
            rate, row = fx[(currency, rate_type)]
            return rate, rate_type, status, row
    return None, None, "missing", None


def translate(tb: list, fx: dict, account_map: dict, coa: dict) -> list:
    """BS rows at the period-end rate, P&L rows at the period-average rate."""
    out = []
    for r in tb:
        if r.currency == FUNCTIONAL_CURRENCY:
            out.append(Translated(r, r.net, None))
            continue
        acct = coa.get(account_map.get(r.code))
        wanted = "period_average" if acct and acct.statement == "PL" else "period_end"
        rate, rate_type, status, fx_row = resolve_rate(fx, r.currency, wanted)
        if rate is None:
            out.append(Translated(r, None, {"wanted": wanted, "status": "missing"}))
            continue
        usd = q(r.net * rate)
        values = [q(r.net * v) for (c, _), (v, _) in fx.items() if c == r.currency]
        out.append(Translated(r, usd, {
            "rate": rate, "rate_type": rate_type, "wanted": wanted, "status": status,
            "source": f"{INPUT_FILES['fx']} row {fx_row}", "difference": q(usd - r.net),
            "swing": q(max(values) - min(values)),
        }))
    return out


# ============================================================
# ADJUSTMENTS VALIDATOR — explains every rejection in plain English
# ============================================================
def validate_je(je: dict, account_map: dict, coa: dict, fx_accounts: set, mappings: dict, id_counts: Counter):
    lines, reject, hold = je["lines"], [], []
    debits, credits = q(sum(ln["debit"] for ln in lines)), q(sum(ln["credit"] for ln in lines))

    bad = [str(k) for k, ln in enumerate(lines, 1)
           if ln["debit"] < 0 or ln["credit"] < 0 or (ln["debit"] > 0) == (ln["credit"] > 0)]
    if bad:
        reject.append(f"Line {', '.join(bad)} must carry one positive amount, either a debit or a credit.")
    if debits != credits:
        reject.append(f"Debits total {fmt(debits)} but credits total {fmt(credits)}, so the entry is out of "
                      f"balance by {fmt(abs(debits - credits))}. An unbalanced entry can't be posted, and the "
                      f"pipeline won't guess which line is wrong: return it to the preparer.")
    if id_counts[je["id"]] > 1:
        reject.append(f"Entry ID {je['id']} appears {id_counts[je['id']]} times. Post each entry once.")
    try:
        in_period = PERIOD_START <= date.fromisoformat(je["date"]).isoformat() <= PERIOD_END
    except ValueError:
        reject.append(f"Date '{je['date']}' is not a valid YYYY-MM-DD date.")
    else:
        if not in_period:
            reject.append(f"Dated {je['date']}, outside {PERIOD} ({PERIOD_START} to {PERIOD_END}).")

    net = defaultdict(Decimal)
    for ln in lines:
        net[account_map.get(ln["account"], ln["account"])] += ln["debit"] - ln["credit"]
    if not bad and all(v == 0 for v in net.values()):
        msg = f"Every account in this entry nets to zero ({', '.join(sorted(net))}), so it balances but changes nothing."
        if any("intercompany" in coa[a].name.lower() for a in net if a in coa):
            msg += (" It looks like an intercompany settlement with the counter-leg missing"
                    " (cash, or the other entity's receivable).")
        reject.append(msg + " Repost it with the real counter-accounts.")

    for ln in lines:
        if ln["account"] in account_map:
            continue
        msg = (f"Account {ln['account']} is not in the chart of accounts. Add it to the COA or map it in the "
               f"review file; the entry is held until then.")
        s = mappings[ln["account"]]["suggestion"]
        if s:
            msg += f" Suggested: {s['coa_code']} {s['coa_name']} (confidence {s['confidence']})."
            if any(account_map.get(o["account"]) == s["coa_code"] for o in lines if o is not ln):
                msg += f" Careful: the entry already uses {s['coa_code']}, so that mapping would make it a no-op."
        hold.append(msg)

    touched = {account_map[ln["account"]] for ln in lines if ln["account"] in account_map}
    overlap = sorted(touched & fx_accounts)
    if FX_UNREALIZED_ACCOUNT in touched and overlap:
        names = ", ".join(f"{a} {coa[a].name}" for a in overlap)
        hold.append(f"This entry revalues {names} through {FX_UNREALIZED_ACCOUNT} {coa[FX_UNREALIZED_ACCOUNT].name}, "
                    f"but the pipeline already translates the foreign-currency rows of {names} at the period-end "
                    f"rate. Posting both would count the revaluation twice. Held for a reviewer to confirm and "
                    f"reject it in the review file.")

    return ("REJECTED" if reject else "QUARANTINED" if hold else "ACCEPTED"), reject + hold


def validate_adjustments(jes, account_map, coa, fx_accounts, mappings, review) -> list:
    ids = Counter(je["id"] for je in jes)
    decisions = review["je_decisions"] if review else {}
    out = []
    for je in jes:
        status, reasons = validate_je(je, account_map, coa, fx_accounts, mappings, ids)
        rec = {"id": je["id"], "description": je["description"], "date": je["date"], "source": je["source"],
               "status": status, "reasons": reasons, "lines": je["lines"]}
        if je["id"] in decisions:  # a reviewer may veto any entry; they can never force one in
            rec.update(status="REJECTED", decided_by=review["reviewer"], decision=decisions[je["id"]]["reason"])
        out.append(rec)
    return out


# ============================================================
# POSTING + PROPOSED SYSTEM ENTRIES
# ============================================================
def tb_component(t: Translated) -> dict:
    c = {"source": t.row.file, "row": t.row.row, "account": t.row.code, "currency": t.row.currency,
         "amount": t.row.net, "usd": t.usd}
    if t.fx:
        c.update(rate=t.fx["rate"], rate_type=t.fx["rate_type"], rate_status=t.fx["status"], rate_source=t.fx["source"])
    return c


def post(translated: list, accepted: list, account_map: dict) -> dict:
    """Ledger: account -> lineage components. Each component's `usd` is debit-positive.
    Unmapped TB accounts keep their own code and surface as suspense lines, never dropped."""
    ledger = defaultdict(list)
    for t in translated:
        if t.usd is not None:
            ledger[account_map.get(t.row.code, t.row.code)].append(tb_component(t))
    for je in accepted:
        for k, ln in enumerate(je["lines"], 1):
            ledger[account_map[ln["account"]]].append({
                "source": INPUT_FILES["adjustments"], "je": je["id"], "line": k, "account": ln["account"],
                "memo": ln["memo"], "usd": q(ln["debit"] - ln["credit"])})
    return ledger


def ledger_net(ledger: dict) -> Decimal:
    return q(sum(c["usd"] for comps in ledger.values() for c in comps))


def propose_entries(delta: Decimal, translated: list, face: dict, materiality: Decimal) -> list:
    """Diagnose an out-of-balance ledger and propose exact fixes. Never posted without approval."""
    if delta == 0:
        return []
    proposals = []
    uplift = q(sum(t.fx["difference"] for t in translated if t.fx and t.usd is not None))
    if uplift:
        options = "; ".join(f"{a} ({why})" for a, why in FX_DIFFERENCE_OPTIONS.items())
        proposals.append({
            "id": "SYS-FX", "side": "credit" if uplift > 0 else "debit", "amount": abs(uplift), "account": None,
            "memo": "Offsets the translation uplift on the foreign-currency TB rows.",
            "account_options": list(FX_DIFFERENCE_OPTIONS),
            "explanation": f"Translating foreign-currency rows at period rates changes their USD value by "
                           f"{fmt(uplift)}, and no other row offsets it: the source TB only balances at face value. "
                           f"Post the offset to one of: {options}. The amount assumes the ERP carried these rows "
                           f"at face value; confirm (docs/questions.md Q1).",
        })
    residual = q(delta - uplift)
    if residual and abs(residual) <= materiality:
        proposals.append({
            "id": "SYS-SUSPENSE", "side": "debit" if residual < 0 else "credit", "amount": abs(residual),
            "account": SUSPENSE_ACCOUNT, "account_options": [SUSPENSE_ACCOUNT],
            "memo": "Source TB difference parked in suspense pending investigation.",
            "explanation": f"The remaining {fmt(abs(residual))} is already in {INPUT_FILES['tb']} (debits "
                           f"{fmt(face['debit'])} vs credits {fmt(face['credit'])} at face value). It is below materiality "
                           f"({fmt(materiality)}), so it can be parked in {SUSPENSE_ACCOUNT} Suspense while the ERP "
                           f"export is investigated. It is never spread across accounts.",
        })
    return proposals


def approve_entries(proposals: list, review: dict | None) -> tuple:
    """Turn approved proposals into system entries. An approval binds to the exact amount:
    if the inputs change, the approval goes stale and the run blocks."""
    entries, issues = [], []
    by_id = {p["id"]: p for p in proposals}
    for pid, a in (review["approved_entries"] if review else {}).items():
        p = by_id.get(pid)
        if p is None:
            issues.append(f"{pid} is approved, but the pipeline no longer proposes it.")
        elif a["amount"] != p["amount"]:
            issues.append(f"{pid} was approved for {fmt(a['amount'])} but now computes as {fmt(p['amount'])}; "
                          f"the inputs changed, so it must be re-approved.")
        elif (p["account"] or a.get("account")) not in p["account_options"]:
            issues.append(f"{pid} needs an account from {p['account_options']}, got '{a.get('account')}'.")
        else:
            entries.append({"source": "system", "entry": pid, "line": 1, "account": p["account"] or a["account"],
                            "memo": p["memo"], "approved_by": review["reviewer"],
                            "reason": a["reason"], "usd": p["amount"] if p["side"] == "debit" else -p["amount"]})
    return entries, issues


# ============================================================
# STATEMENT BUILDER
# ============================================================
def presentation_sign(acct: Account | None) -> int:
    """+1 shows debit balances as positive (assets, suspense); -1 shows credit balances as
    positive (liabilities, equity, and P&L lines as their effect on income). Keyed on account
    type, never normal_balance, so contra accounts (1121, 1211, 1221, 3400, 4200) come out negative."""
    return 1 if acct is None or acct.type == "Asset" else -1


def statement_line(code: str, name: str, comps: list, acct: Account | None) -> dict:
    sign = presentation_sign(acct)
    return {"code": code, "name": name, "amount": q(sign * sum(c["usd"] for c in comps)),
            "presentation": "debit_positive" if sign > 0 else "credit_positive", "lineage": comps}


def root_of(code: str, coa: dict) -> Account:
    a = coa[code]
    while a.parent:
        a = coa[a.parent]
    return a


def group_of(code: str, coa: dict) -> Account:
    """Nearest Header ancestor (e.g. Current Assets); the root if there is none."""
    a = coa[code]
    while a.parent and coa[a.parent].type != "Header":
        a = coa[a.parent]
    return coa[a.parent] if a.parent else a


def label(acct: Account) -> str:
    return acct.name.removeprefix("Total ")


def grouped(lines: list, coa: dict) -> list:
    sections = {}
    for ln in lines:
        root, group = root_of(ln["code"], coa), group_of(ln["code"], coa)
        s = sections.setdefault(root.code, {"code": root.code, "name": label(root), "groups": {}})
        s["groups"].setdefault(group.code, {"code": group.code, "name": label(group), "lines": []})["lines"].append(ln)
    out = []
    for _, s in sorted(sections.items()):
        groups = [dict(g, total=q(sum(ln["amount"] for ln in g["lines"]))) for _, g in sorted(s["groups"].items())]
        out.append({"code": s["code"], "name": s["name"], "total": q(sum(g["total"] for g in groups)), "groups": groups})
    return out


def is_postable(code: str, coa: dict) -> bool:
    return code in coa and coa[code].type != "Header"


def build_pl(ledger: dict, coa: dict) -> dict:
    lines = [statement_line(c, coa[c].name, ledger[c], coa[c]) for c in sorted(ledger)
             if is_postable(c, coa) and coa[c].statement == "PL"]
    sections, running, ladder = grouped(lines, coa), ZERO, []
    for s in sections:
        running = q(running + s["total"])
        if s["code"] in PL_SUBTOTALS:
            ladder.append({"label": PL_SUBTOTALS[s["code"]], "amount": running})
    return {"sections": sections, "subtotals": ladder, "net_income": q(sum(ln["amount"] for ln in lines))}


def build_bs(ledger: dict, coa: dict, names: dict) -> dict:
    lines = [statement_line(c, coa[c].name, ledger[c], coa[c]) for c in sorted(ledger)
             if is_postable(c, coa) and coa[c].statement == "BS"]
    suspense = [statement_line(c, names.get(c, c), ledger[c], None) for c in sorted(ledger) if not is_postable(c, coa)]
    sections = grouped(lines, coa)
    if suspense:
        total = q(sum(ln["amount"] for ln in suspense))
        sections.append({"code": "SUSPENSE", "name": "Suspense - unmapped accounts (pending review)", "total": total,
                         "groups": [{"code": "SUSPENSE", "name": "Unmapped", "lines": suspense, "total": total}]})
    by_type = {t: q(sum(ln["amount"] for ln in lines if coa[ln["code"]].type == t)) for t in ("Asset", "Liability", "Equity")}
    totals = {"assets": by_type["Asset"], "suspense": q(sum(ln["amount"] for ln in suspense)),
              "liabilities": by_type["Liability"], "equity": by_type["Equity"]}
    totals["difference"] = q(totals["assets"] + totals["suspense"] - totals["liabilities"] - totals["equity"])
    return {"sections": sections, "totals": totals}


def all_lines(statement: dict) -> list:
    return [ln for s in statement["sections"] for g in s["groups"] for ln in g["lines"]]


# ============================================================
# VERIFIER — checks the statements before anything is issued
# ============================================================
def verify(c: dict) -> list:
    findings = []

    def check(fid, ok, severity, title, detail, action="", **evidence):
        findings.append({"id": fid, "status": "PASS" if ok else "FAIL", "severity": None if ok else severity,
                         "title": title, "detail": detail, "action": "" if ok else action, "evidence": evidence,
                         "fingerprint": hashlib.sha256(f"{fid}|{detail}".encode()).hexdigest()[:12]})

    coa, mat, bs, pl = c["coa"], c["materiality"], c["bs"], c["pl"]
    hdr = c["adj_header"]
    check("INPUT_HEADER", hdr == {"period": PERIOD, "functional_currency": FUNCTIONAL_CURRENCY}, "BLOCK",
          "Adjustments file matches the period and functional currency",
          f"{INPUT_FILES['adjustments']} declares {hdr['period']} / {hdr['functional_currency']}; "
          f"the run is {PERIOD} / {FUNCTIONAL_CURRENCY}.", "Use the adjustments file for this period.")

    missing = [t for t in c["translated"] if t.fx and t.usd is None]
    check("FX_RATES_AVAILABLE", not missing, "BLOCK", "Every foreign-currency row has a rate",
          "; ".join(f"{t.row.file} row {t.row.row}: no {t.fx['wanted']} rate for {t.row.currency}" for t in missing)
          or "All foreign-currency rows translated.",
          "Add the missing rate to fx_rates.csv; a row is never dropped or guessed.")

    est = [t for t in c["translated"] if t.fx and t.fx["status"] == "fallback"]
    swing = q(sum(t.fx["swing"] for t in est))
    check("FX_ESTIMATES", not est, "BLOCK" if swing > mat else "REVIEW", "No translation relies on an estimated rate",
          " ".join(f"{t.row.file} row {t.row.row} ({t.row.currency} {fmt(t.row.net)}) has no {t.fx['wanted']} rate, "
                   f"so it uses the {t.fx['rate_type']} rate {t.fx['rate']} (last period's closing rate, a point-in-time "
                   f"rate like the missing one). Across the available {t.row.currency} rates its USD value moves by "
                   f"{fmt(t.fx['swing'])}, against materiality of {fmt(mat)}." for t in est) or "All rates exact.",
          "Get the period-end rate from treasury and re-run, or sign off on the estimate.", swing=swing)

    delta, face, uplift = c["delta"], c["face"], c["uplift"]
    parts = [f"{fmt(face['delta'])} already in {INPUT_FILES['tb']} at face value",
             f"{fmt(uplift)} from translating foreign-currency rows with no offsetting entry"]
    dropped = q(sum(t.row.net for t in c["translated"] if t.usd is None))
    if dropped:
        parts.append(f"{fmt(-dropped)} face value of rows with no rate, held out until a rate exists")
    parts += [f"{fmt(e['usd'])} approved {e['entry']}" for e in c["system_entries"]]
    check("LEDGER_BALANCED", delta == 0, "BLOCK", "Ledger balances after translation and posting",
          f"Debits minus credits = {fmt(delta)}. Breakdown: {'; '.join(parts)}.",
          "Approve the proposed entries in a review file (template: reviews/2024-Q4.json), or fix the source "
          "export, then re-run." if c["proposals"] else "Fix the source trial balance and re-run.",
          delta=delta, face_delta=face["delta"], translation_uplift=uplift, proposals=c["proposals"])

    if c["review"]:
        check("REVIEW_FILE", not c["approval_issues"], "BLOCK", "Reviewer approvals still match the numbers",
              " ".join(c["approval_issues"]) or "All approvals current.", "Update the review file and re-run.")

    accepted = [v for v in c["validated"] if v["status"] == "ACCEPTED"]
    bad = [v["id"] for v in accepted if sum(ln["debit"] - ln["credit"] for ln in v["lines"]) != 0]
    check("JE_POSTED_BALANCED", not bad, "BLOCK", "Every posted adjustment balances",
          f"Posted but unbalanced: {bad}." if bad else f"{len(accepted)} posted, all balanced.")

    open_jes = [v for v in c["validated"] if v["status"] != "ACCEPTED" and "decided_by" not in v]
    decided = [v for v in c["validated"] if "decided_by" in v]
    material = [v["id"] for v in open_jes if sum(ln["debit"] for ln in v["lines"]) > mat]
    check("JE_EXCEPTIONS", not open_jes, "BLOCK" if material else "REVIEW",
          "Every adjustment is posted or has a reviewer decision",
          (", ".join(f"{v['id']} {v['status']}" for v in open_jes) + " (reasons in the adjustments table).")
          if open_jes else f"All resolved ({len(decided)} by reviewer decision).",
          "Correct and resubmit, or record a decision in the review file.",
          reviewer_decisions={v["id"]: v["decision"] for v in decided})

    tb_codes = {r.code for r in c["tb"]}
    suspense = {ln["code"]: ln["amount"] for s in bs["sections"] if s["code"] == "SUSPENSE"
                for g in s["groups"] for ln in g["lines"]}
    unmapped = [m for code, m in c["mappings"].items() if code in tb_codes and m["requires_human"]]

    def describe(m, balance=None):
        s = m["suggestion"]
        hint = f"suggested {s['coa_code']} {s['coa_name']} ({s['confidence']})" if s else "no account fits"
        bal = f", balance {fmt(balance)}" if balance is not None else ""
        return f"{m['account']} '{m['name']}' ({m['status']}{bal}; {hint})."

    check("TB_ACCOUNTS_MAPPED", not unmapped,
          "BLOCK" if any(abs(suspense.get(m["account"], 0)) > mat for m in unmapped) else "REVIEW",
          "Every trial-balance account maps to the COA",
          " ".join(describe(m, suspense.get(m["account"], ZERO)) for m in unmapped)
          + " Shown on the balance sheet as suspense, never dropped."
          if unmapped else "All mapped.",
          "Find out what was posted there and map it in the review file, or reclass it in the ERP.")

    pending = [m for code, m in c["mappings"].items()
               if code not in tb_codes and m["requires_human"] and any(".csv" in s for s in m["seen_in"])]
    check("PRIOR_ACCOUNTS_MAPPED", not pending, "REVIEW", "Every prior-period account maps to the COA",
          " ".join(describe(m) for m in pending) + " Needed for comparatives and the equity roll-forward."
          if pending else "All mapped.", "Approve or correct the mapping in the review file.")

    dupes = defaultdict(list)
    for r in c["tb"]:
        dupes[(r.code, r.currency)].append(r)
    dupes = {k: v for k, v in dupes.items() if len(v) > 1}
    check("TB_DUPLICATE_ROWS", not dupes, "REVIEW", "Each account/currency appears once in the trial balance",
          " ".join(f"{code} {rows[0].name} has {len(rows)} {cur} rows "
                   f"({', '.join(f'row {r.row}: {fmt(r.net)}' for r in rows)}), summed to "
                   f"{fmt(sum(r.net for r in rows))}. Right for split postings, wrong for a duplicated export line."
                   for (code, cur), rows in dupes.items()) or "No duplicates.",
          "Confirm against the ERP account detail.")

    lines = all_lines(bs) + all_lines(pl)
    used = Counter()
    for ln in lines:
        for comp in ln["lineage"]:
            used[(comp["source"], comp.get("row") or comp.get("je") or comp.get("entry"), comp.get("line"))] += 1
    expected = {(t.row.file, t.row.row, None) for t in c["translated"]}  # untranslated rows show up as gaps
    expected |= {(INPUT_FILES["adjustments"], v["id"], k) for v in accepted for k in range(1, len(v["lines"]) + 1)}
    expected |= {("system", e["entry"], 1) for e in c["system_entries"] + [c["closing"]]}
    def ref(key):
        source, item, line_no = key
        return f"system entry {item}" if source == "system" else \
            f"{source} row {item}" if line_no is None else f"{item} line {line_no}"

    gaps = [f"missing {ref(k)}" for k in sorted(expected - set(used), key=str)]
    gaps += [f"{'duplicated' if n > 1 else 'unknown'} {ref(k)}" for k, n in sorted(used.items(), key=str)
             if n > 1 or k not in expected]
    check("LINEAGE_COMPLETE", not gaps, "BLOCK", "Every source row and posted line lands in exactly one statement line",
          f"Problems: {'; '.join(gaps)}." if gaps else f"{len(expected)} source items, each used once.",
          "Statement builder defect: do not issue.")

    def shown_sign(code, kind):  # restated on purpose: a second implementation, independent of the builder's
        return -1 if kind == "PL" or (is_postable(code, coa) and coa[code].type != "Asset") else 1

    untied = [ln["code"] for st, kind in ((bs, "BS"), (pl, "PL")) for ln in all_lines(st)
              if ln["amount"] != q(shown_sign(ln["code"], kind) * sum(x["usd"] for x in ln["lineage"]))]
    check("LINEAGE_TIES", not untied, "BLOCK", "Every statement line equals the sum of its lineage",
          f"Lines that don't tie: {untied}." if untied else f"{len(lines)} lines tie to their sources.",
          "Statement builder defect: do not issue.")

    t = bs["totals"]
    explained = t["difference"] == delta
    check("BS_IDENTITY", t["difference"] == 0, "BLOCK", "Assets + suspense = liabilities + equity",
          f"Assets {fmt(t['assets'])} + suspense {fmt(t['suspense'])} vs liabilities {fmt(t['liabilities'])} + "
          f"equity {fmt(t['equity'])}: difference {fmt(t['difference'])}. "
          + ("" if t["difference"] == 0 else
             "Fully explained by the ledger imbalance (see LEDGER_BALANCED)." if explained else
             f"NOT explained by the ledger imbalance ({fmt(delta)}): the statement builder is wrong. Do not trust any number."),
          "Resolve LEDGER_BALANCED." if explained else "Statement builder defect: do not issue.",
          difference=t["difference"], explained_by_ledger=explained)

    ni = pl["net_income"]
    ladder_end = pl["subtotals"][-1]["amount"] if pl["subtotals"] else ni
    re_shown = next((ln["amount"] for ln in all_lines(bs) if ln["code"] == RETAINED_EARNINGS), ZERO)
    re_posted = sum(ln["debit"] - ln["credit"] for v in accepted for ln in v["lines"]
                    if c["account_map"].get(ln["account"]) == RETAINED_EARNINGS)
    re_posted += sum(e["usd"] for e in c["system_entries"] if e["account"] == RETAINED_EARNINGS)
    re_expected = q(-c["tb_net"].get(RETAINED_EARNINGS, ZERO) - re_posted + ni)
    check("NI_TO_EQUITY", ni == ladder_end and re_shown == re_expected, "BLOCK",
          "Net income ties from P&L to retained earnings",
          f"P&L net income {fmt(ni)}, subtotal ladder {fmt(ladder_end)}. Retained earnings on the balance sheet "
          f"{fmt(re_shown)}; recomputed from the raw TB, posted entries and net income: {fmt(re_expected)}.",
          "Statement builder defect: do not issue.")

    prior_net = defaultdict(Decimal)
    for r in c["prior"]:
        prior_net[r.code] += r.net
    prior_delta = q(sum(prior_net.values()))
    def maps_to_pl(m):  # effective mapping, else the suggestion: good enough to describe the prior TB
        target = coa.get(m["coa_code"] or (m["suggestion"] or {}).get("coa_code"))
        return target is not None and target.statement == "PL"

    pl_codes = {code for code, m in c["mappings"].items() if maps_to_pl(m)}
    present_pl = q(sum(v for code, v in prior_net.items() if code in pl_codes))
    check("PRIOR_TB_BALANCED", prior_delta == 0, "REVIEW", "Prior-period trial balance balances",
          f"{INPUT_FILES['prior_tb']} is out of balance by {fmt(prior_delta)} at face value and has P&L rows only "
          f"for {sorted(pl_codes & set(prior_net)) or 'no accounts'}: it looks like a pre-close TB with the P&L "
          f"detail missing, so it can't serve as comparatives as-is." if prior_delta else "Prior TB balances.",
          "Request the full prior-period TB (with P&L detail) from the ERP.", delta=prior_delta)

    opening, prior_re = -c["tb_net"].get(RETAINED_EARNINGS, ZERO), -prior_net.get(RETAINED_EARNINGS, ZERO)
    detail = f"Opening retained earnings in the current TB are {fmt(opening)}; the prior TB shows {fmt(prior_re)}."
    if opening != prior_re and prior_delta:
        implied_ni = q(prior_delta - present_pl)
        gap = q(opening - prior_re - implied_ni)
        detail += (f" If the prior TB is pre-close with P&L detail missing, prior net income is about "
                   f"{fmt(implied_ni)} and prior closing RE about {fmt(prior_re + implied_ni)}, so opening RE is "
                   f"{fmt(abs(gap))} {'lower' if gap < 0 else 'higher'} than it should be (dividends? a "
                   f"prior-period restatement?). "
                   f"Doesn't change this period's BS or P&L; blocks the Statement of Changes in Equity.")
    check("RE_ROLLFORWARD", opening == prior_re, "REVIEW", "Opening retained earnings roll from the prior period",
          detail, "Confirm dividends and any restatement with the controller (docs/questions.md Q3).")

    postable = [a for a in coa.values() if a.type != "Header"]
    parents = {a.parent for a in coa.values() if a.parent}
    notes = {
        "cash-flow category TBD": [a.code for a in postable if a.cf_category == "TBD"],
        "header with no children": [a.code for a in coa.values() if a.type == "Header" and a.code not in parents],
        "postable account used as a parent": sorted(a.code for a in postable if a.code in parents),
        "missing normal balance": [a.code for a in coa.values() if not a.normal_balance],
        "contra account (presented negative)": [a.code for a in postable if a.normal_balance and
                                                (a.normal_balance == "Debit") != (a.type in ("Asset", "Expense"))],
    }
    check("COA_STRUCTURE", not any(notes.values()), "INFO", "Chart of accounts structure",
          "; ".join(f"{k}: {', '.join(v)}" for k, v in notes.items() if v) + ". None changes the BS or P&L; the TBD "
          "categories must be decided before a cash-flow statement.", "Fix in the COA master.", **notes)
    return findings


def apply_sign_offs(findings: list, review: dict | None) -> list:
    """A reviewer signs off a REVIEW item by quoting its fingerprint. If the finding changes (new
    numbers), the fingerprint changes and the sign-off goes stale. BLOCK items can't be signed off."""
    notes = []
    for fid, s in (review["sign_offs"] if review else {}).items():
        f = next((f for f in findings if f["id"] == fid), None)
        if f is None or f["status"] != "FAIL" or f["severity"] != "REVIEW":
            notes.append(f"{fid}: not an open REVIEW item; sign-off ignored.")
        elif s.get("fingerprint") != f["fingerprint"]:
            notes.append(f"{fid}: signed off as {s.get('fingerprint')}, but the finding is now {f['fingerprint']}; "
                         f"its numbers changed, so it needs a fresh review.")
        else:
            f.update(signed_off_by=review["reviewer"], sign_off_reason=s["reason"])
    return notes


def is_open(f: dict) -> bool:
    return f["status"] == "FAIL" and f["severity"] in ("BLOCK", "REVIEW") and "signed_off_by" not in f


def run_status(findings: list) -> str:
    severities = {f["severity"] for f in findings if is_open(f)}
    return "BLOCKED" if "BLOCK" in severities else "REVIEW" if "REVIEW" in severities else "CLEAN"


# ============================================================
# PUBLISH
# ============================================================
def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def render_packet(r: dict) -> str:
    """The finance user's view: a short list of decisions, not pipeline internals."""
    t, status = r["bs"]["totals"], r["status"]
    out = [f"# Review packet: {PERIOD}", "",
           f"Run `{r['run_id']}` · status **{status}**: {STATUS_TEXT[status]}",
           f"Reviewer decisions applied: {r['review']['reviewer'] if r['review'] else 'none (raw run)'}", "",
           "## Headline numbers (USD)", "", "| | Amount |", "|---|---:|",
           f"| Total assets | {fmt(t['assets'])} |", f"| Suspense (unmapped) | {fmt(t['suspense'])} |",
           f"| Total liabilities | {fmt(t['liabilities'])} |", f"| Total equity | {fmt(t['equity'])} |",
           f"| **Balance sheet difference** | **{fmt(t['difference'])}** |"]
    out += [f"| {s['label']} | {fmt(s['amount'])} |" for s in r["pl"]["subtotals"]]
    actionable = sorted((f for f in r["findings"] if is_open(f)), key=lambda f: f["severity"] != "BLOCK")
    out += ["", f"## Decisions needed ({len(actionable)})", ""]
    out += [f"{n}. **[{f['severity']}] Not met: {f['title']}** ({f['id']}). {f['detail']}\n   *Action:* {f['action']}"
            + (f" Or sign off with key `{f['fingerprint']}`." if f["severity"] == "REVIEW" else "")
            for n, f in enumerate(actionable, 1)] or ["Nothing to decide."]
    signed = [f for f in r["findings"] if "signed_off_by" in f]
    if signed or r["sign_off_notes"]:
        out += ["", "## Signed off", ""] + [f"- **{f['id']}** by {f['signed_off_by']}: {f['sign_off_reason']}"
                                            for f in signed] + [f"- Not applied: {n}" for n in r["sign_off_notes"]]
    if r["proposals"]:
        out += ["", "## Proposed entries (computed by the verifier, posted only once approved)", "",
                "| ID | Side | Amount | Account | Why |", "|---|---|---:|---|---|"]
        out += [f"| {p['id']} | {p['side']} | {fmt(p['amount'])} | {p['account'] or ' or '.join(p['account_options'])} "
                f"| {p['explanation']} |" for p in r["proposals"]]
    out += ["", "## Manual adjustments", "", "| JE | Description | Status | Why |", "|---|---|---|---|"]
    for v in r["validated"]:
        why = " ".join(v["reasons"]) or "Balanced, accounts exist, in period."
        if "decided_by" in v:
            why = f"Reviewer ({v['decided_by']}): {v['decision']} {why}"
        out.append(f"| {v['id']} | {v['description']} | {v['status']} | {why} |")
    info = [f for f in r["findings"] if f["status"] == "FAIL" and f["severity"] == "INFO"]
    out += ["", "## For information", ""] + [f"- **{f['title']}**: {f['detail']}" for f in info]
    passed = [f["id"] for f in r["findings"] if f["status"] == "PASS"]
    out += [f"- Checks passed: {', '.join(passed)}.", "",
            "## How to respond", "",
            "Record decisions in a review file (example: `reviews/2024-Q4.json`) and re-run with "
            "`--review <file>`. Approvals bind to exact amounts and sign-offs to the finding's key, so a change in the "
            "numbers re-opens them. Trace any line with `python src/trace.py <code>`.", ""]
    return "\n".join(out)


# ============================================================
# MAIN PIPELINE
# ============================================================
def run(input_dir: Path, output_dir: Path, review_path: Path | None = None, llm=None) -> dict:
    files = {k: Path(input_dir) / v for k, v in INPUT_FILES.items()}
    coa = load_coa(files["coa"])
    tb, prior = load_tb(files["tb"]), load_tb(files["prior_tb"])
    fx = load_fx(files["fx"])
    adj_header, jes = load_adjustments(files["adjustments"])
    review = load_review(Path(review_path), coa, {je["id"] for je in jes}) if review_path else None

    code_files = sorted(Path(__file__).parent.glob("*.py"))
    manifest_inputs = {v: sha256(files[k]) for k, v in INPUT_FILES.items()}
    review_hash = sha256(Path(review_path)) if review_path else None
    fingerprint = {"inputs": manifest_inputs, "review": review_hash, "llm": getattr(llm, "model", None),
                   "code": {p.name: sha256(p) for p in code_files}}
    run_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:12]

    log_lines = []

    def log(msg=""):
        print(msg)
        log_lines.append(msg)

    log(f"FINANCIAL STATEMENT PIPELINE: {PERIOD} (run {run_id})")
    log(f"Loaded {len(coa)} COA accounts, {len(tb)} TB rows, {len(prior)} prior TB rows, {len(fx)} FX rates, "
        f"{len(jes)} JEs; review file: {Path(review_path).name if review_path else 'none'}; "
        f"LLM: {getattr(llm, 'model', 'off')}")

    # 1. Map
    mappings = map_accounts(collect_accounts(tb, prior, jes), coa, review, llm)
    account_map = {code: m["coa_code"] for code, m in mappings.items() if m["coa_code"]}
    log(f"\n[map] {sum(m['status'] == 'EXACT' for m in mappings.values())} exact; needs review:")
    for m in mappings.values():
        if m["status"] != "EXACT":
            s = m["suggestion"]
            log(f"  {m['account']} '{m['name']}' {m['status']}"
                + (f" -> {m['coa_code']}" if m["coa_code"] else "")
                + (f" (suggested {s['coa_code']} {s['coa_name']}, {s['confidence']}, {s['method']})" if s else ""))

    # 2. Translate
    translated = translate(tb, fx, account_map, coa)
    log("\n[fx]")
    for t in translated:
        if t.fx:
            log(f"  {t.row.file} row {t.row.row} {t.row.code} {t.row.currency} {fmt(t.row.net)}"
                + (f" x {t.fx['rate']} ({t.fx['rate_type']}, {t.fx['status']}) = {fmt(t.usd)}" if t.usd is not None
                   else " NO RATE"))

    # 3. Validate adjustments
    fx_accounts = {account_map[t.row.code] for t in translated if t.fx and t.row.code in account_map}
    validated = validate_adjustments(jes, account_map, coa, fx_accounts, mappings, review)
    accepted = [v for v in validated if v["status"] == "ACCEPTED"]
    log("\n[adjustments]")
    for v in validated:
        log(f"  {v['id']} {v['status']}" + (f" (reviewer: {v['decision']})" if "decided_by" in v else "")
            + "".join(f"\n      - {reason}" for reason in v["reasons"]))

    # 4. Post, diagnose, apply approved system entries
    ledger = post(translated, accepted, account_map)
    asset_usd = sum(t.usd for t in translated if t.usd is not None
                    and t.row.code in account_map and coa[account_map[t.row.code]].type == "Asset")
    materiality = q(MATERIALITY * asset_usd)
    face = {"debit": q(sum(r.debit for r in tb)), "credit": q(sum(r.credit for r in tb))}
    face["delta"] = q(face["debit"] - face["credit"])
    uplift = q(sum(t.fx["difference"] for t in translated if t.fx and t.usd is not None))
    proposals = propose_entries(ledger_net(ledger), translated, face, materiality)
    system_entries, approval_issues = approve_entries(proposals, review)
    for e in system_entries:
        e["account"] = account_map.get(e["account"], e["account"])
        ledger[e["account"]].append(e)
    delta = ledger_net(ledger)
    log(f"\n[post] {len(accepted)} JEs posted; ledger out of balance by {fmt(delta)}; materiality {fmt(materiality)}")
    for p in proposals:
        log(f"  proposed {p['id']}: {p['side']} {fmt(p['amount'])}"
            + (" (approved)" if any(e["entry"] == p["id"] for e in system_entries) else " (awaiting approval)"))

    # 5. Build
    pl = build_pl(ledger, coa)
    closing = {"source": "system", "entry": "SYS-CLOSE", "line": 1, "account": RETAINED_EARNINGS,
               "memo": f"Net income for {PERIOD} closed to retained earnings (profit_and_loss.json).",
               "usd": -pl["net_income"]}
    ledger[RETAINED_EARNINGS].append(closing)
    bs = build_bs(ledger, coa, {r.code: r.name for r in tb})
    t = bs["totals"]
    log(f"\n[build] assets {fmt(t['assets'])} | suspense {fmt(t['suspense'])} | liabilities {fmt(t['liabilities'])} "
        f"| equity {fmt(t['equity'])} | difference {fmt(t['difference'])} | net income {fmt(pl['net_income'])}")

    # 6. Verify
    tb_net = defaultdict(Decimal)
    for r in tb:
        tb_net[r.code] += r.net
    findings = verify({
        "coa": coa, "tb": tb, "prior": prior, "translated": translated, "mappings": mappings,
        "validated": validated, "review": review, "adj_header": adj_header, "materiality": materiality,
        "face": face, "uplift": uplift, "delta": delta, "proposals": proposals, "system_entries": system_entries,
        "approval_issues": approval_issues, "closing": closing, "bs": bs, "pl": pl, "tb_net": tb_net,
        "account_map": account_map,
    })
    sign_off_notes = apply_sign_offs(findings, review)
    status = run_status(findings)
    log("\n[verify]")
    for f in findings:
        log(f"  {f['status']:<4} {'[' + f['severity'] + ']' if f['severity'] else '':<8} {f['id']}: {f['title']}"
            + (f" (signed off by {f['signed_off_by']})" if "signed_off_by" in f else ""))
    for note in sign_off_notes:
        log(f"  sign-off not applied: {note}")

    # 7. Publish
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"period": PERIOD, "currency": FUNCTIONAL_CURRENCY, "status": status, "issuable": status == "CLEAN",
             "watermark": "DRAFT - BLOCKED, NOT FOR ISSUE" if status == "BLOCKED" else None, "run_id": run_id}
    result = {"run_id": run_id, "status": status, "bs": bs, "pl": pl, "findings": findings, "validated": validated,
              "mappings": mappings, "proposals": proposals, "review": review, "materiality": materiality,
              "sign_off_notes": sign_off_notes}
    outputs = {
        "balance_sheet.json": {"statement": "Balance Sheet", "as_of": PERIOD_END, **stamp, **bs},
        "profit_and_loss.json": {"statement": "Profit and Loss", "from": PERIOD_START, "to": PERIOD_END, **stamp, **pl},
        "mappings.json": mappings,
        "adjustments.json": {"period": PERIOD, "entries": validated},
        "validation_findings.json": {"status": status, "materiality": materiality, "findings": findings,
                                     "sign_off_notes": sign_off_notes},
    }
    for name, obj in outputs.items():
        write_json(output_dir / name, obj)
    (output_dir / "review_packet.md").write_text(render_packet(result), encoding="utf-8")
    exit_code = 2 if status == "BLOCKED" else 0
    log(f"\nSTATUS: {status}: {STATUS_TEXT[status]} (exit {exit_code})")
    (output_dir / "run_log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    write_json(output_dir / "run_manifest.json", {
        "run_id": run_id, "period": PERIOD, "status": status, "exit_code": exit_code,
        "inputs_sha256": manifest_inputs,
        "review_file": {"name": Path(review_path).name, "sha256": review_hash,
                        "reviewer": review["reviewer"]} if review else None,
        "code_sha256": fingerprint["code"], "llm_model": fingerprint["llm"],
        "config": {"functional_currency": FUNCTIONAL_CURRENCY, "materiality_pct_of_assets": MATERIALITY,
                   "materiality": materiality, "suggest_min_score": SUGGEST_MIN_SCORE},
        "outputs": sorted(list(outputs) + ["review_packet.md", "run_log.txt", "run_manifest.json"]),
    })
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"Build and verify the {PERIOD} Balance Sheet and P&L.")
    ap.add_argument("--input", type=Path, default=ROOT / "input", help="input directory")
    ap.add_argument("--output", type=Path, default=ROOT / "output", help="output directory")
    ap.add_argument("--review", type=Path, help="reviewer decisions file, e.g. reviews/2024-Q4.json")
    ap.add_argument("--llm", action="store_true",
                    help="ask Claude for mapping suggestions (needs ANTHROPIC_API_KEY); never changes a number")
    args = ap.parse_args(argv)

    llm = None
    if args.llm:
        try:
            import llm as llm_module
        except ImportError:
            print("--llm needs the anthropic package (pip install -r requirements.txt); continuing without it.",
                  file=sys.stderr)
        else:
            try:
                llm = llm_module.make_suggester()
            except RuntimeError as e:
                print(f"--llm: {e}; continuing with the deterministic mapper.", file=sys.stderr)
    try:
        result = run(args.input, args.output, args.review, llm)
    except (InputError, OSError) as e:
        print(f"INPUT ERROR: {e}", file=sys.stderr)
        return 1
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
