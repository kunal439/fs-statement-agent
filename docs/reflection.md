# Reflection

## 1. What I would build differently with 3 months

Three things, in priority order.

**Lineage as a graph database, not a list of notes.** Right now lineage is a
flat JSON array attached to FX translations. With time, I would store every
statement cell, every transformation, and every source row as a node in a
graph. The query would be: "show me every input that contributed to this
line." Auditors need that traversal, not a static log.

**A learning mapper.** The Mapper's fuzzy matching is deterministic — it works,
but every re-run gives the same confidence. In production, every human override
of a mapping becomes a training signal. After a few hundred overrides, a
fine-tuned embedding model replaces the string-similarity heuristic entirely,
and the LLM only handles genuinely novel cases.

**Multi-entity consolidation.** The current pipeline is single-entity. Real
groups need IC elimination matrices, minority interest, equity pickup, and
currency translation adjustments that flow to OCI. That is where the design
actually gets hard, and it is a different problem from the single-entity
mapping and validation I built here.

## 2. Where the prototype would break at scale

**Fuzzy matching at 5,000 accounts.** The `name_similarity` function is O(n*m)
per lookup. At 5,000 TB rows against a 3,000-account COA, that is 15 million
comparisons per run. Needs a FAISS-style embedding index to stay under a second.

**Concurrency.** Two finance users posting adjustments simultaneously would
corrupt the balance dict. The current code is single-threaded batch processing.
Production needs optimistic locking on the balances structure.

**FX at 50 currencies.** The rate table is loaded into memory as a dict, and
fallbacks are resolved by linear scan. At scale it needs a proper rate service
with audit logging, version history, and support for multiple rate sources
(Bloomberg, ECB, corporate treasury override).

**The Auditor catches only what it was told to catch.** Every check in
`CHECKS` is manual. At 5,000 accounts, a new class of failure emerges — an
account that maps correctly but to the wrong sibling (e.g., a debit posted to
`6200 Rent` instead of `6300 Marketing`). Catching that needs anomaly detection
trained on historical posting patterns, which I did not design.

## 3. How I used AI tools

**Where they helped:**

- Finding the defect inventory. Claude cross-referenced the TB, COA, and
  adjustments and surfaced the `6905 -> 6900` rename across periods and the
  duplicate `6310` rows — things I would have missed on first read.
- Boilerplate for the code. The dataclasses, CSV loaders, and JSON writers
  were AI-generated and reviewed. Saved about two hours.
- Stress-testing the architecture. I prompted with "argue against using an
  LLM for X" to pressure-test the deterministic/agentic boundary.

**Where they led me wrong:**

- My first architecture draft had seven agents. The LLM happily designed a
  swarm because that was the framing I gave it. I had to pull back to four
  checkpoints plus a deterministic core. The assignment explicitly warns
  against over-engineering, and the default LLM behavior is exactly that.
- The LLM suggested using the period-average rate as a fallback for missing
  period-end rates. That is wrong for balance-sheet revaluation — you use the
  closing rate for BS items and average for PL items. I caught it in review.
- When I hit a persistent sign-convention bug in the balance sheet builder,
  the LLM kept suggesting increasingly complex fixes. The actual resolution was
  simpler: the design intent (report the residual honestly as a validation
  failure) was already the correct answer, not the balanced sheet I was
  chasing.

## 4. What I think the assignment underestimates

**Intercompany elimination is a matching problem, not a netting problem.**

The seeded defect `JE-008` is obvious — a circular entry that nets to zero on
one account. Real IC elimination is harder. Entity A's IC receivable must match
Entity B's IC payable, and in multi-currency groups they rarely match exactly
because of timing differences, FX rate differences, disputed invoices, and
different fiscal calendars. The elimination engine needs tolerance thresholds,
ageing analysis, and a reconciliation workflow that routes mismatches to the
right entity controller.

That is roughly 40% of consolidation effort in real finance teams, and it is
essentially invisible in this mock data. A submission that only handles
single-entity mapping and single-JE validation, however well-executed, is
solving the easy half of the problem.

The second underestimate: the COA is a living document. Accounts get renamed,
merged, split, and deprecated. The mapping problem is not "map TB to COA" —
it is "map TB to COA *version* and reconcile COA versions across periods."
The `6905 -> 6900` rename is the simplest case. Real ERPs have COA change
logs with effective dates, and the pipeline has to respect them.