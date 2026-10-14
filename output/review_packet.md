# Review packet: 2024-Q4

Run `dab528f778f6` · status **BLOCKED**: statements are drafts and must not be issued until the blocking items are resolved.
Reviewer decisions applied: none (raw run)

## Headline numbers (USD)

| | Amount |
|---|---:|
| Total assets | 36,218,200.30 |
| Suspense (unmapped) | 12,400.00 |
| Total liabilities | 18,770,000.00 |
| Total equity | 17,283,500.00 |
| **Balance sheet difference** | **177,100.30** |
| Gross profit | 17,835,000.00 |
| Operating income | 5,994,500.00 |
| Income before tax | 5,621,500.00 |
| Net income | 4,783,500.00 |

## Decisions needed (9)

1. **[BLOCK] Not met: Ledger balances after translation and posting** (LEDGER_BALANCED). Debits minus credits = 177,100.30. Breakdown: -4,800.00 already in trial_balance.csv at face value; 181,900.30 from translating foreign-currency rows with no offsetting entry.
   *Action:* Approve the proposed entries in a review file (template: reviews/2024-Q4.json), or fix the source export, then re-run.
2. **[BLOCK] Not met: Assets + suspense = liabilities + equity** (BS_IDENTITY). Assets 36,218,200.30 + suspense 12,400.00 vs liabilities 18,770,000.00 + equity 17,283,500.00: difference 177,100.30. Fully explained by the ledger imbalance (see LEDGER_BALANCED).
   *Action:* Resolve LEDGER_BALANCED.
3. **[REVIEW] Not met: No translation relies on an estimated rate** (FX_ESTIMATES). trial_balance.csv row 4 (GBP 412,300.00) has no period_end rate, so it uses the opening rate 1.251 (last period's closing rate, a point-in-time rate like the missing one). Across the available GBP rates its USD value moves by 5,359.90, against materiality of 365,162.00.
   *Action:* Get the period-end rate from treasury and re-run, or sign off on the estimate. Or sign off with key `8cfbb78147e6`.
4. **[REVIEW] Not met: Every adjustment is posted or has a reviewer decision** (JE_EXCEPTIONS). JE-002 REJECTED, JE-003 QUARANTINED, JE-005 QUARANTINED, JE-008 REJECTED (reasons in the adjustments table).
   *Action:* Correct and resubmit, or record a decision in the review file. Or sign off with key `9b0b3227d29a`.
5. **[REVIEW] Not met: Every trial-balance account maps to the COA** (TB_ACCOUNTS_MAPPED). 9999 'Suspense - Unmapped' (ORPHAN, balance 12,400.00; no account fits). Shown on the balance sheet as suspense, never dropped.
   *Action:* Find out what was posted there and map it in the review file, or reclass it in the ERP. Or sign off with key `c8ae0812870e`.
6. **[REVIEW] Not met: Every prior-period account maps to the COA** (PRIOR_ACCOUNTS_MAPPED). 6905 'Sundry Operating Expenses' (SUGGESTED; suggested 6900 Other Operating Expenses (0.816)). Needed for comparatives and the equity roll-forward.
   *Action:* Approve or correct the mapping in the review file. Or sign off with key `fb3311a9f3dc`.
7. **[REVIEW] Not met: Each account/currency appears once in the trial balance** (TB_DUPLICATE_ROWS). 6310 Travel and Entertainment has 2 USD rows (row 48: 245,000.00, row 49: 38,500.00), summed to 283,500.00. Right for split postings, wrong for a duplicated export line.
   *Action:* Confirm against the ERP account detail. Or sign off with key `c390969139ec`.
8. **[REVIEW] Not met: Prior-period trial balance balances** (PRIOR_TB_BALANCED). prior_period_tb.csv is out of balance by 2,832,800.00 at face value and has P&L rows only for ['6905']: it looks like a pre-close TB with the P&L detail missing, so it can't serve as comparatives as-is.
   *Action:* Request the full prior-period TB (with P&L detail) from the ERP. Or sign off with key `a025033513a6`.
9. **[REVIEW] Not met: Opening retained earnings roll from the prior period** (RE_ROLLFORWARD). Opening retained earnings in the current TB are 7,240,000.00; the prior TB shows 5,180,000.00. If the prior TB is pre-close with P&L detail missing, prior net income is about 2,700,800.00 and prior closing RE about 7,880,800.00, so opening RE is 640,800.00 lower than it should be (dividends? a prior-period restatement?). Doesn't change this period's BS or P&L; blocks the Statement of Changes in Equity.
   *Action:* Confirm dividends and any restatement with the controller (docs/questions.md Q3). Or sign off with key `240e93d9a098`.

## Proposed entries (computed by the verifier, posted only once approved)

| ID | Side | Amount | Account | Why |
|---|---|---:|---|---|
| SYS-FX | credit | 181,900.30 | 7310 or 3310 | Translating foreign-currency rows at period rates changes their USD value by 181,900.30, and no other row offsets it: the source TB only balances at face value. Post the offset to one of: 7310 (remeasurement of monetary items to P&L, right for a USD-functional entity); 3310 (translation of a foreign operation to the OCI reserve). The amount assumes the ERP carried these rows at face value; confirm (docs/questions.md Q1). |
| SYS-SUSPENSE | debit | 4,800.00 | 9999 | The remaining 4,800.00 is already in trial_balance.csv (debits 71,072,200.00 vs credits 71,077,000.00 at face value). It is below materiality (365,162.00), so it can be parked in 9999 Suspense while the ERP export is investigated. It is never spread across accounts. |

## Manual adjustments

| JE | Description | Status | Why |
|---|---|---|---|
| JE-001 | Accrue Q4 bonus pool | ACCEPTED | Balanced, accounts exist, in period. |
| JE-002 | Reclassify marketing spend wrongly booked to T&E | REJECTED | Debits total 28,500.00 but credits total 25,000.00, so the entry is out of balance by 3,500.00. An unbalanced entry can't be posted, and the pipeline won't guess which line is wrong: return it to the preparer. |
| JE-003 | FX revaluation of EUR cash balance to period-end rate | QUARANTINED | This entry revalues 1110 Cash and Cash Equivalents through 7310 FX Gain/Loss - Unrealized, but the pipeline already translates the foreign-currency rows of 1110 Cash and Cash Equivalents at the period-end rate. Posting both would count the revaluation twice. Held for a reviewer to confirm and reject it in the review file. |
| JE-004 | Bad debt provision top-up | ACCEPTED | Balanced, accounts exist, in period. |
| JE-005 | Reclass conference travel from old code | QUARANTINED | Account 6315 is not in the chart of accounts. Add it to the COA or map it in the review file; the entry is held until then. |
| JE-006 | Depreciation catch-up — Q4 | ACCEPTED | Balanced, accounts exist, in period. |
| JE-007 | Deferred tax true-up | ACCEPTED | Balanced, accounts exist, in period. |
| JE-008 | Intercompany settlement — UK sub | REJECTED | Every account in this entry nets to zero (2170), so it balances but changes nothing. It looks like an intercompany settlement with the counter-leg missing (cash, or the other entity's receivable). Repost it with the real counter-accounts. |
| JE-009 | Accrue legal fees for ongoing litigation | ACCEPTED | Balanced, accounts exist, in period. |
| JE-010 | Reclass long-term debt current portion | ACCEPTED | Balanced, accounts exist, in period. |

## For information

- **Chart of accounts structure**: cash-flow category TBD: 1150, 2170; header with no children: 1290; postable account used as a parent: 3300, 8000; missing normal balance: 7000; contra account (presented negative): 1121, 1211, 1221, 3400, 4200. None changes the BS or P&L; the TBD categories must be decided before a cash-flow statement.
- Checks passed: INPUT_HEADER, FX_RATES_AVAILABLE, JE_POSTED_BALANCED, LINEAGE_COMPLETE, LINEAGE_TIES, NI_TO_EQUITY.

## How to respond

Record decisions in a review file (example: `reviews/2024-Q4.json`) and re-run with `--review <file>`. Approvals bind to exact amounts and sign-offs to the finding's key, so a change in the numbers re-opens them. Trace any line with `python src/trace.py <code>`.
