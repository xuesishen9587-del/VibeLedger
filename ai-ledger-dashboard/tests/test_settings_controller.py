import unittest
from unittest.mock import MagicMock

from api_client import BackendUnavailableError, ConflictError, TimeoutError, ValidationError
from settings_controller import (
    MutationModifiedPendingError,
    SettingsActionController,
    SLOT_CREATE_ACCOUNT,
    SLOT_CREATE_ALIAS,
    SLOT_CREATE_CATEGORY,
)


class TestSettingsActionController(unittest.TestCase):
    """
    Shared temporal proofs for Settings idempotency lifecycle and row-version conflict UX.
    """

    def setUp(self):
        self.session_store = {}
        self.controller = SettingsActionController(session_state=self.session_store)

    def test_idempotency_lifecycle_success_terminates_action(self):
        """
        User begins one logical mutation -> generate K.
        Request succeeds -> logical action terminates -> K is no longer reused.
        """
        action_key = "edit_account_acc_1"
        payload = {"name": "Account 1", "expected_version": 1}

        # 1. Action begins -> generates K
        key_1 = self.controller.get_or_create_action(action_key, payload, operation="patch_account")
        self.assertTrue(key_1)

        # 2. Execution succeeds
        mock_fn = MagicMock(return_value={"id": "acc_1", "name": "Account 1"})
        res = self.controller.execute_mutation(action_key, "patch_account", payload, mock_fn)
        mock_fn.assert_called_once_with(key_1)
        self.assertEqual(res["name"], "Account 1")

        # 3. Subsequent intentional mutation generates new key K2 != K
        key_2 = self.controller.get_or_create_action(action_key, payload, operation="patch_account")
        self.assertNotEqual(key_1, key_2)

    def test_unknown_outcome_retry_reuses_exact_same_key(self):
        """
        Network timeout or backend unavailable after outcome became unknown
        -> preserve K -> retry same logical action uses exact same K and semantic request.
        """
        action_key = "edit_account_acc_2"
        payload = {"name": "Account 2", "balance_scope": "main asset", "expected_version": 0}

        calls = []

        def _failing_fn(key: str):
            calls.append(key)
            if len(calls) == 1:
                raise TimeoutError("Simulated request timeout")
            return {"id": "acc_2", "status": "ok"}

        # Attempt 1 -> TimeoutError
        with self.assertRaises(TimeoutError):
            self.controller.execute_mutation(action_key, "patch_account", payload, _failing_fn)

        self.assertEqual(len(calls), 1)
        first_key = calls[0]

        # Attempt 2 -> Retry same logical action
        res = self.controller.execute_mutation(action_key, "patch_account", payload, _failing_fn)
        self.assertEqual(len(calls), 2)
        second_key = calls[1]

        # Assert exact same key was reused for retry
        self.assertEqual(first_key, second_key)
        self.assertEqual(res["status"], "ok")

        # After successful completion, next mutation uses fresh key
        next_key = self.controller.get_or_create_action(action_key, payload, operation="patch_account")
        self.assertNotEqual(first_key, next_key)

    def test_changed_payload_after_unknown_outcome_rejects_silent_reuse(self):
        """
        User changes the intended mutation after unknown outcome
        -> client must not silently send the changed command under the old logical action
        -> raises MutationModifiedPendingError.
        """
        action_key = "edit_account_acc_3"
        payload_1 = {"name": "Original Name", "expected_version": 1}
        payload_2 = {"name": "Changed Name", "expected_version": 1}

        # Attempt 1 encounters backend unavailable
        def _fail_backend(key: str):
            raise BackendUnavailableError("Network failure")

        with self.assertRaises(BackendUnavailableError):
            self.controller.execute_mutation(action_key, "patch_account", payload_1, _fail_backend)

        # Attempt with modified payload under same logical action must be rejected
        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(action_key, "patch_account", payload_2, lambda k: None)

        # Clearing / reloading action allows user to start a new command with a fresh key
        self.controller.clear_action(action_key)
        new_key = self.controller.get_or_create_action(action_key, payload_2, operation="patch_account")
        self.assertTrue(new_key)

    def test_row_version_conflict_terminates_stale_action_and_triggers_reload(self):
        """
        409 Conflict with ROW_VERSION_CONFLICT
        -> terminates stale mutation attempt
        -> stale key is discarded
        -> is_conflict_reload_required returns True
        -> next intentional save uses fresh key K2 != K
        """
        action_key = "edit_category_cat_1"
        payload_stale = {"name": "Groceries", "expected_version": 0}

        calls = []

        def _conflict_fn(key: str):
            calls.append(key)
            raise ConflictError(
                message="Resource version mismatch",
                code="ROW_VERSION_CONFLICT",
                status_code=409,
            )

        with self.assertRaises(ConflictError) as ctx:
            self.controller.execute_mutation(action_key, "patch_category", payload_stale, _conflict_fn)

        self.assertEqual(ctx.exception.code, "ROW_VERSION_CONFLICT")
        self.assertTrue(self.controller.is_conflict_reload_required(action_key))
        stale_key = calls[0]

        # Stale key must NOT be reused; next intentional save (after reload with new version)
        payload_fresh = {"name": "Groceries", "expected_version": 1}
        new_key = self.controller.get_or_create_action(action_key, payload_fresh, operation="patch_category")
        self.assertNotEqual(stale_key, new_key)

        # Conflict reload required flag is cleared on new action
        self.assertFalse(self.controller.is_conflict_reload_required(action_key))

    def test_non_row_version_conflict_terminates_without_reload_flag(self):
        """
        409 with different code (e.g. IDEMPOTENCY_KEY_REUSE) terminates normally
        without setting is_conflict_reload_required.
        """
        action_key = "create_alias_acc_1"
        payload = {"alias": "Visa"}

        def _other_conflict(key: str):
            raise ConflictError(message="Duplicate key", code="IDEMPOTENCY_KEY_REUSE", status_code=409)

        with self.assertRaises(ConflictError):
            self.controller.execute_mutation(action_key, "create_alias", payload, _other_conflict)

        self.assertFalse(self.controller.is_conflict_reload_required(action_key))

    def test_deterministic_terminal_4xx_terminates_action(self):
        """
        Deterministic terminal 4xx (e.g. 422 ValidationError) terminates logical action.
        """
        action_key = "create_category_invalid"
        payload = {"name": "", "type": "expense"}

        def _validation_err(key: str):
            raise ValidationError("Name cannot be empty", code="VALIDATION_ERROR", status_code=422)

        with self.assertRaises(ValidationError):
            self.controller.execute_mutation(action_key, "create_category", payload, _validation_err)

        # Action is terminated in store
        self.assertNotIn(action_key, self.session_store)

    # --------------------------------------------------------------------------
    # Regression Proofs for Three Create Families (Temporal Closure)
    # --------------------------------------------------------------------------

    def test_account_create_timeout_change_name_or_currency_blocked_before_http(self):
        """
        UI-wiring regression proof 1 (Account Create):
        - submit command A -> timeout -> pending key K
        - exact retry of A -> reuses same K
        - change name -> blocked before HTTP (0 HTTP calls, no K2 generated)
        - change currency -> blocked before HTTP (0 HTTP calls, no K2 generated)
        - explicitly clear action -> command B may begin with fresh K2
        """
        slot = SLOT_CREATE_ACCOUNT
        payload_a = {
            "name": "工行日常卡",
            "balance_scope": "asset",
            "account_type": "savings",
            "currency": "CNY",
            "statement_import_enabled": False,
        }

        # 1. Submit command A -> timeout -> pending key K
        http_calls = []

        def _timeout_http(key: str):
            http_calls.append(key)
            raise TimeoutError("Connection to backend timed out")

        with self.assertRaises(TimeoutError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_account",
                payload=payload_a,
                mutation_fn=_timeout_http,
            )

        self.assertEqual(len(http_calls), 1)
        pending_key = http_calls[0]
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)
        self.assertEqual(self.session_store[slot]["status"], "pending")

        # 2. Exact retry of A -> reuses same K
        retry_calls = []

        def _retry_http(key: str):
            retry_calls.append(key)
            return {"id": "acc-new-1", "name": "工行日常卡", "currency": "CNY"}

        # Simulate retry before mutating: exact retry reuses same K
        # But we want to test changing fields while unresolved:
        # First verify exact same retry uses same K:
        key_for_a = self.controller.get_or_create_action(slot, payload_a, operation="create_account")
        self.assertEqual(key_for_a, pending_key)

        # 3. Change name (A -> B1) -> blocked before HTTP
        payload_b1 = dict(payload_a, name="招行卡")
        forbidden_http = MagicMock()

        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_account",
                payload=payload_b1,
                mutation_fn=forbidden_http,
            )

        forbidden_http.assert_not_called()
        # Verify key was not replaced and no K2 generated
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 4. Change currency (A -> B2) -> blocked before HTTP
        payload_b2 = dict(payload_a, currency="USD")
        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_account",
                payload=payload_b2,
                mutation_fn=forbidden_http,
            )

        forbidden_http.assert_not_called()
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 5. After old action is explicitly cleared -> command B may begin with fresh K2
        self.controller.clear_action(slot)
        self.assertNotIn(slot, self.session_store)

        success_http = MagicMock(return_value={"id": "acc-new-2", "name": "招行卡", "currency": "USD"})
        res = self.controller.execute_mutation(
            action_key=slot,
            operation="create_account",
            payload=payload_b2,
            mutation_fn=success_http,
        )
        self.assertEqual(res["id"], "acc-new-2")
        new_key = success_http.call_args[0][0]
        self.assertNotEqual(new_key, pending_key)

    def test_alias_create_timeout_change_alias_or_target_account_blocked_before_http(self):
        """
        UI-wiring regression proof 2 (Alias Create):
        - submit command A -> timeout -> pending key K
        - exact retry of A -> reuses same K
        - change alias -> blocked before HTTP (0 HTTP calls, no K2 generated)
        - change target account -> blocked before HTTP (0 HTTP calls, no K2 generated)
        - explicitly clear action -> command B may begin with fresh K2
        """
        slot = SLOT_CREATE_ALIAS
        acc_1 = "acc-uuid-1111"
        acc_2 = "acc-uuid-2222"
        payload_a = {"alias": "工行日常卡别名"}

        # 1. Submit command A for acc_1 -> timeout -> pending key K
        http_calls = []

        def _timeout_http(key: str):
            http_calls.append(key)
            raise TimeoutError("Gateway timeout")

        with self.assertRaises(TimeoutError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_alias",
                payload=payload_a,
                resource_id=acc_1,
                mutation_fn=_timeout_http,
            )

        self.assertEqual(len(http_calls), 1)
        pending_key = http_calls[0]
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 2. Exact retry of A (same acc_1, same alias) -> reuses same K
        key_for_a = self.controller.get_or_create_action(slot, payload_a, operation="create_alias", resource_id=acc_1)
        self.assertEqual(key_for_a, pending_key)

        # 3. Change alias (same acc_1, different alias) -> blocked before HTTP
        payload_b1 = {"alias": "工行二类卡别名"}
        forbidden_http = MagicMock()

        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_alias",
                payload=payload_b1,
                resource_id=acc_1,
                mutation_fn=forbidden_http,
            )

        forbidden_http.assert_not_called()
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 4. Change target account (different acc_2, same alias) -> blocked before HTTP
        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_alias",
                payload=payload_a,
                resource_id=acc_2,
                mutation_fn=forbidden_http,
            )

        forbidden_http.assert_not_called()
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 5. After old action is explicitly cleared -> command B may begin with fresh K2
        self.controller.clear_action(slot)
        self.assertNotIn(slot, self.session_store)

        success_http = MagicMock(return_value={"id": "alias-2", "alias": "工行二类卡别名"})
        res = self.controller.execute_mutation(
            action_key=slot,
            operation="create_alias",
            payload=payload_b1,
            resource_id=acc_2,
            mutation_fn=success_http,
        )
        self.assertEqual(res["id"], "alias-2")
        new_key = success_http.call_args[0][0]
        self.assertNotEqual(new_key, pending_key)

    def test_category_create_timeout_change_name_or_type_blocked_before_http(self):
        """
        UI-wiring regression proof 3 (Category Create):
        - submit command A -> timeout -> pending key K
        - exact retry of A -> reuses same K
        - change name -> blocked before HTTP (0 HTTP calls, no K2 generated)
        - change type -> blocked before HTTP (0 HTTP calls, no K2 generated)
        - explicitly clear action -> command B may begin with fresh K2
        """
        slot = SLOT_CREATE_CATEGORY
        payload_a = {
            "name": "餐饮美食",
            "category_type": "expense",
            "description": "餐馆日常消费",
        }

        # 1. Submit command A -> timeout -> pending key K
        http_calls = []

        def _timeout_http(key: str):
            http_calls.append(key)
            raise TimeoutError("Request timed out")

        with self.assertRaises(TimeoutError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_category",
                payload=payload_a,
                mutation_fn=_timeout_http,
            )

        self.assertEqual(len(http_calls), 1)
        pending_key = http_calls[0]
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 2. Exact retry of A -> reuses same K
        key_for_a = self.controller.get_or_create_action(slot, payload_a, operation="create_category")
        self.assertEqual(key_for_a, pending_key)

        # 3. Change name (餐饮美食 -> 商务宴请) -> blocked before HTTP
        payload_b1 = dict(payload_a, name="商务宴请")
        forbidden_http = MagicMock()

        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_category",
                payload=payload_b1,
                mutation_fn=forbidden_http,
            )

        forbidden_http.assert_not_called()
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 4. Change category_type (expense -> income) -> blocked before HTTP
        payload_b2 = dict(payload_a, category_type="income")
        with self.assertRaises(MutationModifiedPendingError):
            self.controller.execute_mutation(
                action_key=slot,
                operation="create_category",
                payload=payload_b2,
                mutation_fn=forbidden_http,
            )

        forbidden_http.assert_not_called()
        self.assertEqual(self.session_store[slot]["idempotency_key"], pending_key)

        # 5. After old action is explicitly cleared -> command B may begin with fresh K2
        self.controller.clear_action(slot)
        self.assertNotIn(slot, self.session_store)

        success_http = MagicMock(return_value={"id": "cat-new-2", "name": "商务宴请", "category_type": "income"})
        res = self.controller.execute_mutation(
            action_key=slot,
            operation="create_category",
            payload=payload_b2,
            mutation_fn=success_http,
        )
        self.assertEqual(res["id"], "cat-new-2")
        new_key = success_http.call_args[0][0]
        self.assertNotEqual(new_key, pending_key)

    def test_exact_same_retry_reuses_original_key_across_all_three_families(self):
        """
        Regression proof 4:
        Proves that exact same retries across all three create families
        preserve and reuse the original idempotency key until resolution.
        """
        families = [
            (
                SLOT_CREATE_ACCOUNT,
                "create_account",
                {"name": "Savings", "currency": "CNY", "account_type": "savings", "balance_scope": "asset"},
                None,
            ),
            (
                SLOT_CREATE_ALIAS,
                "create_alias",
                {"alias": "Debit Card 1"},
                "acc-uuid-9999",
            ),
            (
                SLOT_CREATE_CATEGORY,
                "create_category",
                {"name": "Utilities", "category_type": "expense", "description": "Electric bill"},
                None,
            ),
        ]

        for slot, op, payload, res_id in families:
            calls = []

            def _flaky_mutation(key: str):
                calls.append(key)
                if len(calls) == 1:
                    raise BackendUnavailableError("Temporary network disconnect")
                return {"status": "created", "id": "entity-123"}

            # Attempt 1 -> fails with unknown outcome
            with self.assertRaises(BackendUnavailableError):
                self.controller.execute_mutation(
                    action_key=slot,
                    operation=op,
                    payload=payload,
                    resource_id=res_id,
                    mutation_fn=_flaky_mutation,
                )

            self.assertEqual(len(calls), 1)
            initial_key = calls[0]

            # Attempt 2 -> exact same retry -> must reuse initial_key
            result = self.controller.execute_mutation(
                action_key=slot,
                operation=op,
                payload=payload,
                resource_id=res_id,
                mutation_fn=_flaky_mutation,
            )

            self.assertEqual(len(calls), 2)
            retry_key = calls[1]
            self.assertEqual(initial_key, retry_key)
            self.assertEqual(result["status"], "created")

            # Terminated in store -> cannot reuse
            self.assertNotIn(slot, self.session_store)


if __name__ == "__main__":
    unittest.main()
