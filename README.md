# FS Statement Agent

Agentic pipeline that maps an ERP trial balance to a chart of accounts,
validates manual adjustments, and generates a Balance Sheet + P&L
with self-verification.

## Setup

    python -m venv venv
    venv\Scripts\activate     (Windows)
    source venv/bin/activate  (Mac/Linux)
    pip install -r requirements.txt

## Run

    python src/pipeline.py

## Outputs

See `output/` for generated statements, mappings, rejected adjustments,
and lineage artifacts.

## Documentation

- Architecture → `docs/architecture.md`
- Defect inventory → `docs/defects.md`
- Reflection → `docs/reflection.md`