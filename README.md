# FS Statement Agent

Agentic pipeline that ingests an ERP trial balance, translates non-functional
currencies to USD, maps accounts to a chart of accounts with confidence
scoring, validates manual journal entries, and produces a Balance Sheet plus
a P&L with self-verification.

**Period:** 2024-Q4 · **Functional currency:** USD

## What it does

Given a trial balance, chart of accounts, FX rates, and a batch of manual
adjustments, the pipeline:

1. **Translates** non-USD rows using a rate resolution hierarchy (exact →
   fallback → flag → hard fail)
2. **Maps** every TB account to the COA using a Mapper agent that returns
   confidence scores and reasoning, and flags orphan accounts for review
3. **Validates** every manual JE for balance, existence, economic substance
   (circular-entry detection), and sign convention. Rejected JEs are
   quarantined with plain-English reasons.
4. **Builds** a Balance Sheet and P&L from the closed books
5. **Verifies** the results with an Auditor checklist (A = L + E, P&L
   identity, TB balanced) and reports failures with suggested actions

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac/Linux
pip install -r requirements.txt
