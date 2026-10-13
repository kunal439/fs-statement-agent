# Defect Inventory — Input Data (2024-Q4)

Analysis of the 5 input files. Defects found: **21** across 5 categories.
Severity: **Critical** (blocks generation) · **High** (material) · **Medium** (review) · **Low** (cosmetic).

## A. Trial Balance (`trial_balance.csv`)

| ID | Defect | Evidence | Severity | Handling |
|----|--------|----------|----------|----------|
| TB-1 | Unbalanced TB (debits ≠ credits) | Sum differs by ~1,000,300 USD-equivalent | Critical | Requires FX translation + adjustment review |
| TB-2 | Duplicate account code | `6310` appears twice (245,000 + 38,500) | Medium | Sum rows; flag for reviewer |
| TB-3 | Orphan account not in COA | `9999 Suspense - Unmapped` | High | Route to Mapper; surface as reconciling item |
| TB-4 | Multi-currency rows | `1110 Cash` has USD, EUR, GBP rows | Medium | Translate using period-end rates |
| TB-5 | Suspense account in use | `9999` implies upstream unmapped transaction | High | Escalate; do not silently absorb |

## B. Chart of Accounts (`chart_of_accounts.csv`)

| ID | Defect | Evidence | Severity | Handling |
|----|--------|----------|----------|----------|
| COA-1 | Ambiguous CF category | `1150 Other Current Assets` = `TBD` | Medium | Default to Operating; flag reviewer |
| COA-2 | Ambiguous CF category | `2170 Intercompany Payable` = `TBD` | Medium | Default to Financing; flag reviewer |
| COA-3 | Header with no children | `1290 Other Assets` | Low | Signals incomplete COA |
| COA-4 | Missing normal balance | `7000 Non-Operating Items` (Header) | Low | Header nodes don't post; no impact |

## C. Manual Adjustments (`manual_adjustments.json`)

| ID | Defect | Evidence | Severity | Handling |
|----|--------|----------|----------|----------|
| JE-1 | Debits ≠ Credits | `JE-002`: 28,500 vs 25,000 (Δ 3,500) | High | **Reject** with explanation |
| JE-2 | Account not in COA | `JE-005` references `6315` | High | **Quarantine** until mapped |
| JE-3 | Circular intercompany entry | `JE-008` debits and credits `2170` for same 320,000 | High | **Reject** — no economic substance |
| JE-4 | Imbalanced reclassification | `JE-002` intends 25,000 but debits 28,500 | Medium | Cannot auto-correct; return to preparer |

## D. FX Rates (`fx_rates.csv`)

| ID | Defect | Evidence | Severity | Handling |
|----|--------|----------|----------|----------|
| FX-1 | Missing GBP period-end rate | Only GBP average + opening present | High | Fall back to opening; flag as estimated |
| FX-2 | No USD opening rate row | USD functional; rate = 1.0 implicit | Low | Document assumption |

## E. Cross-file / Prior Period (`prior_period_tb.csv`)

| ID | Defect | Evidence | Severity | Handling |
|----|--------|----------|----------|----------|
| X-1 | Renamed account across periods | Prior has `6905`, current has `6900` | Medium | Map prior → current via Mapper |
| X-2 | Prior missing detailed PL accounts | No revenue/expense detail beyond 6905 | Medium | Comparatives partial; disclose |
| X-3 | Prior references account not in current COA | `6905` | Medium | Same as X-1 |
| X-4 | Current has account not in prior | `9999` | Low | Expected — new suspense account |

## F. Handling Strategy

Every defect maps to one of four outcomes:

- **Auto-handle:** FX fallback, currency translation, exact mappings
- **Flag-and-continue:** duplicate rows, ambiguous CF, header anomalies
- **Quarantine:** orphan accounts, JEs with unmapped accounts
- **Reject:** imbalanced JEs, circular IC entries

No defect is silently absorbed. Every one surfaces in the run log or in
`output/validation_findings.json`.