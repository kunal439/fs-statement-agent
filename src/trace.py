"""
Auditor trace: show where a statement figure comes from, down to source rows.

    python src/trace.py 1110                          # a line: Cash, from output/
    python src/trace.py 1100                          # a group total: Current Assets, line by line
    python src/trace.py 3200 --output output/reviewed  # Retained earnings, reviewed run

Reads the published statements only; it never re-runs the pipeline.
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def money(x) -> str:
    return f"{Decimal(x):,.2f}"


def describe(c: dict) -> str:
    if c["source"] == "system":
        who = f", approved by {c['approved_by']}" if c.get("approved_by") else ""
        return f"system entry {c['entry']}{who}: {c['memo']}"
    if "je" in c:
        return f"{c['source']} {c['je']} line {c['line']} (account {c['account']}): {c['memo']}"
    text = f"{c['source']} row {c['row']} (account {c['account']}): {c['currency']} {money(c['amount'])}"
    if c.get("rate"):
        text += f" x {c['rate']} [{c['rate_type']}, {c['rate_status']}; {c['rate_source']}]"
    return text


def print_line(line: dict, indent: str = "") -> None:
    sign = "+" if line["presentation"] == "debit_positive" else "-"
    print(f"{indent}{line['code']} {line['name']} = {money(line['amount'])}   [{sign}(sum of debit-positive USD below)]")
    for n, comp in enumerate(line["lineage"], 1):
        branch = "└─" if n == len(line["lineage"]) else "├─"
        print(f"{indent}  {branch} {Decimal(comp['usd']):>16,.2f}  {describe(comp)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trace a statement line or group total back to its source rows.")
    ap.add_argument("code", help="account code of a line (e.g. 1110) or of a group/section (e.g. 1100, 1000)")
    ap.add_argument("--output", type=Path, default=ROOT / "output", help="run output directory")
    args = ap.parse_args(argv)

    for name in ("balance_sheet.json", "profit_and_loss.json"):
        st = json.loads((args.output / name).read_text(encoding="utf-8"))
        header = f"{st['statement']} {st['period']} [{st['status']}, run {st['run_id']}]"
        for section in st["sections"]:
            groups = section["groups"] if section["code"] == args.code else \
                [g for g in section["groups"] if g["code"] == args.code]
            if groups:
                node = section if section["code"] == args.code else groups[0]
                print(f"{header}\n{node['name']} ({args.code}) total = {money(node['total'])} {st['currency']}"
                      f" = sum of the lines below")
                for g in groups:
                    for line in g["lines"]:
                        print_line(line, "  ")
                return 0
            for g in section["groups"]:
                for line in g["lines"]:
                    if line["code"] == args.code:
                        print(f"{header}\n{section['name']} > {g['name']}")
                        print_line(line)
                        return 0
    print(f"No statement line or group {args.code} in {args.output}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
