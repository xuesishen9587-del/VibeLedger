from pathlib import Path
from unittest.mock import Mock, patch
import unittest
from contextlib import ExitStack
from streamlit.testing.v1 import AppTest
import streamlit as st

APP=Path(__file__).resolve().parents[1]/"app.py"


class AppNavigationTest(unittest.TestCase):
    def test_logged_out_app_never_requests_financial_data_and_hides_token_entry(self):
        with patch.dict("os.environ",{"SUPABASE_URL":"https://project.supabase.co","SUPABASE_PUBLISHABLE_KEY":"sb_publishable_test"}),patch("api_client.ApiClient.request") as request:
            app=AppTest.from_file(str(APP)).run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual([x.label for x in app.text_input],["邮箱","密码"])
        self.assertEqual(len(app.radio),0)
        request.assert_not_called()

    def test_four_destinations_route_to_current_pages_and_statement_import(self):
        login=Mock()
        login.access_token.return_value="session-token"
        calls=[]
        def page(name):
            def render(*args,**kwargs):
                calls.append((name,kwargs))
                st.write(name)
            return render
        targets={"wealth_page.render":"wealth","investment_view.render":"investment","spending_page.render":"spending",
            "statement_page.render":"statement","wealth_page.render_balance_drafts":"balance_drafts","settings_page.render":"settings",
            "investment_view.render_settings":"threshold","device_view.render":"devices"}
        with ExitStack() as stack:
            stack.enter_context(patch("login_view.require_login",return_value=("session-token",login)))
            request=stack.enter_context(patch("api_client.ApiClient.request"))
            for target,name in targets.items():
                stack.enter_context(patch(target,side_effect=page(name)))
            app=AppTest.from_file(str(APP)).run()
            self.assertEqual(app.radio[0].options,["财富","支出","复核","设置"])
            for destination in ("支出","复核","设置"):
                calls.clear()
                app.radio(key="main_page").set_value(destination).run()
                self.assertEqual(len(app.exception),0)
                self.assertTrue(calls)
            self.assertIn("devices",[name for name,_ in calls])
            app.radio(key="main_page").set_value("支出").run()
            app.radio(key="spending_section").set_value("账单导入").run()
            self.assertEqual(calls[-1][0],"statement")
            request.assert_not_called()

    def test_password_callback_clears_password_and_enters_authenticated_session(self):
        provider=Mock()
        provider.sign_in.return_value={"access_token":"fixture-token","refresh_token":"fixture-refresh","expires_at":9999999999,"user":{"id":"alice"}}
        with patch("login_view.SupabasePasswordAuth",return_value=provider),patch("api_client.ApiClient.request",return_value={"counts":{}}),patch("wealth_page.render"),patch("investment_view.render"):
            app=AppTest.from_file(str(APP)).run()
            app.text_input(key="login_email").set_value("a@example.test")
            app.text_input(key="login_password").set_value("fixture-password")
            next(b for b in app.button if b.label=="登录").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state["_login"]["subject"],"alice")
        self.assertNotIn("fixture-password",repr(app.session_state))
        self.assertEqual(app.radio[0].options,["财富","支出","复核","设置"])

    def test_logout_removes_cached_finances_and_returns_to_login(self):
        provider=Mock()
        targets=["wealth_page.render","investment_view.render","settings_page.render","investment_view.render_settings","device_view.render"]
        with ExitStack() as stack:
            stack.enter_context(patch("login_view.SupabasePasswordAuth",return_value=provider))
            for target in targets:
                stack.enter_context(patch(target))
            app=AppTest.from_file(str(APP))
            app.session_state["_login"]={"subject":"alice","access":"fixture-token","refresh":"fixture-refresh","expires":9999999999}
            app.session_state["cached_finances"]="private cached report"
            app.run()
            app.radio(key="main_page").set_value("设置").run()
            next(b for b in app.button if b.label=="退出登录").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertNotIn("cached_finances",app.session_state)
        self.assertNotIn("_login",app.session_state)
        self.assertEqual([x.label for x in app.text_input],["邮箱","密码"])
        provider.sign_out.assert_called_once_with("fixture-token")

    def test_settings_refresh_preserves_unknown_outcome_command(self):
        script='''
import streamlit as st
from settings_page import render
class Client:
    def list_accounts(self):
        return {"items":[]}
    def list_categories(self,**kwargs):
        return {"items":[]}
render(Client())
'''
        app=AppTest.from_string(script)
        app.session_state["_settings_actions"]={"pending":{"idempotency_key":"keep-original-key"}}
        app.run()
        self.assertEqual(len(app.exception),0)
        next(b for b in app.button if b.key=="btn_settings_refresh").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state["_settings_actions"]["pending"]["idempotency_key"],"keep-original-key")
