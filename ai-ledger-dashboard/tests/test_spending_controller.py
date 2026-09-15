import unittest
from unittest.mock import MagicMock
from api_client import TimeoutError, ServiceUnavailableError, ConflictError
from settings_controller import MutationModifiedPendingError
from spending_controller import SpendingActions


class SpendingActionsTest(unittest.TestCase):
    def test_unknown_outcome_retry_keeps_original_key_and_payload(self):
        client = MagicMock()
        client.request.side_effect = [TimeoutError(), {"id": "saved"}]
        state = {}
        actions = SpendingActions(state, client)
        with self.assertRaises(TimeoutError):
            actions.execute("create", "POST", "/transactions", {"amount": "10.00"})
        key = state["create"]["key"]
        with self.assertRaises(MutationModifiedPendingError):
            actions.execute("create", "POST", "/transactions", {"amount": "20.00"})
        self.assertEqual(actions.retry("create"), {"id": "saved"})
        self.assertEqual(client.request.call_args.kwargs["headers"]["Idempotency-Key"], key)
        self.assertEqual(state, {})

    def test_5xx_retains_command_and_conflict_ends_it(self):
        client = MagicMock()
        state = {}
        actions = SpendingActions(state, client)
        client.request.side_effect = ServiceUnavailableError("Unavailable", status_code=503)
        with self.assertRaises(ServiceUnavailableError):
            actions.execute("edit", "PATCH", "/transactions/a", {"expected_version": 0})
        self.assertIn("edit", state)
        client.request.side_effect = ConflictError("Reload", code="ROW_VERSION_CONFLICT", status_code=409)
        with self.assertRaises(ConflictError):
            actions.retry("edit")
        self.assertNotIn("edit", state)

    def test_target_is_part_of_command_identity(self):
        client = MagicMock()
        actions = SpendingActions({}, client)
        client.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            actions.execute("edit", "PATCH", "/transactions/a", {"expected_version": 0})
        with self.assertRaises(MutationModifiedPendingError):
            actions.execute("edit", "PATCH", "/transactions/b", {"expected_version": 0})
