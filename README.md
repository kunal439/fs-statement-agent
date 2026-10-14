# FS Statement Agent

Builds a **Balance Sheet and P&L** from a messy ERP trial balance, chart of
accounts, FX rates and manual journal entries, and **verifies its own output
before anything is issued**. Every statement line traces back to source rows.
Problems are diagnosed exactly and turned into proposed fixes. Nothing is
plugged without a human decision.

Prototype slice from the take-home brief: *statement generator for P&L +
Balance Sheet with a verification agent that catches its own errors before
output*. Mapping and journal-entry validation are built only as deep as that
slice needs.

| Deliverable | Where |
|---|---|
| Architecture document (4.1) | [`docs/architecture.md`](docs/architecture.md) |
| Working prototype (4.2) | `src/`, `tests/`, outputs in [`output/`](output/) |
| One-page reflection (4.3), incl. AI tool usage | [`docs/reflection.md`](docs/reflection.md) |
| Clarifying questions and assumptions | [`docs/questions.md`](docs/questions.md) |
| Defect inventory (26 found) | [`docs/defects.md`](docs/defects.md) |

## Quick start with Docker

```bash
docker build -t fs-statement-agent .            # also runs the test suite; the build fails if a test fails

# 1) Raw run on the messy data. Expected: BLOCKED, exit code 2.
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/output:/app/output" fs-statement-agent

# 2) Re-run with the reviewer's decisions. Expected: REVIEW (balances to 0.00, 5 items await sign-off), exit 0.
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/output:/app/output" \
  fs-statement-agent --review reviews/2024-Q4.json --output output/reviewed

# 3) Trace a line (1110 Cash) or a group total (1100 Current Assets) back to source rows
docker run --rm -v "$PWD/output:/app/output" --entrypoint python fs-statement-agent src/trace.py 1110
```

`--user` keeps the output files owned by you on Linux. Docker Desktop on
macOS and Windows doesn't need it. On Windows PowerShell, use `${PWD}`.

## Run locally (Python 3.9+, standard library only)

```bash
python src/pipeline.py                                                     # raw run -> output/
python src/pipeline.py --review reviews/2024-Q4.json --output output/reviewed
python src/trace.py 1110                                                   # a line; 1100 = a group total
python -m unittest discover -s tests -v                                    # 21 tests
```

### Optional: LLM mapping suggestions

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python src/pipeline.py --llm
# Docker: docker run --rm -e ANTHROPIC_API_KEY -v "$PWD/output:/app/output" fs-statement-agent --llm
```

Claude suggests a COA account for accounts with no exact match. Its answer is
constrained to valid COA codes (or `NONE`), and it **never changes a number**:
suggestions go to the review packet. Without a key, the pipeline uses its
deterministic mapper.

## What the two runs show

| | Raw run (`output/`) | Reviewed run (`output/reviewed/`) |
|---|---|---|
| Status | **BLOCKED**: drafts, `issuable: false`, exit 2 | **REVIEW**: balances; `issuable: false` until the open items are signed off |
| Balance sheet difference | 177,100.30 (decomposed exactly: −4,800.00 at source + 181,900.30 translation uplift) | **0.00** |
| Net income | 4,783,500.00 | 4,965,400.30 (includes the approved FX remeasurement) |
| Journal entries | 6 posted · JE-002, JE-008 rejected · JE-003, JE-005 held | Reviewer rejects JE-002/003/008; JE-005 still open |
| Proposed fixes | `SYS-FX` 181,900.30, `SYS-SUSPENSE` 4,800.00, awaiting approval | Both approved (bound to their exact amounts) |
| Items left for a human | 9 (2 blocking) | GBP estimate signed off; 5 open: JE-005, 9999 suspense, duplicate 6310 rows, prior TB, RE roll-forward |

Start with `output/review_packet.md`. It is the finance user's view: a short
list of decisions, the proposed entries, and every JE with a plain-English
reason.

## Outputs (per run)

| File | Contents |
|---|---|
| `review_packet.md` | Status, headline numbers, decisions needed, proposed entries, JE table |
| `balance_sheet.json` / `profit_and_loss.json` | Statements grouped by COA, with per-line lineage (source row, FX rate and source, JE line, approval) |
| `validation_findings.json` | Every check (16, plus `REVIEW_FILE` with a review file): PASS/FAIL, severity, evidence, action, sign-off key |
| `adjustments.json` | Every JE with status and reasons |
| `mappings.json` | Every account code seen in any input: how it mapped, suggestions, alternatives |
| `run_manifest.json` | Run id, SHA-256 of every input, review file and source file, config, exit code |
| `run_log.txt` | Human-readable log of the run |

Money is `Decimal` and written as strings (`"903813.00"`). Re-running with the
same inputs produces byte-identical files; CI checks this.

**Exit codes:** `0` CLEAN or REVIEW · `2` BLOCKED · `1` invalid input (for example a thousands separator in a CSV amount). Only a CLEAN run (every check passed or signed off) is stamped `issuable: true`.

## Repository layout

```
src/pipeline.py       ingest -> map -> translate -> validate JEs -> post -> build -> verify -> publish
src/llm.py            optional Claude mapping suggestions (guardrailed, never applied)
src/trace.py          auditor trace of a statement line
reviews/2024-Q4.json  example reviewer decisions (mappings, JE rejections, approvals, sign-offs)
tests/                21 stdlib unittest tests, incl. one that re-introduces an old bug
input/                the provided mock data, unmodified
output/               committed results of both runs
Dockerfile            multi-stage: tests gate the build; runs as a non-root user
.github/workflows/    CI: tests, output reproducibility, docker build
```
