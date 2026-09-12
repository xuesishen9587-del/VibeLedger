import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.services import balance_capture,statement_import


class BalanceTotalReviewTest(unittest.TestCase):
    def draft(self,**total):
        return {"rows":[{"row_id":"cash","selected":True,"account_id":"account","expected_account_version":0,
            "time_basis":"explicit","balance":"100.00","currency":"CNY","display_unit":"0.01"}],
            "totals":[{"amount":"100.00","currency":"CNY","scope":"complete","covered_row_ids":["cash"],
                "explicitly_rounded":False,"display_unit":"0.01",**total}],"acknowledge_evidence":False}

    def validate(self,draft):
        with patch.object(balance_capture.balances,"validate"):
            return balance_capture.validate(None,SimpleNamespace(household_id="household"),draft)

    def test_unknown_component_requires_review_instead_of_silent_noncomparison(self):
        draft=self.draft(covered_row_ids=["cash","missing"])
        result,blocked=self.validate(draft)
        self.assertTrue(blocked)
        self.assertEqual(result["warnings"][0]["code"],"TOTAL_REQUIRES_REVIEW")
        draft["acknowledge_evidence"]=True
        self.assertFalse(self.validate(draft)[1])

    def test_explicit_exclusion_is_not_a_missing_extracted_component(self):
        draft=self.draft(covered_row_ids=["cash","excluded"])
        draft["rows"].append({"row_id":"excluded","selected":False})
        result,blocked=self.validate(draft)
        self.assertFalse(blocked)
        self.assertEqual(result["warnings"][0]["code"],"TOTAL_NOT_COMPARABLE")

    def test_uncertain_scope_does_not_become_safe_when_a_row_is_excluded(self):
        draft=self.draft(scope="uncertain",covered_row_ids=["cash","excluded"])
        draft["rows"].append({"row_id":"excluded","selected":False})
        self.assertTrue(self.validate(draft)[1])

    def test_only_explicit_rounding_allows_the_boundary_difference(self):
        self.assertTrue(self.validate(self.draft(amount="100.01"))[1])
        result,blocked=self.validate(self.draft(amount="100.01",explicitly_rounded=True))
        self.assertFalse(blocked)
        self.assertEqual(result["warnings"][0]["code"],"DISPLAY_ROUNDING")
        self.assertTrue(self.validate(self.draft(amount="100.02",explicitly_rounded=True))[1])


class StatementLinkReviewTest(unittest.TestCase):
    def test_voided_target_never_passes_link_validation(self):
        actor=SimpleNamespace(household_id="household")
        draft={"identity_ok":True,"partial":False,"period_start":"2026-02-01","period_end":"2026-02-28",
            "lines":[{"row_id":"row","action":"link_existing","transaction_id":"voided","expected_transaction_version":1}]}
        with patch.object(statement_import,"account_for_import",return_value={"id":"account"}), \
             patch.object(statement_import.schema,"get_household",return_value={"timezone":"Asia/Singapore"}), \
             patch.object(statement_import,"provider_matches",return_value=[]), \
             patch.object(statement_import.spending,"require_record",return_value={"status":"voided"}):
            result,blocked=statement_import.validate(None,actor,{"statement_account_id":"account"},draft)
        self.assertTrue(blocked)
        self.assertEqual(result["warnings"][0]["code"],"IMPORT_CHANGED")
