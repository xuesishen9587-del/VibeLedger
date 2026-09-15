import json
import time
from uuid import uuid4
from unittest.mock import Mock
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from tests.support.db_helper import BaseDbTestCase
from tests.integration import test_s2_spending_db as setup
from app.api.deps import get_auth_context
from app.auth.jwks_verifier import JWKSBrowserAuthVerifier
from app.auth.browser_verifier import set_browser_verifier


class TestS4JWKSAuthDb(BaseDbTestCase):
    def seed_test_data(self):
        setup.TestS2SpendingDb.seed_test_data(self)
        self.client.app.dependency_overrides.pop(get_auth_context)
        self.issuer="https://household.supabase.co/auth/v1"
        self.key=ec.generate_private_key(ec.SECP256R1())
        jwk=json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="one",alg="ES256",use="sig")
        self.fetch=Mock(return_value={"keys":[jwk]})
        set_browser_verifier(JWKSBrowserAuthVerifier(self.issuer+"/.well-known/jwks.json",self.issuer,"authenticated",["ES256"],fetch=self.fetch))

    def tearDown(self):
        set_browser_verifier(None)
        super().tearDown()

    def headers(self,sub=None,**claims):
        value=jwt.encode({"sub":sub or str(self.user),"iss":self.issuer,"aud":"authenticated","exp":time.time()+300,**claims},
            self.key,algorithm="ES256",headers={"kid":"one"})
        return {"Authorization":"Bearer "+value}

    def test_verified_user_maps_to_household_and_unknown_signup_cannot_enter(self):
        result=self.client.get("/api/v1/review",headers=self.headers())
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn(self.client.get("/api/v1/review",headers=self.headers(str(uuid4()))).status_code,(401,403))
        self.assertEqual(self.client.get("/api/v1/review").status_code,401)

    def test_disabled_member_is_rechecked_even_with_cached_signing_key(self):
        headers=self.headers()
        self.assertEqual(self.client.get("/api/v1/review",headers=headers).status_code,200)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE users SET status='disabled' WHERE id=%s",(self.user,))
        self.conn.commit()
        self.assertIn(self.client.get("/api/v1/review",headers=headers).status_code,(401,403))
        self.assertEqual(self.fetch.call_count,1)

    def test_wrong_project_audience_and_expiry_fail_before_financial_access(self):
        for claims in ({"iss":"https://other.supabase.co/auth/v1"},{"aud":"other"},{"exp":1}):
            with self.subTest(claims=claims):
                self.assertEqual(self.client.get("/api/v1/review",headers=self.headers(**claims)).status_code,401)
