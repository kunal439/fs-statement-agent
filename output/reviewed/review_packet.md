# Review packet: 2024-Q4

Run `14210a2d9f3c` · status **REVIEW**: statements are complete but not issuable until the items below are signed off.
Reviewer decisions applied: Demo Controller

## Headline numbers (USD)

| | Amount |
|---|---:|
| Total assets | 36,218,200.30 |
| Suspense (unmapped) | 17,200.00 |
| Total liabilities | 18,770,000.00 |
| Total equity | 17,465,400.30 |
| **Balance sheet difference** | **0.00** |
| Gross profit | 17,835,000.00 |
| Operating income | 5,994,500.00 |
| Income before tax | 5,803,400.30 |
| Net income | 4,965,400.30 |

## Decisions needed (5)

1. **[REVIEW] Not met: Every adjustment is posted or has a reviewer decision** (JE_EXCEPTIONS). JE-005 QUARANTINED (reasons in the adjustments table).
   *Action:* Correct and resubmit, or record a decision in the review file. Or sign off with key `ce2d3066a860`.
2. **[REVIEW] Not met: Every trial-balance account maps to the COA** (TB_ACCOUNTS_MAPPED). 9999 'Suspense - Unmapped' (ORPHAN, balance 17,200.00; no account fits). Shown on the balance sheet as suspense, never dropped.
   *Action:* Find out what was posted there and map it in the review file, or reclass it in the ERP. Or sign off with key `788155afec97`.
3. **[REVIEW] Not met: Each account/currency appears once in the trial balance** (TB_DUPLICATE_ROWS). 6310 Travel and Entertainment has 2 USD rows (row 48: 245,000.00, row 49: 38,500.00), summed to 283,500.00. Right for split postings, wrong for a duplicated export line.
   *Action:* Confirm against the ERP account detail. Or sign off with key `c390969139ec`.
4. **[REVIEW] Not met: Prior-period trial balance balances** (PRIOR_TB_BALANCED). prior_period_tb.csv is out of balance by 2,832,800.00 at face value and has P&L rows only for ['6905']: it looks like a pre-close TB with the P&L detail missing, so it can't serve as comparatives as-is.
   *Action:* Request the full prior-period TB (with P&L detail) from the ERP. Or sign off with key `a025033513a6`.
5. **[REVIEW] Not met: Opening retained earnings roll from the prior period** (RE_ROLLFORWARD). Opening retained earnings in the current TB are 7,240,000.00; the prior TB shows 5,180,000.00. If the prior TB is pre-close with P&L detail missing, prior net income is about 2,700,800.00 and prior closing RE about 7,880,800.00, so opening RE is 640,800.00 lower than it should be (dividends? a prior-period restatement?). Doesn't change this period's BS or P&L; blocks the Statement of Changes in Equity.
   *Action:* Confirm dividends and any restatement with the controller (docs/questions.md Q3). Or sign off with key `240e93d9a098`.

## Signed off

- **FX_ESTIMATES** by Demo Controller: The 5,359.90 swing across available GBP rates is about 1.5% of materiality. Accept the opening-rate estimate for this close; treasury to supply the period-end rate.

## Proposed entries (computed by the verifier, posted only once approved)

| ID | Side | Amount | Account | Why |
|---|---|---:|---|---|
| SYS-FX | credit | 181,900.30 | 7310 or 3310 | Translating foreign-currency rows at period rates changes their USD value by 181,900.30, and no other row offsets it: the source TB only balances at face value. Post the offset to one of: 7310 (remeasurement of monetary items to P&L, right for a USD-functional entity); 3310 (translation of a foreign operation to the OCI reserve). The amount assumes the ERP carried these rows at face value; confirm (docs/questions.md Q1). |
| SYS-SUSPENSE | debit | 4,800.00 | 9999 | The remaining 4,800.00 is already in trial_balance.csv (debits 71,072,200.00 vs credits 71,077,000.00 at face value). It is below materiality (365,162.00), so it can be parked in 9999 Suspense while the ERP export is investigated. It is never spread across accounts. |

## Manual adjustments

| JE | Description | Status | Why |
|---|---|---|---|
| JE-001 | Accrue Q4 bonus pool | ACCEPTED | Balanced, accounts exist, in period. |
| JE-002 | Reclassify marketing spend wrongly booked to T&E | REJECTED | Reviewer (Demo Controller): Returned to preparer: debit 28,500 vs credit 25,000. Confirm the reclass amount and resubmit. Debits total 28,500.00 but credits total 25,000.00, so the entry is out of balance by 3,500.00. An unbalanced entry can't be posted, and the pipeline won't guess which line is wrong: return it to the preparer. |
| JE-003 | FX revaluation of EUR cash balance to period-end rate | REJECTED | Reviewer (Demo Controller): The pipeline remeasures EUR cash at the period-end rate; posting this entry as well would double-count. This entry revalues 1110 Cash and Cash Equivalents through 7310 FX Gain/Loss - Unrealized, but the pipeline already translates the foreign-currency rows of 1110 Cash and Cash Equivalents at the period-end rate. Posting both would count the revaluation twice. Held for a reviewer to confirm and reject it in the review file. |
| JE-004 | Bad debt provision top-up | ACCEPTED | Balanced, accounts exist, in period. |
| JE-005 | Reclass conference travel from old code | QUARANTINED | Account 6315 is not in the chart of accounts. Add it to the COA or map it in the review file; the entry is held until then. |
| JE-006 | Depreciation catch-up — Q4 | ACCEPTED | Balanced, accounts exist, in period. |
| JE-007 | Deferred tax true-up | ACCEPTED | Balanced, accounts exist, in period. |
| JE-008 | Intercompany settlement — UK sub | REJECTED | Reviewer (Demo Controller): Placeholder IC settlement with no counter-leg. IC team to repost against cash once the UK sub confirms. Every account in this entry nets to zero (2170), so it balances but changes nothing. It looks like an intercompany settlement with the counter-leg missing (cash, or the other entity's receivable). Repost it with the real counter-accounts. |
| JE-009 | Accrue legal fees for ongoing litigation | ACCEPTED | Balanced, accounts exist, in period. |
| JE-010 | Reclass long-term debt current portion | ACCEPTED | Balanced, accounts exist, in period. |

## For information

- **Chart of accounts structure**: cash-flow category TBD: 1150, 2170; header with no children: 1290; postable account used as a parent: 3300, 8000; missing normal balance: 7000; contra account (presented negative): 1121, 1211, 1221, 3400, 4200. None changes the BS or P&L; the TBD categories must be decided before a cash-flow statement.
- Checks passed: INPUT_HEADER, FX_RATES_AVAILABLE, LEDGER_BALANCED, REVIEW_FILE, JE_POSTED_BALANCED, PRIOR_ACCOUNTS_MAPPED, LINEAGE_COMPLETE, LINEAGE_TIES, BS_IDENTITY, NI_TO_EQUITY.

## How to respond

Record decisions in a review file (example: `reviews/2024-Q4.json`) and re-run with `--review <file>`. Approvals bind to exact amounts and sign-offs to the finding's key, so a change in the numbers re-opens them. Trace any line with `python src/trace.py <code>`.
