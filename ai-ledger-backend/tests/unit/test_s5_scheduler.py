import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.scheduler import verify_scheduler_token
from app.main import create_app


class SchedulerBoundaryTests(unittest.TestCase):
    audience = "https://isolated-backend.example"
    email = "daily@example.iam.gserviceaccount.com"

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def token(self, **overrides):
        claims = {"iss": "https://accounts.google.com", "aud": self.audience,
                  "email": self.email, "email_verified": True, "sub": "service-identity",
                  "iat": int(time.time()) - 1, "exp": int(time.time()) + 300}
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"})

    def test_real_signature_claims_and_expiry_validation(self):
        # Stub only certificate retrieval: Google's signature/expiry verifier executes.
        with patch("google.oauth2.id_token._fetch_certs", return_value={"test-key": self.pem}):
            verify_scheduler_token(self.token(), self.audience, self.email)
            for claims in ({"aud": "other"}, {"email": "other"}, {"iss": "browser"},
                           {"email_verified": False}, {"exp": 1}):
                with self.subTest(claims=claims), self.assertRaises(HTTPException) as err:
                    verify_scheduler_token(self.token(**claims), self.audience, self.email)
                self.assertEqual(err.exception.status_code, 401)
            token = self.token()
            with self.assertRaises(HTTPException):
                verify_scheduler_token(token[:-8] + "AAAAAAAA", self.audience, self.email)

    def test_http_auth_scope_and_method_before_database(self):
        client = TestClient(create_app())
        config = SimpleNamespace(SCHEDULER_AUDIENCE=self.audience, SCHEDULER_SERVICE_ACCOUNT=self.email)
        with patch("app.auth.scheduler.get_settings", return_value=config), \
             patch("google.oauth2.id_token._fetch_certs", return_value={"test-key": self.pem}), \
             patch("app.api.routes.internal_schedules.run_due", return_value={"processed": 0}) as run:
            path = "/internal/spending-schedules/run"
            for headers in ({}, {"Authorization": "Bearer device-token"},
                            {"Authorization": "Bearer " + self.token(iss="supabase")}):
                self.assertEqual(client.post(path, headers=headers).status_code, 401)
            run.assert_not_called()
            headers = {"Authorization": "Bearer " + self.token()}
            self.assertEqual(client.post(path, headers=headers, json={"household_id": "foreign"}).status_code, 422)
            self.assertEqual(client.post(path + "?today=2099-01-01", headers=headers).status_code, 422)
            self.assertEqual(client.get(path, headers=headers).status_code, 405)
            run.assert_not_called()
            self.assertEqual(client.post(path, headers=headers, json={}).json(), {"processed": 0})
            run.assert_called_once()

    def test_retired_routes_not_registered(self):
        paths = create_app().openapi()["paths"]
        for path in paths:
            self.assertFalse(any(word in path for word in ("reconciliation", "installment", "work-queue", "audit-events")), path)
        self.assertIn("/internal/spending-schedules/run", paths)
