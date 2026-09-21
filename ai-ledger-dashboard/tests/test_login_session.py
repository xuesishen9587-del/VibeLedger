import unittest
from unittest.mock import Mock
from login_session import LoginSession, SupabasePasswordAuth, LoginError, LoginUnavailable


def payload(subject="alice",access="access-a",refresh="refresh-a",expires=2000):
    return {"access_token":access,"refresh_token":refresh,"expires_at":expires,"user":{"id":subject}}


class LoginSessionTest(unittest.TestCase):
    def setUp(self):
        self.state={}
        self.provider=Mock()
        self.provider.sign_in.return_value=payload()
        self.now=[1000]
        self.login=LoginSession(self.state,self.provider,clock=lambda:self.now[0])
        self.authorize=Mock()

    def sign_in(self):
        self.login.sign_in("a@example.test","password-not-retained",self.authorize)

    def test_login_authorizes_member_and_never_stores_password(self):
        self.state.update(old_cached_financial_data="old",auth_token="old-environment-token")
        self.sign_in()
        self.authorize.assert_called_once_with("access-a")
        self.assertEqual(set(self.state),{"_login"})
        self.assertNotIn("password-not-retained",repr(self.state))
        self.assertEqual(self.login.access_token(),"access-a")
        self.provider.refresh.assert_not_called()

    def test_two_sessions_rotate_independently(self):
        self.sign_in()
        second={}
        other_provider=Mock()
        other_provider.sign_in.return_value=payload("bob","access-b","refresh-b")
        other=LoginSession(second,other_provider,clock=lambda:1000)
        other.sign_in("b@example.test","password",Mock())
        self.now[0]=1941
        self.provider.refresh.return_value=payload(access="new-access-a",refresh="rotated-a",expires=3000)
        self.assertEqual(self.login.access_token(),"new-access-a")
        self.assertEqual(self.state["_login"]["refresh"],"rotated-a")
        self.assertEqual(other.access_token(),"access-b")
        other_provider.refresh.assert_not_called()

    def test_transient_refresh_preserves_unknown_financial_request_and_original_refresh(self):
        self.sign_in()
        self.state["_spending_actions"]={"save":{"key":"original-command"}}
        self.now[0]=2001
        self.provider.refresh.side_effect=LoginUnavailable("Retry")
        with self.assertRaises(LoginUnavailable):
            self.login.access_token()
        self.assertEqual(self.state["_login"]["refresh"],"refresh-a")
        self.assertEqual(self.state["_spending_actions"]["save"]["key"],"original-command")

    def test_expired_refresh_reauthentication_preserves_same_user_only(self):
        self.sign_in()
        self.state["pending"]="original-command"
        self.now[0]=1941
        self.provider.refresh.side_effect=LoginError("Expired")
        with self.assertRaises(LoginError):
            self.login.access_token()
        self.assertNotIn("_login",self.state)
        self.sign_in()
        self.assertEqual(self.state["pending"],"original-command")
        self.provider.sign_in.return_value=payload("bob",expires=3000)
        self.sign_in()
        self.assertNotIn("pending",self.state)

    def test_identity_change_on_refresh_requires_new_login(self):
        self.sign_in()
        self.now[0]=1941
        self.provider.refresh.return_value=payload("bob",expires=3000)
        with self.assertRaises(LoginError):
            self.login.access_token()
        self.assertNotIn("_login",self.state)
        self.assertEqual(self.state["_reauth_subject"],"alice")

    def test_logout_always_clears_all_state_even_if_provider_is_unavailable(self):
        self.sign_in()
        self.state["password_widget"]="secret"
        self.state["cached_report"]={"amount":"123"}
        self.provider.sign_out.side_effect=LoginUnavailable("offline")
        self.assertFalse(self.login.sign_out())
        self.assertEqual(self.state,{})

    def test_nonmember_and_malformed_response_never_establish_session(self):
        self.authorize.side_effect=RuntimeError("token must never appear")
        with self.assertRaises(LoginError) as caught:
            self.sign_in()
        self.assertNotIn("token must never appear",str(caught.exception))
        self.assertNotIn("_login",self.state)
        self.provider.sign_out.assert_called_once_with("access-a")
        self.authorize.side_effect=None
        for bad in ({},payload(expires=float("nan")),payload(expires=0)):
            self.provider.sign_in.return_value=bad
            with self.assertRaises(LoginUnavailable):
                self.sign_in()

    def test_only_publishable_https_configuration_and_safe_errors(self):
        for url,key in (("http://project.supabase.co","sb_publishable_test"),("https://project.supabase.co","sb_secret_private"),
                        ("https://user:pass@project.supabase.co","sb_publishable_test")):
            with self.assertRaises(LoginError):
                SupabasePasswordAuth(url,key)
        post=Mock(return_value=Mock(status_code=400,text="password and token",json=Mock(return_value={"error":"secret"})))
        auth=SupabasePasswordAuth("https://project.supabase.co","sb_publishable_test",post)
        with self.assertRaises(LoginError) as caught:
            auth.sign_in("a@example.test","secret")
        self.assertNotIn("secret",str(caught.exception))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["params"],{"grant_type":"password"})
        self.assertEqual(post.call_args.kwargs["headers"],{"apikey":"sb_publishable_test"})
