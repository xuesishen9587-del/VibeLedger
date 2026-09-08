import unittest
from unittest.mock import MagicMock

from api_client import BackendUnavailableError, ConflictError, TimeoutError, ValidationError
from settings_controller import (
    MutationModifiedPendingError,
    SettingsActionController,
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


if __name__ == "__main__":
    unittest.main()
