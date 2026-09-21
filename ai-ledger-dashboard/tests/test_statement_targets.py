import unittest
from unittest.mock import Mock
from api_client import ApiError
from statement_targets import TargetBrowser


class StatementTargetsTest(unittest.TestCase):
    def test_snapshot_lookup_uses_scoped_collection_and_reports_missing_target(self):
        client=Mock()
        client.request.side_effect=[{"items":[{"id":"older"}]},{"items":[]}]
        browser=TargetBrowser({"items":{}},client,"/api/v1/accounts/account/snapshots",lookup_parameter="snapshot_id")
        choices,missing=browser.choices(["older","voided"])
        self.assertEqual(choices,{"older":{"id":"older"}})
        self.assertEqual(missing,["voided"])
        self.assertEqual(client.request.call_args_list[0].args,("GET","/api/v1/accounts/account/snapshots"))
        self.assertEqual(client.request.call_args_list[0].kwargs["params"],{"snapshot_id":"older","limit":1})

    def test_later_pages_are_reachable_and_old_choices_are_retained(self):
        client=Mock()
        client.request.side_effect=[
            {"items":[{"id":str(i)} for i in range(page*50,(page+1)*50)],"next_cursor":str(page+1)} for page in range(4)] + [
            {"items":[{"id":"201","row_version":3}],"next_cursor":None}]
        browser=TargetBrowser({},client,"/api/v1/transactions")
        browser.load({"merchant":"Cafe"})
        for _ in range(4):
            browser.load({"merchant":"Cafe"},more=True)
        choices,_=browser.choices()
        self.assertIn("0",choices)
        self.assertEqual(choices["201"]["row_version"],3)
        self.assertEqual(client.request.call_args.kwargs["params"],{"merchant":"Cafe","limit":50,"cursor":"4"})

    def test_failed_read_preserves_page_and_search_does_not_drop_selected_target(self):
        client=Mock()
        client.request.side_effect=[{"items":[{"id":"chosen","row_version":2}],"next_cursor":"next"},
            TimeoutError(),{"items":[],"next_cursor":None},{"id":"chosen","row_version":4}]
        browser=TargetBrowser({},client,"/api/v1/transactions")
        browser.load()
        with self.assertRaises(TimeoutError):
            browser.load(more=True)
        self.assertEqual(browser.state["next_cursor"],"next")
        browser.load({"from":"2026-02-01"})
        choices,missing=browser.choices(["chosen"])
        self.assertEqual(choices["chosen"]["row_version"],4)
        self.assertEqual(missing,[])

    def test_selected_schedule_outside_current_page_is_resolved(self):
        client=Mock()
        client.request.return_value={"id":"plan-201","name":"Old plan","row_version":8}
        browser=TargetBrowser({"items":{}},client,"/api/v1/spending-schedules")
        choices,_=browser.choices(["plan-201"])
        self.assertEqual(choices["plan-201"]["row_version"],8)
        client.request.assert_called_once_with("GET","/api/v1/spending-schedules/plan-201")

    def test_deleted_target_is_reported_and_authorization_failure_is_not_hidden(self):
        client=Mock()
        browser=TargetBrowser({"items":{}},client,"/api/v1/transactions")
        client.request.side_effect=ApiError("Not found",status_code=404)
        choices,missing=browser.choices(["deleted"])
        self.assertEqual(choices,{})
        self.assertEqual(missing,["deleted"])
        client.request.side_effect=ApiError("Login expired",status_code=401)
        with self.assertRaises(ApiError):
            browser.choices(["deleted"])
