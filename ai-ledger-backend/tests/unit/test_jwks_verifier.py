import json
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from app.auth.jwks_verifier import JWKSBrowserAuthVerifier
from app.domain.auth import InvalidCredentialsError

ISSUER="https://household.supabase.co/auth/v1"


def key_pair(kid="one",algorithm="RS256"):
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048) if algorithm=="RS256" else ec.generate_private_key(ec.SECP256R1())
    builder=jwt.algorithms.RSAAlgorithm if algorithm=="RS256" else jwt.algorithms.ECAlgorithm
    public=json.loads(builder.to_jwk(private.public_key()))
    public.update(kid=kid,alg=algorithm,use="sig")
    return private,public


def token(private,kid="one",algorithm="RS256",**claims):
    return jwt.encode({"sub":"member","iss":ISSUER,"aud":"authenticated","exp":time.time()+300,**claims},private,algorithm=algorithm,headers={"kid":kid})


class JWKSVerifierTest(unittest.TestCase):
    def setUp(self):
        self.private,self.public=key_pair()
        self.now=[1000]
        self.fetch=Mock(return_value={"keys":[self.public]})
        self.verifier=JWKSBrowserAuthVerifier(ISSUER+"/.well-known/jwks.json",ISSUER,"authenticated",["RS256","ES256"],fetch=self.fetch,clock=lambda:self.now[0])

    def test_signature_claims_and_public_key_cache(self):
        value=token(self.private)
        self.assertEqual(self.verifier.verify(value)["sub"],"member")
        self.verifier.verify(value)
        self.assertEqual(self.fetch.call_count,1)
        for claims in ({"iss":"https://other.supabase.co/auth/v1"},{"aud":"other"},{"exp":0},{"sub":""},{"nbf":time.time()+900}):
            with self.subTest(claims=claims),self.assertRaises(InvalidCredentialsError):
                self.verifier.verify(token(self.private,**claims))
        forged,_=key_pair()
        with self.assertRaises(InvalidCredentialsError):
            self.verifier.verify(token(forged))

    def test_unknown_kid_refreshes_for_rotation_and_revoked_keys_disappear(self):
        self.verifier.verify(token(self.private))
        second,public=key_pair("two","ES256")
        self.fetch.return_value={"keys":[public]}
        self.verifier.verify(token(second,"two","ES256"))
        self.assertEqual(self.fetch.call_count,2)
        with self.assertRaises(InvalidCredentialsError):
            self.verifier.verify(token(self.private))
        self.assertEqual(self.fetch.call_count,2)

    def test_unknown_key_flood_has_bounded_network_requests(self):
        self.verifier.verify(token(self.private))
        for kid in ("unknown-a","unknown-b","unknown-c"):
            with self.assertRaises(InvalidCredentialsError):
                self.verifier.verify(token(self.private,kid))
        self.assertEqual(self.fetch.call_count,2)
        self.now[0]+=31
        with self.assertRaises(InvalidCredentialsError):
            self.verifier.verify(token(self.private,"unknown-d"))
        self.assertEqual(self.fetch.call_count,3)

    def test_expired_cache_fails_closed_and_outage_retries_are_bounded(self):
        value=token(self.private)
        self.verifier.verify(value)
        self.now[0]+=301
        self.fetch.side_effect=RuntimeError("sensitive provider response")
        for _ in range(2):
            with self.assertRaises(InvalidCredentialsError) as caught:
                self.verifier.verify(value)
            self.assertNotIn("sensitive",str(caught.exception))
        self.assertEqual(self.fetch.call_count,2)
        self.now[0]+=31
        self.fetch.side_effect=None
        self.verifier.verify(value)
        self.assertEqual(self.fetch.call_count,3)

    def test_configuration_and_algorithm_cannot_be_selected_by_token(self):
        for url,issuer,alg in (("https://evil.test/keys",ISSUER,["RS256"]),(ISSUER+"/.well-known/jwks.json",ISSUER,["HS256"]),
            ("http://local/auth/v1/.well-known/jwks.json","http://local/auth/v1",["RS256"])):
            with self.assertRaises(InvalidCredentialsError):
                JWKSBrowserAuthVerifier(url,issuer,"authenticated",alg)
        with self.assertRaises(InvalidCredentialsError):
            self.verifier.verify(token("fixture-secret-for-hmac-only","one","HS256"))
        self.fetch.assert_not_called()

    def test_invalid_duplicate_key_set_and_missing_required_claims(self):
        self.fetch.return_value={"keys":[self.public,self.public]}
        with self.assertRaises(InvalidCredentialsError):
            self.verifier.verify(token(self.private))
        self.now[0]+=31
        self.fetch.return_value={"keys":[self.public]}
        value=jwt.encode({"sub":"member","iss":ISSUER,"aud":"authenticated"},self.private,algorithm="RS256",headers={"kid":"one"})
        with self.assertRaises(InvalidCredentialsError):
            self.verifier.verify(value)

    def test_production_has_no_static_secret_fallback(self):
        from app.auth.browser_verifier import get_browser_verifier,set_browser_verifier
        set_browser_verifier(None)
        with patch("app.config.get_settings",return_value=SimpleNamespace(AUTH_JWKS_URL=None,ENVIRONMENT="production")):
            with self.assertRaises(InvalidCredentialsError):
                get_browser_verifier()
