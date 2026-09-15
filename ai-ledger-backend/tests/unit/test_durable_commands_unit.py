import unittest
from uuid import uuid4

from app.auth.context import AuthContext
from app.services.durable_commands import compute_command_hash, get_audit_actor_info
from app.api.routes.accounts import CreateAliasRequest, PatchAliasRequest
from app.api.routes.categories import CreateCategoryRequest, PatchCategoryRequest


class TestDurableCommandsUnit(unittest.TestCase):
    def test_compute_command_hash_deterministic_key_ordering(self):
        body1 = {"name": "Groceries", "type": "expense", "description": "Food"}
        body2 = {"description": "Food", "name": "Groceries", "type": "expense"}
        hash1 = compute_command_hash("POST /api/v1/categories", body1)
        hash2 = compute_command_hash("POST /api/v1/categories", body2)
        self.assertEqual(hash1, hash2)

    def test_compute_command_hash_operation_and_path_sensitivity(self):
        acc1 = uuid4()
        acc2 = uuid4()
        body = {"alias": "Debit"}
        hash1 = compute_command_hash(f"POST /api/v1/accounts/{acc1}/aliases", body)
        hash2 = compute_command_hash(f"POST /api/v1/accounts/{acc2}/aliases", body)
        self.assertNotEqual(hash1, hash2)

    def test_get_audit_actor_info_browser_and_device(self):
        u_id = uuid4()
        d_id = uuid4()
        h_id = uuid4()

        # Browser actor
        ctx_browser = AuthContext(
            household_id=h_id,
            user_id=u_id,
            device_id=None,
            auth_mode="browser",
            household_role="owner",
        )
        actor_type, actor_user, actor_device = get_audit_actor_info(ctx_browser)
        self.assertEqual(actor_type, "user")
        self.assertEqual(actor_user, u_id)
        self.assertIsNone(actor_device)

        # Device actor
        ctx_device = AuthContext(
            household_id=h_id,
            user_id=u_id,
            device_id=d_id,
            auth_mode="device",
            household_role="owner",
        )
        actor_type, actor_user, actor_device = get_audit_actor_info(ctx_device)
        self.assertEqual(actor_type, "device")
        self.assertEqual(actor_user, u_id)
        self.assertEqual(actor_device, d_id)

    def test_patch_category_request_field_presence_hashing(self):
        cat_id = uuid4()
        op = f"PATCH /api/v1/categories/{cat_id}"

        # 1. Omitted description
        req_omitted = PatchCategoryRequest(expected_version=0, name="Food")
        dump_omitted = req_omitted.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(dump_omitted, {"expected_version": 0, "name": "Food"})

        # 2. Explicit null description (clearing description)
        req_null = PatchCategoryRequest(expected_version=0, name="Food", description=None)
        dump_null = req_null.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(dump_null, {"expected_version": 0, "name": "Food", "description": None})

        # 3. Explicit value description
        req_val = PatchCategoryRequest(expected_version=0, name="Food", description="Groceries")
        dump_val = req_val.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(dump_val, {"expected_version": 0, "name": "Food", "description": "Groceries"})

        # Hashes must be pairwise distinct
        h_omitted = compute_command_hash(op, dump_omitted)
        h_null = compute_command_hash(op, dump_null)
        h_val = compute_command_hash(op, dump_val)

        self.assertNotEqual(h_omitted, h_null)
        self.assertNotEqual(h_omitted, h_val)
        self.assertNotEqual(h_null, h_val)

    def test_patch_alias_request_field_presence_hashing(self):
        acc_id = uuid4()
        alias_id = uuid4()
        op = f"PATCH /api/v1/accounts/{acc_id}/aliases/{alias_id}"

        # 1. Alias only
        req_alias = PatchAliasRequest(expected_version=0, alias="Salary")
        dump_alias = req_alias.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(dump_alias, {"expected_version": 0, "alias": "Salary"})

        # 2. Status only
        req_status = PatchAliasRequest(expected_version=0, status="inactive")
        dump_status = req_status.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(dump_status, {"expected_version": 0, "status": "inactive"})

        # 3. Both
        req_both = PatchAliasRequest(expected_version=0, alias="Salary", status="inactive")
        dump_both = req_both.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(dump_both, {"expected_version": 0, "alias": "Salary", "status": "inactive"})

        h_alias = compute_command_hash(op, dump_alias)
        h_status = compute_command_hash(op, dump_status)
        h_both = compute_command_hash(op, dump_both)

        self.assertNotEqual(h_alias, h_status)
        self.assertNotEqual(h_alias, h_both)
        self.assertNotEqual(h_status, h_both)


if __name__ == "__main__":
    unittest.main()
