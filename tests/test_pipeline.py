"""Checks for every claim the docs make about the pipeline. Run: python -m unittest -v"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import pipeline as p  # noqa: E402

INPUT, REVIEW = ROOT / "input", ROOT / "reviews" / "2024-Q4.json"


def finding(result, fid):
    return next(f for f in result["findings"] if f["id"] == fid)


def line(statement, code):
    return next(ln for ln in p.all_lines(statement) if ln["code"] == code)


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        with mock.patch("builtins.print"):
            cls.raw = p.run(INPUT, cls.tmp / "raw")
            cls.reviewed = p.run(INPUT, cls.tmp / "reviewed", REVIEW)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_quiet(self, *args, **kwargs):
        with mock.patch("builtins.print"):
            return p.run(*args, **kwargs)

    # --- the seeded defects are caught and explained -------------------------------------
    def test_raw_run_blocks_with_fully_explained_imbalance(self):
        self.assertEqual(self.raw["status"], "BLOCKED")
        ev = finding(self.raw, "LEDGER_BALANCED")["evidence"]
        self.assertEqual((ev["delta"], ev["face_delta"], ev["translation_uplift"]),
                         (Decimal("177100.30"), Decimal("-4800.00"), Decimal("181900.30")))
        bs = finding(self.raw, "BS_IDENTITY")["evidence"]
        self.assertTrue(bs["explained_by_ledger"])
        self.assertEqual(bs["difference"], Decimal("177100.30"))

    def test_adjustment_statuses(self):
        statuses = {v["id"]: v["status"] for v in self.raw["validated"]}
        self.assertEqual(statuses, {
            "JE-001": "ACCEPTED", "JE-002": "REJECTED", "JE-003": "QUARANTINED", "JE-004": "ACCEPTED",
            "JE-005": "QUARANTINED", "JE-006": "ACCEPTED", "JE-007": "ACCEPTED", "JE-008": "REJECTED",
            "JE-009": "ACCEPTED", "JE-010": "ACCEPTED"})
        je2 = next(v for v in self.raw["validated"] if v["id"] == "JE-002")
        self.assertIn("out of balance by 3,500.00", je2["reasons"][0])

    def test_contra_accounts_reduce_their_section(self):
        for code, amount in {"1121": "-230000.00", "1211": "-4415000.00", "1221": "-980000.00", "3400": "-420000.00"}.items():
            self.assertEqual(line(self.raw["bs"], code)["amount"], Decimal(amount), code)
        self.assertEqual(line(self.raw["pl"], "4200")["amount"], Decimal("-340000.00"))

    def test_unmapped_account_is_suspense_not_dropped(self):
        self.assertEqual(line(self.raw["bs"], "9999")["amount"], Decimal("12400.00"))
        self.assertIsNone(self.raw["mappings"]["9999"]["suggestion"])  # no fake 9999 -> 7310 match

    def test_mapper_suggests_but_never_applies(self):
        m = self.raw["mappings"]["6905"]
        self.assertEqual((m["status"], m["coa_code"], m["suggestion"]["coa_code"]), ("SUGGESTED", None, "6900"))
        self.assertTrue(m["requires_human"])
        self.assertEqual(self.reviewed["mappings"]["6905"]["status"], "OVERRIDDEN")

    def test_gbp_fallback_rate_is_flagged_with_sensitivity(self):
        f = finding(self.raw, "FX_ESTIMATES")
        self.assertEqual((f["status"], f["severity"], f["evidence"]["swing"]), ("FAIL", "REVIEW", Decimal("5359.90")))

    # --- the review loop closes ---------------------------------------------------------
    def test_reviewed_run_balances_and_needs_only_sign_off(self):
        self.assertEqual(self.reviewed["status"], "REVIEW")
        self.assertEqual(self.reviewed["bs"]["totals"]["difference"], Decimal("0.00"))
        self.assertEqual(line(self.reviewed["bs"], "9999")["amount"], Decimal("17200.00"))
        blocking = [f["id"] for f in self.reviewed["findings"] if f["severity"] == "BLOCK"]
        self.assertEqual(blocking, [])
        self.assertEqual(finding(self.reviewed, "FX_ESTIMATES")["signed_off_by"], "Demo Controller")
        self.assertEqual([f["id"] for f in self.reviewed["findings"] if p.is_open(f)],
                         ["JE_EXCEPTIONS", "TB_ACCOUNTS_MAPPED", "TB_DUPLICATE_ROWS", "PRIOR_TB_BALANCED", "RE_ROLLFORWARD"])
        bs = json.loads((self.tmp / "reviewed" / "balance_sheet.json").read_text())
        self.assertFalse(bs["issuable"])  # REVIEW is not issuable until every item is signed off

    def test_stale_sign_off_reopens_the_item(self):
        doc = json.loads(REVIEW.read_text())
        doc["sign_offs"]["FX_ESTIMATES"]["fingerprint"] = "000000000000"
        stale = self.tmp / "stale_sign_off.json"
        stale.write_text(json.dumps(doc))
        result = self.run_quiet(INPUT, self.tmp / "stale_sign_off", stale)
        self.assertTrue(p.is_open(finding(result, "FX_ESTIMATES")))
        self.assertIn("FX_ESTIMATES", result["sign_off_notes"][0])

    def test_stale_approval_blocks(self):
        doc = json.loads(REVIEW.read_text())
        doc["approved_entries"]["SYS-FX"]["amount"] = "181000.00"
        stale = self.tmp / "stale.json"
        stale.write_text(json.dumps(doc))
        result = self.run_quiet(INPUT, self.tmp / "stale", stale)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(finding(result, "REVIEW_FILE")["status"], "FAIL")

    def test_review_file_cannot_map_to_a_header(self):
        doc = json.loads(REVIEW.read_text())
        doc["account_mappings"]["9999"] = {"coa_code": "1000", "reason": "x"}
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(doc))
        with self.assertRaises(p.InputError):
            self.run_quiet(INPUT, self.tmp / "bad", bad)
        doc["account_mappings"]["9999"] = {"coa_code": ["1110"], "reason": "x"}  # wrong type, same clean error
        bad.write_text(json.dumps(doc))
        with self.assertRaises(p.InputError):
            self.run_quiet(INPUT, self.tmp / "bad", bad)

    # --- traceability and the verifier ---------------------------------------------------
    def test_every_source_row_lands_in_exactly_one_line(self):
        for result in (self.raw, self.reviewed):
            for fid in ("LINEAGE_COMPLETE", "LINEAGE_TIES", "NI_TO_EQUITY"):
                self.assertEqual(finding(result, fid)["status"], "PASS", fid)
        rows = {c["row"] for c in line(self.raw["bs"], "1110")["lineage"]}
        self.assertEqual(rows, {2, 3, 4})

    def test_verifier_catches_a_sign_bug(self):
        """Re-introduce the original bug (sign keyed on normal_balance) and expect a block."""
        def buggy(acct):
            return 1 if acct is None or (acct.type == "Asset" and acct.normal_balance == "Debit") else -1
        with mock.patch.object(p, "presentation_sign", buggy):
            result = self.run_quiet(INPUT, self.tmp / "buggy", REVIEW)
        f = finding(result, "BS_IDENTITY")
        self.assertEqual((f["status"], f["evidence"]["explained_by_ledger"]), ("FAIL", False))
        self.assertEqual(finding(result, "LINEAGE_TIES")["status"], "FAIL")  # independent sign rule disagrees
        self.assertEqual(result["status"], "BLOCKED")

    def test_missing_rate_blocks_and_never_hides_the_row(self):
        inp = self.copy_input("no_gbp_rate")
        fx = inp / "fx_rates.csv"
        fx.write_text("".join(ln for ln in fx.read_text().splitlines(True) if not ln.startswith("GBP,opening")))
        result = self.run_quiet(inp, self.tmp / "no_gbp_out")
        self.assertEqual(result["status"], "BLOCKED")
        for fid in ("FX_RATES_AVAILABLE", "LINEAGE_COMPLETE"):
            self.assertEqual(finding(result, fid)["status"], "FAIL", fid)
        self.assertIn("row 4", finding(result, "LINEAGE_COMPLETE")["detail"])
        self.assertIn("no rate, held out", finding(result, "LEDGER_BALANCED")["detail"])

    # --- production properties -------------------------------------------------------------
    def test_runs_are_byte_identical(self):
        again = self.tmp / "again"
        self.run_quiet(INPUT, again)
        for f in sorted((self.tmp / "raw").iterdir()):
            self.assertEqual(f.read_bytes(), (again / f.name).read_bytes(), f.name)

    def copy_input(self, name):
        target = self.tmp / name
        shutil.copytree(INPUT, target)
        return target

    def test_malformed_input_fails_fast(self):
        bad = self.copy_input("bad_input")
        tb = bad / "trial_balance.csv"
        tb.write_text(tb.read_text().replace("4250000.00", "4,250,000"))
        with self.assertRaises(p.InputError):
            self.run_quiet(bad, self.tmp / "bad_out")

    def test_coa_parent_cycle_fails_fast(self):
        bad = self.copy_input("cycle_input")
        coa = bad / "chart_of_accounts.csv"
        coa.write_text(coa.read_text().replace("1100,Current Assets,Header,1000", "1100,Current Assets,Header,1110"))
        with self.assertRaises(p.InputError):
            self.run_quiet(bad, self.tmp / "cycle_out")

    def test_exit_codes(self):
        with mock.patch("builtins.print"):
            self.assertEqual(p.main(["--output", str(self.tmp / "cli_raw")]), 2)
            self.assertEqual(p.main(["--review", str(REVIEW), "--output", str(self.tmp / "cli_rev")]), 0)


@unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
class LLMGuardrailTest(unittest.TestCase):
    """The LLM path, with a fake client: no network, no key."""

    def suggester(self, answer):
        import llm

        def create(**kwargs):
            return SimpleNamespace(stop_reason="end_turn", model=llm.MODEL,
                                   content=[SimpleNamespace(type="text", text=json.dumps(answer))])
        return llm.make_suggester(SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))

    def run_with(self, answer, out):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.print"):
            return p.run(INPUT, Path(tmp) / out, llm=self.suggester(answer))

    def test_valid_suggestion_is_recorded_but_not_applied(self):
        result = self.run_with({"coa_code": "6310", "confidence": 0.9, "reasoning": "Conference travel is T&E."}, "ok")
        m = result["mappings"]["6315"]
        self.assertEqual((m["status"], m["coa_code"], m["suggestion"]["coa_code"]), ("SUGGESTED", None, "6310"))
        je5 = next(v for v in result["validated"] if v["id"] == "JE-005")
        self.assertEqual(je5["status"], "QUARANTINED")
        self.assertIn("no-op", " ".join(je5["reasons"]))  # mapping 6315 -> 6310 would cancel the entry

    def test_invented_code_is_discarded(self):
        result = self.run_with({"coa_code": "1234", "confidence": 0.99, "reasoning": "made up"}, "bad")
        self.assertIn("discarded", result["mappings"]["9999"]["llm_note"])

    def test_no_credentials_falls_back_instead_of_crashing(self):
        env = {k: v for k, v in __import__("os").environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", env, clear=True), \
                mock.patch("builtins.print"), mock.patch("sys.stderr"):
            self.assertEqual(p.main(["--llm", "--output", str(Path(tmp) / "out")]), 2)  # ran, BLOCKED as usual

    def test_none_answer_leaves_account_for_a_human(self):
        result = self.run_with({"coa_code": "NONE", "confidence": 0.9, "reasoning": "Suspense."}, "none")
        self.assertEqual(result["mappings"]["9999"]["status"], "ORPHAN")


if __name__ == "__main__":
    unittest.main()
