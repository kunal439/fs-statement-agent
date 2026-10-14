# Defect Inventory: Input Data (2024-Q4)

**26 defects** across the five input files: the ones the brief lists, plus
the ones it says it deliberately left out. Every defect that can change a
number surfaces in `output/validation_findings.json`, in
`output/review_packet.md`, or as a JE status. None is silently absorbed. The
informational ones (TB-7, FX-2, FX-3, X-4, X-5) are documented here and in
`docs/questions.md`.

**Severity:**
- **Critical:** blocks issue.
- **High:** material, or changes a number.
- **Medium:** needs review.
- **Low:** cosmetic or informational.

## A. Trial balance (`trial_balance.csv`)

| ID | Defect | Evidence | Sev. | Surfaced as |
|----|--------|----------|------|-------------|
| TB-1 | Source TB doesn't balance | Debits 71,072,200.00 vs credits 71,077,000.00 at face value: **−4,800.00** (the seeded "unbalanced after FX rounding") | Critical | `LEDGER_BALANCED` (BLOCK); proposed `SYS-SUSPENSE` |
| TB-2 | Translation creates an imbalance with no offset | TB balances only at face value; translating EUR/GBP cash adds **181,900.30** (EUR 78,413.00, GBP 103,487.30) | Critical | `LEDGER_BALANCED`; proposed `SYS-FX`; question Q1 |
| TB-3 | Duplicate account rows | `6310` on rows 48 (245,000.00) and 49 (38,500.00) | Medium | `TB_DUPLICATE_ROWS` (summed, flagged) |
| TB-4 | Orphan account not in COA | `9999 Suspense - Unmapped`, 12,400.00 debit | High | `TB_ACCOUNTS_MAPPED`; BS suspense line |
| TB-5 | Suspense account in use at all | 9999 implies an unposted or misposted transaction upstream | High | Same; investigation item (Q8) |
| TB-6 | Multi-currency rows for one account | `1110` has USD, EUR and GBP rows (2, 3, 4) | Medium | Translated row by row; lineage shows each rate |
| TB-7 | Fewer accounts than described | The brief says ~80 accounts; the file has 62 rows / 59 codes | Low | Noted |

## B. Chart of accounts (`chart_of_accounts.csv`)

| ID | Defect | Evidence | Sev. | Surfaced as |
|----|--------|----------|------|-------------|
| COA-1 | Ambiguous cash-flow category | `1150 Other Current Assets` = `TBD` | Medium | `COA_STRUCTURE`; Q11 |
| COA-2 | Ambiguous cash-flow category | `2170 Intercompany Payable` = `TBD` | Medium | `COA_STRUCTURE`; Q11 |
| COA-3 | Header with no children | `1290 Other Assets` | Low | `COA_STRUCTURE` |
| COA-4 | Missing normal balance | `7000 Non-Operating Items` | Low | `COA_STRUCTURE` |
| COA-5 | Postable accounts used as parents | `3300 Accumulated OCI` (parent of 3310), `8000 Income Tax Expense` (parent of 8100, 8200) | Low | `COA_STRUCTURE`; Q12 |
| COA-6 | Contra accounts typed by section, not by sign | `1121`, `1211`, `1221` are `Asset` with **Credit** normal balance; `3400` Equity/Debit; `4200` Revenue/Debit | High | Presented negative (sign keyed on type); `COA_STRUCTURE`. This trap caused the original sign bug |

## C. Manual adjustments (`manual_adjustments.json`)

| ID | Defect | Evidence | Sev. | Surfaced as |
|----|--------|----------|------|-------------|
| JE-1 | Debits ≠ credits | `JE-002`: 28,500.00 vs 25,000.00 (Δ 3,500.00) | High | **REJECTED**, with plain-English reason |
| JE-2 | Account not in COA | `JE-005` debits `6315` ("Conf travel") | High | **QUARANTINED** until mapped |
| JE-3 | Circular intercompany entry | `JE-008` debits and credits `2170` for 320,000.00; every account nets to zero | High | **REJECTED** |
| JE-4 | Entry duplicates a system process | `JE-003` revalues EUR cash via 7310, which the pipeline already translates at the period-end rate: double count | High | **QUARANTINED** for a reviewer |
| JE-5 | Description contradicts the lines | `JE-005` says "reclass … *from* old code", but moves 18,500 *out of* 6310 *into* the unknown 6315. Mapping 6315 → 6310 would make it a no-op | Medium | Warning in the JE reason when that mapping is suggested |

## D. FX rates (`fx_rates.csv`)

| ID | Defect | Evidence | Sev. | Surfaced as |
|----|--------|----------|------|-------------|
| FX-1 | Missing GBP period-end rate | Only GBP average (1.264) and opening (1.251) | High | Falls back to opening (point-in-time); swing **5,359.90** vs materiality 365,162.00 → `FX_ESTIMATES` (REVIEW) |
| FX-2 | No USD opening row | USD is functional; rate 1.0 implicit | Low | No impact |
| FX-3 | JE-003's amount contradicts the TB | 11,200 / 825,400 implies a EUR carrying rate of ~1.0814; the TB only balances if EUR is carried at face value | Medium | Q1 |

## E. Prior period and cross-file (`prior_period_tb.csv`)

| ID | Defect | Evidence | Sev. | Surfaced as |
|----|--------|----------|------|-------------|
| X-1 | Renamed account across periods | Prior `6905 Sundry Operating Expenses` → current `6900 Other Operating Expenses` | Medium | `PRIOR_ACCOUNTS_MAPPED`; suggested at 0.816, approved in the review file |
| X-2 | Prior TB doesn't balance | Out by **2,832,800.00** at face value; P&L detail missing except 6905 | High | `PRIOR_TB_BALANCED` |
| X-3 | Retained earnings don't roll forward | Current opening RE 7,240,000.00 vs prior 5,180,000.00. Even inferring prior net income (~2,700,800.00), opening RE is **640,800.00** short | High | `RE_ROLLFORWARD`; blocks the SOCIE; Q3 |
| X-4 | FX reserve moved with no OCI source | `3310` 95,000 → 180,000 | Medium | SOCIE design note (architecture §9) |
| X-5 | New account vs prior | `9999` appears only in the current period | Low | Expected: new suspense |

## Handling strategy

| Outcome | Applies to | Rule |
|---|---|---|
| **Auto-handle** | Exact mappings, translation, contra presentation, duplicate rows (summed) | Deterministic, logged in lineage |
| **Flag and continue** (REVIEW) | Estimated rates, duplicates, orphan below materiality, prior-period gaps, COA structure | Statements issuable after sign-off |
| **Quarantine** | JEs with unknown accounts or that duplicate a system process | Held until a reviewer decides |
| **Reject** | Unbalanced, circular, out-of-period or duplicate-ID entries | Returned to the preparer with the reason |
| **Block** | Unbalanced ledger, missing rate, material orphan or estimate, stale approval, any lineage or identity failure | Drafts only, exit 2, proposed fixes |
