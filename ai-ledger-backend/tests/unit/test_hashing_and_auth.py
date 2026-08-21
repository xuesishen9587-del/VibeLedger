import unittest
import hashlib
from app.services.expense_service import compute_request_hash

class TestHashingAndAuth(unittest.TestCase):
    def test_request_hash_determinism_and_canonicalization(self):
        payload1 = {
            "idempotency_key": "req-12345",
            "captured_at": "2026-08-20T10:00:00Z",
            "client_version": "1.0.0",
            "image": {
                "mime_type": "image/jpeg",
                "base64": "aGVsbG8="
            },
            "note": "Lunch with team"
        }
        payload2 = {
            "note": "Lunch with team",
            "client_version": "1.0.0",
            "image": {
                "base64": "aGVsbG8=",
                "mime_type": "image/jpeg"
            },
            "captured_at": "2026-08-20T10:00:00Z",
            "idempotency_key": "req-12345"
        }
        
        hash1 = compute_request_hash(payload1)
        hash2 = compute_request_hash(payload2)
        
        self.assertEqual(len(hash1), 32)
        self.assertEqual(hash1, hash2)

    def test_request_hash_sensitivity_to_changes(self):
        base_payload = {
            "idempotency_key": "req-12345",
            "captured_at": "2026-08-20T10:00:00Z",
            "client_version": "1.0.0",
            "image": {"mime_type": "image/jpeg", "base64": "aGVsbG8="},
            "note": "Lunch"
        }
        base_hash = compute_request_hash(base_payload)

        # 1. Different note
        diff_note = dict(base_payload, note="Dinner")
        self.assertNotEqual(base_hash, compute_request_hash(diff_note))

        # 2. Different base64
        diff_image = dict(base_payload, image={"mime_type": "image/jpeg", "base64": "d29ybGQ="})
        self.assertNotEqual(base_hash, compute_request_hash(diff_image))

        # 3. Different captured_at
        diff_time = dict(base_payload, captured_at="2026-08-21T10:00:00Z")
        self.assertNotEqual(base_hash, compute_request_hash(diff_time))

    def test_device_token_sha256_hashing(self):
        raw_token = "secret_ios_device_token_abcdef123456"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        
        self.assertEqual(len(token_hash), 32)
        self.assertNotIn(raw_token.encode("utf-8"), token_hash)
        
        # Re-hashing produces same deterministic digest
        self.assertEqual(token_hash, hashlib.sha256(raw_token.encode("utf-8")).digest())
