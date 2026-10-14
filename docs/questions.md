# Clarifying Questions and Working Assumptions

The brief allows three questions before starting. These are the three I would
send, because each one changes **numbers on the statements**, not just
presentation, and none can be answered from the files alone. The prototype
makes an explicit, documented assumption for each one. For Q2 and Q3 an
answer changes a review-file decision or a config value. For Q1, an answer
other than "face value" needs one more input, the ERP's USD carrying value
per row: `SYS-FX` then becomes closing value minus carrying value. The design
stays the same.

## The three pre-start questions

**Q1. What do the EUR and GBP amounts in `trial_balance.csv` represent, and what USD value does the ledger carry for them?**
The TB balances (to within 4,800.00) only if every amount is read at face
value. Translated at period-end rates, the EUR and GBP cash rows gain
181,900.30 that nothing offsets. The answer decides what that difference is:
- a remeasurement gain in P&L (account 7310, right for a USD-functional entity holding foreign cash);
- a translation reserve movement in OCI (account 3310, if the cash belongs to a foreign operation);
- or an export bug, where the amounts are already USD with a currency tag.

`JE-003` adds a fourth reading: its 11,200 revaluation implies a carrying
rate of about 1.0814, not face value.
*Assumption:* amounts are in transaction currency, and the ledger carries them
at face value. The difference is **proposed** as `SYS-FX` and never posted
without approval. The demo approval sends it to 7310.

**Q2. Are the manual adjustments already posted in the TB export, or still to be posted on top of it?**
If the export is post-adjustment, applying the ten entries double-counts every
one. `JE-003` is a symptom of the ambiguity: it revalues EUR cash, which the
pipeline also revalues.
*Assumption:* the TB is pre-adjustment, so entries are posted on top. Any
entry that duplicates a system process (JE-003) is held for a reviewer.

**Q3. Is `prior_period_tb.csv` a pre-close or post-close TB, and were dividends or restatements booked between the periods?**
Opening retained earnings in the current TB are 7,240,000.00; the prior TB
shows 5,180,000.00. The prior TB is out of balance by 2,832,800.00 and has
only one P&L account (6905). If it is pre-close with the P&L detail missing,
prior net income was about 2,700,800.00, and opening RE is still 640,800.00
short.
*Assumption:* the prior TB is not usable for comparatives or the SOCIE as it
stands. The pipeline flags the roll-forward break (`RE_ROLLFORWARD`) and does
not invent a bridge.

## Questions for the kickoff call

| # | Question | Why it matters | Assumption in the prototype |
|---|---|---|---|
| 4 | US GAAP or IFRS? | FX treatment (ASC 830 vs IAS 21), presentation of contra accounts, and the cash-flow classification of interest | Framework-neutral; FX handled as for a USD-functional entity |
| 5 | What is the materiality basis, and who signs off? | Decides BLOCK vs REVIEW for unmapped balances and FX estimates | 1% of total assets in the translated TB before adjustments (365,162.00 here), config `MATERIALITY` |
| 6 | Where does the period-end GBP rate come from, and what is the fallback policy? | GBP has no period-end rate | Fall back to the opening rate (last period's close, a point-in-time rate). The 5,359.90 swing is reported against materiality |
| 7 | Is there a UK subsidiary in scope ("JE-008 Intercompany settlement, UK sub")? | Single entity or group: elimination of 2170 against the sub's receivable | Single entity; 2170 stays on the balance sheet |
| 8 | What is in 9999 Suspense (12,400.00), and is there an approved way to clear it? | A suspense balance usually hides an unposted or misposted transaction | Shown as a suspense line on the BS, never dropped or reclassified |
| 9 | Are the two 6310 rows (245,000 and 38,500) split postings or a duplicated export line? | Double-count risk of 38,500 | Summed and flagged for review |
| 10 | Should 6315 "Conf travel" be added to the COA, or mapped (to 6310 or 6300)? | Mapping it to 6310 turns JE-005 into a no-op, because JE-005 also credits 6310 | JE-005 is held; the reviewer decides |
| 11 | Cash-flow category for 1150 Other Current Assets and 2170 Intercompany Payable (both `TBD`) | Needed before a cash-flow statement | Not needed for the BS and P&L slice; listed as information |
| 12 | Are 3300 Accumulated OCI and 8000 Income Tax Expense postable or headers? | Both are typed postable but act as parents | Grouped as parents; they hold no balance in this data |
| 13 | Is the COA `account_type` field authoritative for presentation? 1121, 1211 and 1221 are typed `Asset` with credit normal balances | Contra-asset presentation | Yes: sign is driven by type, so contra accounts present as negatives |
| 14 | Which approvals need four eyes (preparer ≠ approver)? | Segregation of duties for the review file | One reviewer per review file; production would enforce two |

## Where each assumption lives

| Assumption | Code / file | How to change it |
|---|---|---|
| Functional currency, period, materiality | `src/pipeline.py` CONFIG block | Edit the constant; the run id changes with it |
| FX fallback (period-end to opening) | `RATE_FALLBACK` | Remove the entry to make a missing rate blocking |
| Where the FX difference goes | `FX_DIFFERENCE_OPTIONS`, review file `SYS-FX.account` | Reviewer picks 7310 or 3310 |
| Suspense account for an approved TB difference | `SUSPENSE_ACCOUNT` | Config |
| Mapping decisions, JE rejections, approvals | `reviews/2024-Q4.json` | Reviewer edits; approvals bind to exact amounts |
