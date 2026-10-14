# Reflection

## 1. With 3 months instead of 8 hours
- **Investigator agent** for suspense and exceptions: read-only GL tools, a step budget, findings rather than postings. It is the one place a real agent loop pays for itself.
- **Review service** in place of a JSON file: signed decisions, preparer ≠ approver, four eyes above materiality, and approvals that become a versioned COA-alias table.
- **Measure the mapper before trusting it:** label reviewer decisions, compare string match vs LLM precision per account type, and calibrate the LLM's self-reported confidence (today it is uncalibrated, which is one reason it never auto-applies).
- **Cash flow and SOCIE**, which need a complete prior TB. Then multi-entity work: IC matching, CTA, restatements as a versioned layer.

## 2. Where it breaks at scale
- **Mapper:** string similarity is O(TB × COA). At 5,000 × 3,000 it needs a retrieval index, and LLM calls need batching plus a cache keyed by account fingerprint.
- **Reviewer bandwidth breaks first.** Six review items on 62 rows becomes thousands on a real ledger. Findings must be grouped by root cause and ranked by materiality, or the queue gets rubber-stamped.
- **No entity dimension.** Consolidation needs per-entity runs feeding a layer that does IC matching (tolerances, ageing, disputes), CTA and minority interest, and handles different period ends.
- **One process, in memory:** fine to about a million rows. Beyond that, partition by account and stream the lineage to a store instead of embedding it in JSON.

## 3. How I used AI tools
**Where they helped:**
- cross-referencing the five files to find defects (the 6905 → 6900 rename, the duplicate 6310 rows);
- loaders and boilerplate;
- pressure-testing the code/LLM boundary with "argue against using an LLM for X".

I used Claude Code for a final review pass, the refactor, the tests and the Docker setup. I read every change and made the accounting calls (what blocks, the FX policy, what a model may touch) myself.

**Where they led me wrong:**
- **Over-engineering:** my first architecture had seven agents, because the model happily designs a swarm when asked. Six of the seven wrapped arithmetic.
- **FX fallback:** the model suggested the period-average rate for the missing GBP period-end rate. Average is a flow rate. I used the opening rate (last period's close, a point-in-time rate) and quantified what the choice is worth: 5,360 USD against materiality of 365,162.
- **The sign bug.** AI-written sign logic keyed on `normal_balance`, so contra assets were *added* to assets, overstating them by 11.25M. When I chased it, the model proposed increasingly complex fixes, and I settled for "report the residual honestly". A later AI-assisted review decomposed the 11.41M residual: 98.6% of it was the bug. The lesson is that a verifier must *explain* its residual. `BS_IDENTITY` now blocks on any difference the ledger doesn't account for, and a test re-introduces the bug to prove it.
- **Fuzzy matching:** `difflib` "mapped" Conf travel to COGS - Materials. That is why non-exact matches are only ever suggestions.

## 4. What I think is underestimated
**Knowing what *state* each input is in matters more than mapping it.** In this small bundle:
- the TB balances only if EUR and GBP amounts are read at face value, so either they aren't really EUR and GBP, or 181,900 of remeasurement was never booked;
- JE-003 recomputes a revaluation the ledger may or may not already contain;
- the prior TB looks pre-close, and retained earnings don't roll by 640,800.

No model can answer these from the files; only the ERP's data dictionary and the controller can. A pipeline that guesses produces clean-looking statements that are wrong by six figures. The product is the system that notices, quantifies and asks, and then remembers the answer.
