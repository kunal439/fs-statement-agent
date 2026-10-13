# Architecture — Agentic Financial Statement Generator

**Period:** 2024-Q4 · **Functional currency:** USD · **Entity:** fictional NetSuite-style export

---

## 1. Design Philosophy

Financial statement generation is **90% deterministic arithmetic and 10% judgment calls**. The 90% is where reliability, auditability, and reproducibility live. The 10% is where an LLM adds real value — mapping ambiguous accounts, explaining rejections in language a finance user can act on, and detecting semantic errors (like a circular journal entry) that pure arithmetic cannot see.

The design therefore splits the system into two layers:

- A **deterministic core** that ingests, translates, aggregates, and builds statements as pure functions with stable contracts.
- Four **agent checkpoints** placed only at genuine decision points, each with a typed input/output schema and a deterministic fallback rule if the agent fails or returns low confidence.

This rejects both common failure modes of "AI for finance":

- The **"just prompt a frontier model with the trial balance"** approach — hallucinated mappings, no lineage, non-reproducible output, unusable for audit.
- The **"12-agent swarm"** approach — over-engineering; most steps are arithmetic and become unstable when wrapped in agents.

I built the Mapper and Adjuster agents end-to-end in the prototype. The Statement Agent stayed on paper — the assignment asked for one slice, not four, and mapping and validation is where the mess actually is.

## 2. Agent Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│  DETERMINISTIC CORE — pure functions, unit-tested, idempotent       │
│                                                                     │
│  Ingest → Normalize → FX Translate → Map to COA → Post Adjustments  │
│  → Aggregate → Build Statements → Reconcile                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
        ▲              ▲               ▲              ▲
        │              │               │              │
   ┌────┴────┐   ┌─────┴─────┐   ┌─────┴─────┐   ┌────┴─────┐
   │ MAPPER  │   │  POSTER   │   │ REPORTER  │   │ AUDITOR  │
   │  Agent  │   │   Agent   │   │   Agent   │   │  Agent   │
   └─────────┘   └───────────┘   └───────────┘   └──────────┘
   Handles:      Handles:        Handles:        Handles:
   - unmapped    - debit≠credit  - narrative     - cross-checks
   - ambiguous   - orphan accts  - anomaly       - self-correction
   - confidence  - circular IC     flagging      - escalation
```

### Why four agents and not one or twelve

Each agent sits at a decision point where **arithmetic is insufficient**:

- **Mapper** — decides which COA account a TB row belongs to when the code or name does not match exactly. This is a semantic problem.
- **Poster** — validates each manual journal entry for balance, existence, economic substance, and sign convention. Arithmetic catches imbalance; judgment catches a circular entry that technically balances.
- **Reporter** — writes the narrative around anomalies (for example, "Cash increased 12% due to a EUR revaluation uplift that was partially offset by operating outflows"). Language generation over structured data.
- **Auditor** — runs the checklist after every stage and, when a check fails, diagnoses the cause and decides whether to auto-correct, escalate, or halt.

Between these checkpoints, everything is a pure function. Load a CSV: pure. Apply an FX rate: pure. Sum debits: pure. Build a balance sheet: pure. None of these belong in an agent.

## 3. Where LLMs Earn Their Keep

| Task | LLM? | Why |
|------|------|-----|
| Sum debits/credits per account | No | Arithmetic. Pure function. |
| FX translation using a rate table | No | Deterministic lookup and multiply. |
| Exact-match COA lookup | No | Hash table. |
| Fuzzy COA match (for example, `6905` to `6900` rename) | **Yes** | Semantic similarity plus reasoning about account type. |
| Detecting a circular journal entry | **Yes** | Requires understanding economic substance, not just balance. |
| Explaining a JE rejection to a finance user | **Yes** | Natural language generation over structured error. |
| Deciding if an unmapped account is material | **Yes** | Context-dependent judgment against a materiality threshold. |
| Building the balance sheet | No | Arithmetic. |
| Writing the audit narrative | **Yes** | Summarization over lineage graph. |
| Validating A = L + E | No | Arithmetic. |
| Diagnosing *why* validation failed | **Yes** | Root-cause reasoning across multiple exception types. |
| Deciding to auto-correct versus halt | **Yes** | Requires estimating the blast radius of an incorrect auto-fix. |

**Rule of thumb:** if a competent junior accountant with a calculator and a lookup table could do it, it is deterministic code. If it requires judgment, domain knowledge, or natural language, it is an agent.

## 4. Failure Modes and Handling

The five failure modes below are not hypothetical. Each one is *seeded in the provided mock data* and handled explicitly by the design. Where the defect has an ID from `docs/defects.md`, it is referenced directly.

### 4.1 Hallucinated account mappings (defects TB-3, X-1)

**Symptom:** The TB contains account `9999 Suspense - Unmapped`, which has no COA match. The prior period has `6905 Sundry Operating Expenses`, which has been renamed to `6900 Other Operating Expenses` in the current COA.

**Mitigation:** The Mapper never returns a bare mapping. It returns a structured object:

```json
{
  "account_code": "6905",
  "suggested_coa_code": "6900",
  "confidence": 0.87,
  "reasoning": "Prior period 'Sundry Operating Expenses' renamed to 'Other Operating Expenses' in current COA; both are catch-all operating expense accounts under 6000.",
  "alternatives": [{"code": "6900", "score": 0.87}, {"code": "6400", "score": 0.12}],
  "requires_human": false
}
```

If `confidence < 0.85` **or** the account is material (over 1% of total assets), `requires_human` is set to `true` and the pipeline halts with an escalation packet. No silent mappings.

### 4.2 Debits not equal to credits after adjustments (defect JE-1)

**Symptom:** `JE-002` reclassifies marketing spend wrongly booked to T&E. It debits `6300` for 28,500 but only credits `6310` for 25,000 — an imbalance of 3,500.

**Mitigation:** The Poster validates every JE before posting:

- Sum of debits equals sum of credits (tolerance 0.01)
- Every referenced account exists in the COA
- No circular entries (same account on both sides with net zero)
- Sign conventions match normal balance (with notes for legitimate contra entries)

Rejected JEs go to a quarantine queue with a plain-English explanation of the exact imbalance. The pipeline continues with accepted JEs only, and the final statements carry a "N adjustments quarantined" note.

### 4.3 Account fits no COA node (defect TB-3)

**Mitigation:** Three-tier escalation:

1. **Auto-map** if confidence is 0.85 or higher and account name similarity is 0.9 or higher
2. **Suggest and flag** if confidence is between 0.5 and 0.85 — routed to `review_queue.json`
3. **Quarantine** if confidence is below 0.5 — excluded from statements, surfaced in `unmapped_accounts.json` with materiality impact

The `9999 Suspense` account is a special case: it maps to a "Suspense" pseudo-node that appears on the Balance Sheet as an explicit reconciling item, never silently dropped.

### 4.4 FX gaps (defect FX-1)

**Symptom:** `fx_rates.csv` provides `period_average` and `opening` rates for GBP but **no period-end rate**. GBP cash cannot be revalued at period-end.

**Mitigation:** The FX Translator uses a rate resolution hierarchy:

1. **Exact match** — currency + rate_type + period
2. **Type fallback** — for `period_end`, use `opening` if `period_end` is missing; flag
3. **Any rate fallback** — use the most recent available rate for that currency; flag
4. **Hard fail** — if no rate is found and the position is material, halt with an `FX_GAP` error

For the GBP cash row, the translator uses the opening rate 1.251, marks the translated amount as `estimated: true` in lineage, and the Auditor evaluates whether the potential swing exceeds materiality. If it does, the pipeline escalates rather than guessing.

### 4.5 Circular intercompany entries (defect JE-3)

**Symptom:** `JE-008` debits `2170 Intercompany Payable` for 320,000 and credits the same account for 320,000. The entry technically balances but has zero economic substance.

**Mitigation:** The Poster detects:

- Same account on both debit and credit sides with net zero
- IC pairs where both legs are to the same entity
- Entries with no net effect on any account

`JE-008` is flagged as **circular** and rejected with the explanation: *"Entry debits and credits the same account (2170) for the same amount. Net effect is zero. Appears to be a placeholder for an IC settlement that was not completed."*

The reason this matters: pure arithmetic says this JE is fine (debits equal credits). Only semantic understanding catches that it is meaningless.

## 5. Validation and Self-Correction Loop

The Auditor runs a checklist after every stage in the pipeline. Some checks are structural (does every account map to a COA node?), some are accounting identities (does A = L + E?), and some guard the traceability contract (does every cell have lineage?).

```python
CHECKS = [
    # Structural
    ("TB debits == TB credits", check_tb_balanced),
    ("Every TB account maps to COA", check_all_mapped),
    ("No orphan accounts in statements", check_no_orphans),
    # Accounting identities
    ("Assets == Liabilities + Equity", check_balance_sheet_identity),
    ("Net Income == Revenue - Expenses", check_pl_identity),
    ("Opening + Net Income + OCI == Closing Equity", check_socie),
    ("Cash flow ties to cash balance change", check_cf_reconciliation),
    # Adjustment integrity
    ("Every posted JE balances", check_je_balanced),
    ("No circular JEs posted", check_no_circular),
    # FX
    ("All material FX positions translated", check_fx_complete),
    # Traceability
    ("Every statement cell has lineage", check_lineage_complete),
]
```

When a check fails, the Auditor emits a structured error rather than a generic message:

```json
{
  "check": "TB debits == TB credits",
  "status": "FAIL",
  "expected": 68337300.00,
  "actual": 67337000.00,
  "delta": 1000300.00,
  "affected_accounts": ["1110", "9999"],
  "severity": "HIGH",
  "suggested_action": "TB is out of balance by approximately 1M USD-equivalent. Check FX translation of EUR and GBP rows and confirm that orphan account 9999 belongs in the COA."
}
```

The suggested action drives what happens next:

1. If the fix is arithmetic (recompute a sum, re-apply a rate), the pipeline self-corrects and re-runs that stage.
2. If the fix requires judgment (map an account, decide materiality), the Auditor routes to the relevant agent.
3. If neither can resolve it, the pipeline halts with a human-readable exception report.

Retries are bounded: max 3 attempts per check, then hard fail. No infinite loops, no silent fixes.

The reason this matters in production is that a broken financial statement is worse than no financial statement. If the pipeline cannot prove A = L + E, it should refuse to emit the balance sheet rather than produce a plausible-looking wrong one.

## 6. Traceability — Lineage as a DAG

Every number on every statement carries a `lineage_id` that resolves to a directed acyclic graph of source rows and transformations. An auditor does not have to trust the pipeline — they can walk the graph.

```
Statement Cell: BS Cash = 5,487,613
  └── Aggregation: sum(translated cash accounts)
       ├── TB row 2: 1110 USD 4,250,000
       │    └── Source: trial_balance.csv row 2
       ├── TB row 3: 1110 EUR 825,400
       │    ├── Source: trial_balance.csv row 3
       │    └── FX: EUR period_end 1.095 → 903,813
       │         └── Source: fx_rates.csv row 5
       └── TB row 4: 1110 GBP 412,300
            ├── Source: trial_balance.csv row 4
            └── FX: GBP opening 1.251 (FALLBACK — no period_end rate)
                 └── Source: fx_rates.csv row 8 [ESTIMATED]
```

The auditor workflow: click any cell, see the full DAG, drill to the source row, see every transformation applied, verify each rate and each mapping independently.

Lineage is stored as JSON alongside the statements in `output/`. It is not a log file — it is structured data the finance team can query ("show me everything that fed the Cash line for 2024-Q4").

The reason this matters: without lineage, an AI-assisted reporting tool is unusable in finance. When an auditor asks "why is cash 5,487,613 and not 5,000,000?", the answer needs to be a drillable trail, not a model's confidence score.

## 7. Idempotency and Replay

The pipeline is a pure function:

```
(TB, COA, Adjustments, FX rates, Config) → (Statements, Lineage, Exceptions)
```

Given the same inputs, it produces byte-identical outputs. No hidden state, no wall-clock timestamps in the output beyond what is declared, no randomness anywhere.

Every run is versioned with a hash of its inputs. Re-running with one changed adjustment produces a diff report:

> "Cash changed by +11,200 due to JE-003 FX reval."

This property matters for three reasons:

1. **CI safety** — the pipeline can run in a build system without side effects.
2. **Reproducibility** — an auditor can re-run the same quarter and get the same numbers.
3. **Diffing** — when a restatement is posted, the delta between runs is itself a report. The finance team can see exactly which line items moved and why.

The prototype is idempotent by construction — all inputs are read-only, all outputs are written to `output/`, and no global state is mutated between stages.

## 8. Human-in-the-Loop Escalation

The pipeline has three exit states. This is the interface with the finance user — the only thing they see.

| State | When | Output |
|-------|------|--------|
| **CLEAN** | All checks pass, no escalations | Statements delivered |
| **REVIEW** | Non-blocking issues (low-confidence mappings, estimated FX, quarantined JEs) | Statements delivered with `review_packet.json` listing every item needing sign-off |
| **BLOCKED** | Material unresolved issue (unbalanced TB after adjustments, missing FX for material position, orphan account above materiality) | No statements delivered; `exception_report.json` explains exactly what is needed |

The finance user sees a single call to action: *"3 items require your review before this can be published."* They do not see pipeline internals or agent traces — they see a short list of decisions.

Three properties matter here:

- **Escalation is explicit, not implicit.** Nothing is silently absorbed. A quarantined JE appears on the review packet every time, not just the first time.
- **Materiality is configurable, not hard-coded.** The threshold defaults to 1% of total assets but can be overridden per entity.
- **BLOCKED is a first-class outcome.** The pipeline is designed to refuse output when the numbers cannot be trusted, not to approximate them.

The reason this matters: in finance software, a silent failure is worse than a loud one. A tool that quietly guesses is unusable. A tool that says "I do not know, here is what I need from you" is trustworthy.

## 9. What the Prototype Implements

The prototype in `src/pipeline.py` implements the deterministic core plus three of the four agent checkpoints end-to-end against the provided mock data.

**Implemented:**

- **FX translation** with the rate resolution hierarchy (handles defect FX-1 — missing GBP period-end rate)
- **Mapper** with confidence scoring and orphan detection (handles defects TB-3 and X-1 — accounts `9999` and `6905`)
- **Poster** with the four-check validation (handles defects JE-1, JE-2, and JE-3 — imbalance, missing account, circular entry)
- **Balance Sheet and P&L builders** with the Auditor checklist (surfaces TB-1 — the unbalanced trial balance — as a check failure with a suggested action)

**Designed but not coded:**

- **Reporter** (narrative generation around anomalies) — deliberate scope cut. The assignment asked for one slice, and mapping plus validation is where the messy-data problem actually lives. The reflection explains this tradeoff.

The prototype writes five artifacts to `output/`:

- `mappings.json` — every TB account mapped to COA with confidence and reasoning
- `rejected_adjustments.json` — JEs rejected with plain-English reasons
- `quarantined_adjustments.json` — JEs held for human review
- `validation_findings.json` — checklist results with suggested actions
- `lineage.json` — the traceability graph for every statement cell

Run it with `python src/pipeline.py`. See `README.md` for setup instructions.