# Architecture: Agentic Financial Statement Generator

**Period:** 2024-Q4 · **Functional currency:** USD · **Entity:** single, NetSuite-style export
**Prototype slice:** Statement generator for P&L + Balance Sheet, with a verifier that catches its own errors before output (`src/pipeline.py`)

---

## 0. Summary

- **"Generate financials" is ten sub-problems** with very different reliability requirements (§1). Eight are arithmetic, policy or bookkeeping and must be exact and replayable. Two need semantics: mapping unfamiliar accounts, and investigating exceptions.
- **The orchestrator is plain code, not an LLM.** The control flow is known in advance, and the output must replay byte-for-byte. The LLM is a tool called at a few judgment points (§3).
- **One rule bounds the AI:** a model may *propose, explain or investigate*. It never produces a number that reaches a statement. Numbers change only through deterministic code or a reviewer's recorded decision (§2).
- **The verifier diagnoses and proposes; it never plugs.** On this data it decomposes a 177,100.30 imbalance exactly, proposes two entries that would fix it, and blocks until a human approves them (§6).
- **Every statement line carries its lineage** down to file and row, FX rate and rate source, JE line, or approved system entry. The verifier proves that every source row is used exactly once (§7).

---

## 1. Problem decomposition

| # | Sub-problem | What makes it hard here | Reliability requirement | Approach | Prototype |
|---|---|---|---|---|---|
| 1 | **Ingest and normalise** | Mixed currencies; duplicate rows; a source TB that doesn't balance | Total and exact; reject malformed input | Code; strict parsing at the trust boundary, `Decimal` money | ✅ |
| 2 | **COA mapping** | Orphans (9999), renamed accounts (6905→6900), unknown JE accounts (6315) | Never wrong silently; humans own non-exact maps | Code for exact matches; LLM *suggests* the rest; reviewer approves | ✅ |
| 3 | **FX translation / remeasurement** | Missing GBP period-end rate; translation creates an unexplained 181,900.30 | Exact, policy-driven, rate source traceable | Code + rate policy; estimates flagged with sensitivity | ✅ |
| 4 | **Adjustment validation and posting** | Unbalanced JE, unknown account, circular IC entry, JE duplicating translation | Exact; every rejection explained to a finance user | Code (rules) + plain-English reasons | ✅ |
| 5 | **Intercompany elimination** | Matching across entities, FX, timing, disputes | Exact once matched; matching is fuzzy | Code matching engine + human for breaks | Design (§10) |
| 6 | **Opening balances, comparatives, restatements** | Prior TB incomplete; retained earnings don't roll | Exact; restatements versioned | Code + human confirmation | Checks only |
| 7 | **Statement assembly** (BS, P&L, CF, SOCIE) | Contra accounts, close to RE, CF classification | Exact | Code | BS + P&L ✅, CF/SOCIE design (§9) |
| 8 | **Verification and self-correction** | Must catch the builder's own bugs, not just bad data | Exhaustive; blocking | Code: independent identities and lineage checks | ✅ |
| 9 | **Lineage / audit trail** | Every cell → source rows, rates, decisions | Complete, immutable | Code; lineage embedded per line | ✅ |
| 10 | **Review, override, publication** | Humans must decide; decisions must be auditable and must not go stale | Every override attributable and bound to an amount | Review file (UI in production), status gate, exit codes | ✅ |

The split matters because each row needs a different failure policy. An
ingest error stops the run (exit 1). A mapping doubt becomes a review item. A
ledger imbalance blocks issue. An IC break goes to an entity controller.

---

## 2. Where deterministic code ends and LLM reasoning begins

**Rule of thumb:** if a competent junior accountant with a calculator and a
lookup table could do it, it is code. An LLM is justified only where the input
is natural language or the task is open-ended, *and* a human or a
deterministic check sits between the model and the ledger.

| Task | LLM? | Why | In prototype |
|---|---|---|---|
| Sum, translate, post, close, build statements | **No** | Arithmetic and policy | Code |
| Exact COA match | **No** | Hash lookup | Code |
| Map an account with no exact match (9999, 6905, 6315) | **Yes, suggest only** | Semantic. String similarity fails here: it proposes 6315 "Conf travel" → 5100 *COGS - Materials* (0.52) and 9999 "Suspense" → 7310 *FX Gain/Loss* (0.455) | `--llm`: Claude with output constrained to a JSON-schema `enum` of postable COA codes or `NONE`; re-validated; never applied |
| Detect a circular entry (JE-008) | **No** | "Every account nets to zero" is arithmetic. My first design said LLM here; that was wrong | Code |
| Detect a JE that duplicates a system process (JE-003) | Rule first, LLM later | The rule (touches 7310 and a translated account) catches this case; reading JE *descriptions* for broader overlaps is LLM work | Rule |
| Diagnose why the ledger doesn't balance | **No** | An identity: face difference + translation uplift + approved entries = delta. An LLM can only make this less exact | Code |
| Explain a rejection to a finance user | Templates first | The six rejection types have fixed causes; templates are exact and testable | Templates |
| Investigate what sits in suspense (9999) | **Yes, agentic** | Open-ended: query GL detail, compare periods, read memos. Unknown number of steps; read-only; output is a finding, not a posting | Design (§3) |
| Narrative for management ("why did cash move") | **Yes** | Summarising structured lineage into prose | Design (Reporter) |
| Decide materiality, approve a mapping, choose an FX policy | **No: human** | Accountability can't be delegated to a model | Review file |

### The LLM checkpoint as built (`src/llm.py`)

- **Model:** `claude-opus-5-5`, effort `medium`, server-side refusal fallback enabled.
- **Constrained output:** `output_config.format` JSON schema; `coa_code` is an `enum` of the postable codes plus `NONE`. A hallucinated code is impossible by construction, and still rejected by a second check.
- **Prompt-injection hygiene:** ERP names and memos sit inside `<account>` tags, and the system prompt tells the model to classify them, never obey them.
- **"Nothing fits" is a first-class answer:** `NONE` beats a confident wrong mapping, and the prompt says why.
- **Failure means fallback:** API errors, refusals or malformed JSON fall back to the deterministic mapper, with a note in `mappings.json`.
- **Never applied:** the suggestion, the model id and the reasoning go to the review packet. Only the review file moves a balance. That is also why LLM non-determinism can't break idempotency (§8).
- **Tested without a network:** a fake client exercises valid, invented and `NONE` answers.

---

## 3. Agent topology

```
                       ┌───────────────────────────────────────────────────────────────┐
  inputs (immutable,   │ ORCHESTRATOR: deterministic code, a fixed DAG                 │
  hashed) ───────────▶ │                                                               │
                       │  ingest → map → translate → validate JEs → post → build →     │
                       │  VERIFY ──┬─ CLEAN  → publish                                 │
                       │           ├─ REVIEW → publish + review packet                 │
                       │           └─ BLOCKED→ drafts (watermarked) + proposed fixes   │
                       └──────┬──────────────────────┬───────────────────────┬─────────┘
                              │ tool call            │ tool call             │ hand-off
                     ┌────────▼────────┐   ┌─────────▼─────────┐   ┌─────────▼──────────┐
                     │ MAPPER (LLM)    │   │ REPORTER (LLM)    │   │ HUMAN REVIEWER     │
                     │ one structured  │   │ one call: prose   │   │ approves / rejects │
                     │ call per        │   │ from the review   │   │ in a review file;  │
                     │ unmatched acct  │   │ packet  [design]  │   │ re-run is the loop │
                     └─────────────────┘   └───────────────────┘   └─────────┬──────────┘
                                                                             │ asks
                                                                   ┌─────────▼──────────┐
                                                                   │ INVESTIGATOR       │
                                                                   │ (agent loop,       │
                                                                   │ read-only GL tools)│
                                                                   │ [design]           │
                                                                   └────────────────────┘
```

### Why this shape

- **Not "prompt a frontier model with the TB":** no lineage, no replay, numbers you can't defend.
- **Not one LLM agent with tools as orchestrator:** the sequence never varies, so an LLM choosing it adds nondeterminism and cost for nothing. Its mistakes would also be control-flow mistakes, the hardest kind to audit.
- **Not a swarm:** my first draft had seven agents. Six of them wrapped arithmetic.
- **Single-call LLM tools (Mapper, Reporter)** are workflows, not agents. Input and output are fixed, and the output is schema-constrained and checked.
- **One genuine agent (Investigator):** "what is in suspense?" has an unknown number of steps (query GL detail, follow a document number, compare to last period). It meets the bar for an agent:
  - the task can't be specified in advance;
  - the value is high (suspense hides misstatements);
  - the cost of error is low, because it is read-only and its output is a finding a human reads.

  Tools: `gl_lines(account, period)`, `document(id)`, `prior_period(account)`, all read-only and scoped to the entity and period, with a step budget.
- **The Verifier is not an LLM.** Its checks are identities, and an identity is either true or false.

---

## 4. Pipeline and data contracts

| Stage | Input → output | Deterministic | On failure |
|---|---|---|---|
| Ingest | 5 files → typed rows (`Decimal`, row numbers kept) | ✅ | `InputError`, exit 1: thousands separators, bad currency, duplicate COA codes, unknown parents |
| Map | all account codes in TB, prior TB, JEs → `mappings.json` | ✅ (LLM suggestions advisory) | Non-exact codes: `SUGGESTED` / `ORPHAN`, never applied |
| Translate | TB rows → USD (BS at period-end, P&L at average) | ✅ | Missing rate → BLOCK; fallback rate → flagged with sensitivity |
| Validate JEs | JEs → `ACCEPTED` / `REJECTED` / `QUARANTINED` + reasons | ✅ | Only `ACCEPTED` posts |
| Post | rows + accepted JEs + approved system entries → ledger | ✅ | Unmapped accounts post to their own code (suspense) |
| Build | ledger → BS and P&L, with closing entry to RE | ✅ | n/a (the verifier judges) |
| Verify | statements + ledger + inputs → findings → status | ✅ | BLOCK / REVIEW / INFO |
| Publish | everything → `output/` + manifest | ✅ | BLOCKED: statements watermarked `issuable: false`, exit 2 |

All money is `Decimal`, rounded to cents half-up at translation. JSON writes
amounts as strings (`"903813.00"`), so no float ever touches a balance.

---

## 5. Failure modes: how each one is handled on this data

### 5.1 Hallucinated account mappings
- **The risk is real even without an LLM.** The original string matcher mapped 9999 *Suspense* → 7310 *FX Gain/Loss* at 0.455, and `difflib` maps 6315 "Conf travel" → 5100 *COGS - Materials* at 0.52.
- **Handling:**
  1. Only exact code matches map automatically.
  2. Every other account gets at most a suggestion: string score ≥ 0.60, or an enum-constrained LLM answer.
  3. Suggestions are never applied.
  4. A reviewer mapping must target a *postable* COA account; anything else fails the run (`InputError`).
  5. Exact code matches whose name drifted (similarity < 0.60) are flagged `EXACT_NAME_DRIFT`.
- **Result:** 6905 → 6900 is suggested (0.816), and the demo reviewer approves it. 9999 gets no suggestion. 6315 gets none deterministically.

### 5.2 Debits ≠ credits, in an entry and in the ledger
- **Entry level:** JE-002 is **REJECTED**: "Debits total 28,500.00 but credits total 25,000.00, so the entry is out of balance by 3,500.00… return it to the preparer." The pipeline never guesses which line is wrong.
- **Ledger level:** after translation and posting, debits exceed credits by **177,100.30**. The verifier decomposes this exactly:
  - **−4,800.00** is already in `trial_balance.csv` at face value (debits 71,072,200.00 vs credits 71,077,000.00). This is the seeded "unbalanced after FX rounding".
  - **+181,900.30** comes from translating EUR and GBP cash at period rates with no offsetting entry. The source only balances at face value (see Q1).
- **Fix:** it proposes `SYS-FX` (credit 181,900.30 to 7310 or 3310) and `SYS-SUSPENSE` (debit 4,800.00 to 9999, allowed only because it is below materiality) and **blocks** until they are approved.

### 5.3 An account that fits no COA node
- **9999** (12,400.00) appears as an explicit **suspense line** on the balance sheet, never dropped. The identity is checked as *assets + suspense = liabilities + equity*.
- **Materiality decides the severity:** BLOCK if the balance exceeds materiality (1% of trial-balance total assets: 365,162.00), REVIEW otherwise.
- **JE-005** debits 6315, which is not in the COA, so it is **QUARANTINED** until 6315 is mapped or added.
- **Mapping trap:** if the reviewer maps 6315 → 6310, the validator warns the entry becomes a no-op, because it already credits 6310.

### 5.4 FX gaps
- **GBP has no period-end rate.** The pipeline uses the **opening rate 1.251**: the prior period's closing rate, a *point-in-time* rate like the missing one. The period average (1.264) is a *flow* rate meant for P&L items.
- **The estimate is quantified:** across the available GBP rates the USD value moves by **5,359.90**, against materiality of 365,162.00, so it is a REVIEW item. A swing above materiality would BLOCK.
- **No rate at all → BLOCK.** The row is held out of the ledger, shows as a `LINEAGE_COMPLETE` gap, and its face value appears in the `LEDGER_BALANCED` breakdown. It is never silently dropped (tested).
- **Two FX defects the brief didn't list:**
  - Translation itself unbalances the TB (§5.2).
  - JE-003 revalues EUR cash that the pipeline already translates at the period-end rate. Posting both double-counts, so JE-003 is **QUARANTINED** for a reviewer.

### 5.5 Circular intercompany entries
- **JE-008** debits and credits 2170 for 320,000. Every account nets to zero, so it is **REJECTED**: "balances but changes nothing… looks like an intercompany settlement with the counter-leg missing".
- **Detection is arithmetic**, which also catches multi-account variants where every account nets to zero.
- **Real IC is a matching problem** across entities (§10).

### 5.6 Defects found beyond the brief
Full inventory: `docs/defects.md`, 26 defects.
- duplicate 6310 rows;
- the prior TB is out of balance by 2,832,800.00;
- opening retained earnings don't roll (a 640,800.00 gap even after inferring prior net income);
- the FX reserve moved 95,000 → 180,000 with no OCI source;
- contra accounts typed `Asset` with credit normal balances: this trap caused the sign bug I describe in the reflection.

---

## 6. Validation and self-correction loop

### What the verifier checks before anything is issued

| Check | What it proves | On failure |
|---|---|---|
| `INPUT_HEADER` | Adjustments file is for 2024-Q4 / USD | BLOCK |
| `FX_RATES_AVAILABLE` | Every foreign row has a rate | BLOCK |
| `FX_ESTIMATES` | No fallback rates, or their swing is immaterial | REVIEW, or BLOCK above materiality |
| `LEDGER_BALANCED` | Debits = credits after translation and posting, with an exact decomposition | BLOCK |
| `REVIEW_FILE` | Every approval still matches the amount it approved | BLOCK |
| `JE_POSTED_BALANCED` | Every posted JE balances (rechecked independently) | BLOCK |
| `JE_EXCEPTIONS` | Every JE is posted or has a reviewer decision | REVIEW, or BLOCK if an open entry is material |
| `TB_ACCOUNTS_MAPPED` | Every TB account maps | REVIEW, or BLOCK above materiality |
| `PRIOR_ACCOUNTS_MAPPED` | Prior-period accounts map (comparatives, roll-forward) | REVIEW |
| `TB_DUPLICATE_ROWS` | No account/currency appears twice | REVIEW |
| `LINEAGE_COMPLETE` | Every TB row (translated or not), posted JE line and system entry lands in exactly one statement line | BLOCK |
| `LINEAGE_TIES` | Every line equals the sum of its lineage, under a sign rule restated independently of the builder | BLOCK |
| `BS_IDENTITY` | A + suspense = L + E; any difference must equal the ledger imbalance exactly, or the builder is wrong | BLOCK |
| `NI_TO_EQUITY` | P&L net income = subtotal ladder, and BS retained earnings = opening RE (from the raw TB) + posted RE entries + net income | BLOCK |
| `PRIOR_TB_BALANCED` | Prior TB usable for comparatives | REVIEW |
| `RE_ROLLFORWARD` | Opening RE = prior closing RE | REVIEW |
| `COA_STRUCTURE` | TBD categories, childless headers, postable parents, contra accounts | INFO |

**Run status** is the worst *open* severity. A REVIEW item closes when the reviewer signs it off by quoting its key (a fingerprint of the finding). If the numbers change, the key changes and the item re-opens. BLOCK items can't be signed off:

| Status | Meaning | Statements | Exit |
|---|---|---|---|
| **CLEAN** | Every check passed or was signed off | Issuable (`issuable: true`) | 0 |
| **REVIEW** | Numbers verified; sign-off items open | Complete but `issuable: false`; `review_packet.md` lists the items and their keys | 0 |
| **BLOCKED** | Numbers can't be trusted | Watermarked `DRAFT - BLOCKED, NOT FOR ISSUE`, `issuable: false` | 2 |

A non-zero exit means a scheduler can never mistake a BLOCKED run for success.

### What happens when a check fails

```
verify ──fail──▶ diagnose (exact decomposition) ──▶ propose exact entries ──▶ BLOCKED + review packet
   ▲                                                                                │
   └───────── deterministic re-run ◀── reviewer approves in review file (bound to amount)
```

1. **Diagnose exactly.** The verifier doesn't say "TB is off, check FX". It says "177,100.30 = −4,800.00 at source + 181,900.30 translation uplift."
2. **Propose, never plug.** `SYS-FX` and `SYS-SUSPENSE` are computed to the cent, but posting them automatically would put a 181,900.30 gain into the P&L without anyone deciding to. So the run blocks.
3. **A human decides** in `reviews/2024-Q4.json`. An approval records the **amount**; if an input changes, the approval goes stale and the run blocks (`REVIEW_FILE`, tested). A sign-off records the finding's **key**; if its numbers change, it re-opens (tested).
4. **Re-run.** The pipeline is a pure function, so re-running *is* the retry. On this data the reviewed run balances to 0.00. The GBP estimate is signed off. Five items stay open because the demo reviewer can't know the answers (JE-005, 9999, duplicate rows, prior TB, RE roll-forward), so the run is **REVIEW** and not issuable.

**Why no automatic retries?** A deterministic stage returns the same answer
every time, so retrying it is a no-op. Bounded retries belong only at the
network edge: the Anthropic SDK retries 429/5xx twice. An LLM answer that
fails validation is *discarded*, not retried until it complies.

### Verifying the verifier
The checks that matter most catch **the pipeline's own bugs**, not bad data.
The original builder keyed signs on `normal_balance`, so the three contra
assets were *added* to assets. That overstated assets by 11,250,000.00, and
the old verifier blamed "the TB imbalance". Today `BS_IDENTITY` requires the
residual to equal the ledger imbalance *exactly*. Anything else is reported
as a builder defect and blocks. `LINEAGE_TIES` re-derives every line's sign
with its own copy of the rule, and `NI_TO_EQUITY` recomputes retained
earnings from the raw TB rather than from the builder's closing entry. `tests/test_pipeline.py::test_verifier_catches_a_sign_bug`
re-introduces the original bug and asserts that both `BS_IDENTITY`
(`explained_by_ledger: false`) and `LINEAGE_TIES` fail and the run blocks.

---

## 7. Traceability: how an auditor gets from a cell to source rows

Every statement line embeds its lineage. Each component is a source row, a JE
line, or an approved system entry, with its debit-positive USD amount. For
example, the GBP part of Cash (`output/reviewed/balance_sheet.json`):

```json
{ "source": "trial_balance.csv", "row": 4, "account": "1110", "currency": "GBP",
  "amount": "412300.00", "usd": "515787.30", "rate": "1.251", "rate_type": "opening",
  "rate_status": "fallback", "rate_source": "fx_rates.csv row 8" }
```

Reviewer decisions are part of the lineage too. The same file's 7310 line includes:

```json
{ "source": "system", "entry": "SYS-FX", "account": "7310", "approved_by": "Demo Controller",
  "reason": "USD functional currency: EUR/GBP cash are monetary items, ...", "usd": "-181900.30" }
```

The auditor's tool reads the published output and never re-runs anything:

```
$ python src/trace.py 1110
Balance Sheet 2024-Q4 [BLOCKED, run dab528f778f6]
Assets > Current Assets
1110 Cash and Cash Equivalents = 5,669,600.30   [+(sum of debit-positive USD below)]
  ├─     4,250,000.00  trial_balance.csv row 2 (account 1110): USD 4,250,000.00
  ├─       903,813.00  trial_balance.csv row 3 (account 1110): EUR 825,400.00 x 1.095 [period_end, exact; fx_rates.csv row 3]
  └─       515,787.30  trial_balance.csv row 4 (account 1110): GBP 412,300.00 x 1.251 [opening, fallback; fx_rates.csv row 8]
```

Each trace states three things:
- which file and row each piece came from;
- which rate converted it, and where that rate came from;
- whether anything was estimated or approved, and by whom.

`LINEAGE_COMPLETE` and `LINEAGE_TIES` guarantee the trail is whole: no row is
missing, double-counted or unexplained. The `run_id` and input hashes in
`run_manifest.json` pin the trail to the exact file versions.

**In production**, lineage moves to an append-only store (tables keyed by
`run_id`, cell and component) that is queryable both ways:
- "everything that fed this cell";
- "every cell this row feeds", which is what an auditor asks after finding a bad source row.

---

## 8. Idempotency, versioning and human override

- **Pure function:** `(inputs, review file, code) → outputs`.
  - No timestamps, randomness or absolute paths in any output.
  - `run_id` = SHA-256 of the input hashes, review-file hash, source-code hashes and LLM model id.
  - Byte-identical replays are tested (`test_runs_are_byte_identical`) and enforced in CI, which re-runs the pipeline and fails on any diff in `output/`.
  - The Docker image (Python 3.12) produces exactly the same bytes as Python 3.9 locally.
- **LLM non-determinism is contained:** suggestions never move a number, so every statement amount is identical with or without `--llm`. The run id differs, because the model id is part of the fingerprint. Only the suggestion text in `mappings.json` can vary, and it is labelled with the model id.
- **What a reviewer can do:**
  - map an account (the target must be a postable account);
  - reject any JE;
  - approve a proposed system entry (the amount must match);
  - sign off a REVIEW item (the key must match).
- **What a reviewer cannot do:**
  - force-post an unbalanced or circular entry;
  - sign off a BLOCK;
  - type a number into a statement.

  Every decision is attributed (`approved_by`), carries a reason, and appears in lineage.
- **In production:**
  - review files become records in a review service: signed, immutable, versioned with the run;
  - preparer ≠ approver, with four eyes above materiality;
  - publication is a separate, permissioned step that accepts only CLEAN or signed-off REVIEW runs.

---

## 9. Cash Flow Statement and SOCIE (designed, not built)

### Cash Flow (indirect method)
1. **Start from net income** (from the P&L the prototype already produces).
2. **Remove non-cash items:**
   - depreciation 6500, amortisation 6510, bad-debt expense 6600 (through the AR allowance);
   - unrealised FX 7310;
   - gain on disposal 7400 (reclassified to investing);
   - deferred tax 8200 (through 1250 and 2230).
3. **Working capital:** the change in every BS account with `cf_category = Operating`, current vs prior.
4. **Investing** (`Investing` accounts) and **Financing** (`Financing` accounts, plus dividends from the RE roll-forward).
5. **FX effect on cash** as its own line: translating EUR and GBP cash at closing vs opening rates.
6. **BLOCK check:** opening cash + net cash flow = closing cash (1110), lineage per line as for the BS.

**Blocked on this data** by:
- `TBD` categories for 1150 and 2170 (Q11);
- a prior TB with no P&L detail that is out of balance by 2,832,800.00 (Q3);
- PPE movements that need the fixed-asset register (additions vs disposals), since the TB only gives net balances.

### Statement of Changes in Equity
- **Columns:** 3100, 3110, 3200, 3310 (OCI reserve), 3400.
- **Rows:** opening (= prior closing), restatements, net income, OCI, dividends, share transactions, closing.
- **BLOCK check:** closing = BS equity, by column.

**Blocked on this data** by:
- `RE_ROLLFORWARD`: opening RE doesn't roll from the prior TB;
- 3310 moved 95,000 → 180,000 with no OCI source;
- the gap needs dividends or a restatement explained (Q3).

The pipeline already detects all three. A SOCIE built anyway would be a plausible-looking fabrication.

---

## 10. Multi-entity, intercompany, restatements, COA versions

- **Intercompany elimination is matching, not netting.** For each pair of entities and currency:
  - match IC receivables to payables, and IC revenue to expense, within a tolerance;
  - age the breaks;
  - route unmatched items to both entity controllers.

  Elimination entries are system entries with lineage, like `SYS-FX`. JE-008 is the degenerate case, an entry with no counterparty.
- **Consolidation FX:** translate each subsidiary from its functional currency (BS at closing, P&L at average, equity at historical), with the difference to 3310 CTA. The prototype's `SYS-FX` choice between 7310 and 3310 is the single-entity version of this decision.
- **Restatements:** a separate, versioned input layer of prior-period adjustments. Comparatives are re-run from it, never edited, and the diff between runs is the restatement disclosure.
- **COA versions:** the mapping becomes a versioned table with effective dates. 6905 → 6900 should be a recorded alias, approved once, not a fuzzy match every run. Approved reviewer mappings become that table, and the training and evaluation set for the LLM mapper.

---

## 11. Production shape

- **Job:** one container run per entity and period. Inputs are immutable objects keyed by hash. Outputs and the manifest are stored per `run_id`.
- **Exit codes** let a scheduler distinguish *done* (0), *needs humans* (2) and *broken input* (1).
- **Review service + UI:** renders `review_packet.md` content as a queue, captures decisions as signed records, and triggers the re-run.
- **Observability:** findings are metrics (BLOCK rate, mean time-to-clear, top check ids), and LLM suggestion acceptance rate is tracked against reviewer decisions.
- **Security:**
  - the image runs as a non-root user;
  - the API key comes only from the environment;
  - the LLM sees one account plus the COA, never the full ledger;
  - the Investigator's tools are read-only.

---

## 12. What the prototype implements

| Component | Status |
|---|---|
| Strict ingest, `Decimal` money, fail-fast input errors | ✅ `src/pipeline.py` |
| Mapper: exact / name-drift / suggestion / orphan; reviewer overrides | ✅ |
| LLM mapping suggestions (enum-constrained, never applied, fake-client tests) | ✅ `src/llm.py`, opt-in `--llm` |
| FX: period-end/average by statement, fallback with sensitivity, missing → BLOCK | ✅ |
| JE validator: balance, line shape, duplicates, period, circular, unknown account, FX overlap | ✅ |
| Diagnosis + proposed entries + approvals bound to amounts | ✅ |
| BS + P&L with subtotals, close to RE, suspense line | ✅ |
| Verifier: 17 checks, sign-offs bound to finding keys, CLEAN/REVIEW/BLOCKED, exit codes | ✅ |
| Lineage per line + `trace.py` (lines and group totals) | ✅ `src/trace.py` |
| Run manifest, byte-identical replays, CI check | ✅ |
| Tests (21), Docker image gated on tests | ✅ `tests/`, `Dockerfile` |
| Reporter (LLM narrative), Investigator (agent) | Designed (§3) |
| Cash flow, SOCIE, multi-entity, IC, restatements | Designed (§9, §10) |

Run it: see `README.md`. Questions and assumptions: `docs/questions.md`.
