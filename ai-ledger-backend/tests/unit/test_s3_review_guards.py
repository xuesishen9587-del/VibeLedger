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


class StatementFactReviewTest(unittest.TestCase):
    def validate(self, **fields):
        actor=SimpleNamespace(household_id="household")
        line={"row_id":"row","action":"create","original_amount":"12.00","original_currency":"CNY",
            "occurred_on":"2026-02-03","transaction_type":"expense","category_id":"food",
            "requires_review":True,**fields}
        draft={"identity_ok":True,"partial":False,"period_start":"2026-02-01","period_end":"2026-02-28","lines":[line]}
        with patch.object(statement_import,"account_for_import",return_value={"id":"account"}), \
             patch.object(statement_import.schema,"get_household",return_value={"timezone":"Asia/Singapore"}), \
             patch.object(statement_import,"provider_matches",return_value=[]), \
             patch.object(statement_import.schema,"get_category",return_value={"status":"active","category_type":"expense"}), \
             patch.object(statement_import.repo,"rows",side_effect=lambda conn,query,args: [{"id":"row","extracted_payload":{}}] if "statement_lines" in query else []):
            return statement_import.validate(None,actor,{"id":"receipt","statement_account_id":"account"},draft)

    def test_valid_uncertain_facts_clear_after_acknowledgement(self):
        result,blocked=self.validate()
        self.assertTrue(blocked)
        self.assertEqual([w["code"] for w in result["warnings"]],["STATEMENT_LINE_UNCERTAIN"])
        self.assertFalse(self.validate(requires_review=False)[1])

    def test_invalid_facts_are_specific_and_never_acknowledgement_only(self):
        for fields,code in [({"category_id":None},"INVALID_CATEGORY"),
                            ({"transaction_type":None},"INVALID_TRANSACTION_TYPE"),
                            ({"original_amount":"0"},"INVALID_AMOUNT")]:
            for acknowledged in (False,True):
                with self.subTest(fields=fields,acknowledged=acknowledged):
                    result,blocked=self.validate(**fields,requires_review=not acknowledged)
                    self.assertTrue(blocked)
                    self.assertEqual(result["warnings"][0]["code"],code)


class StatementLinkReviewTest(unittest.TestCase):
    def test_voided_target_never_passes_link_validation(self):
        actor=SimpleNamespace(household_id="household")
        draft={"identity_ok":True,"partial":False,"period_start":"2026-02-01","period_end":"2026-02-28",
            "lines":[{"row_id":"row","action":"link_existing","transaction_id":"voided","expected_transaction_version":1}]}
        with patch.object(statement_import,"account_for_import",return_value={"id":"account"}), \
             patch.object(statement_import.schema,"get_household",return_value={"timezone":"Asia/Singapore"}), \
             patch.object(statement_import,"provider_matches",return_value=[]), \
             patch.object(statement_import.spending,"require_record",return_value={"status":"voided"}), \
             patch.object(statement_import.repo,"rows",return_value=[{"id":"row","extracted_payload":{}}]):
            result,blocked=statement_import.validate(None,actor,{"id":"receipt","statement_account_id":"account"},draft)
        self.assertTrue(blocked)
        self.assertEqual(result["warnings"][0]["code"],"IMPORT_CHANGED")
