import json
import uuid
from typing import Any, Callable, Dict, Optional

from api_client import BackendUnavailableError, ConflictError, TimeoutError


class MutationModifiedPendingError(Exception):
    """
    Raised when the user modifies the intended mutation command after an unknown outcome
    (network timeout / unavailable) while the previous action is still pending.
    Prevents silent key reuse for semantically different payloads.
    """
    pass


def _compute_fingerprint(operation: str, payload: Any) -> str:
    """Computes a deterministic JSON fingerprint for the intended mutation."""
    try:
        serialized = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        serialized = str(payload)
    return f"{operation}:{serialized}"


class SettingsActionController:
    """
    Manages the idempotency key lifecycle and optimistic concurrency control (OCC)
    for logical UI actions in Dashboard Account / Alias / Category Settings.

    Guarantees:
    - Exactly one stable Idempotency-Key per logical UI action.
    - Preserves key across network timeouts / backend unavailable retries.
    - Rejects silent key reuse if intended payload changes after unknown outcome.
    - Terminates logical action upon successful completion or terminal 4xx.
    - On 409 ROW_VERSION_CONFLICT: specifically terminates stale action,
      discards stale key, and requires reload so next intentional Save uses
      the newly loaded row_version with a fresh idempotency key.
    - Persists state across Streamlit reruns using st.session_state.
    """

    def __init__(self, session_state: Optional[Dict[str, Any]] = None):
        self._custom_state = session_state
        self._internal_fallback: Dict[str, Any] = {}

    def _get_store(self) -> Dict[str, Any]:
        if self._custom_state is not None:
            return self._custom_state
        try:
            import streamlit as st
            if hasattr(st, "session_state"):
                if "_settings_actions" not in st.session_state:
                    st.session_state["_settings_actions"] = {}
                return st.session_state["_settings_actions"]
        except Exception:
            pass
        return self._internal_fallback

    def get_or_create_action(self, action_key: str, payload: Any, operation: str = "") -> str:
        """
        Retrieves existing idempotency key for retry of same payload,
        or generates a fresh idempotency key for a new logical action.
        """
        store = self._get_store()
        fp = _compute_fingerprint(operation, payload)
        existing = store.get(action_key)

        if existing:
            status = existing.get("status")
            if status == "pending":
                if existing.get("fingerprint") == fp:
                    # Same semantic mutation being retried after unknown outcome
                    return existing["idempotency_key"]
                else:
                    # User changed intended mutation after unknown outcome
                    raise MutationModifiedPendingError(
                        f"Mutation payload for action '{action_key}' was modified while a previous attempt "
                        "has an unknown outcome. Please reload current state before submitting a new command."
                    )
            elif status == "conflict_reload_required":
                # Previous attempt encountered ROW_VERSION_CONFLICT.
                # User has now initiated a new intentional save.
                # Clean up old action and generate new key.
                store.pop(action_key, None)

        # Generate fresh key for new logical action
        new_key = str(uuid.uuid4())
        store[action_key] = {
            "idempotency_key": new_key,
            "fingerprint": fp,
            "status": "pending",
        }
        return new_key

    def complete_action(self, action_key: str) -> None:
        """Terminates the logical action so its key is never reused."""
        store = self._get_store()
        store.pop(action_key, None)

    def record_unknown_outcome(self, action_key: str) -> None:
        """Preserves action state as pending for retry after timeout / network failure."""
        store = self._get_store()
        if action_key in store:
            store[action_key]["status"] = "pending"

    def handle_conflict(self, action_key: str, conflict_error: ConflictError) -> None:
        """
        Handles 409 ConflictError.
        If code is 'ROW_VERSION_CONFLICT', specifically marks the stale action as terminated,
        discards stale key, and requires reload so the next save uses fresh state.
        """
        store = self._get_store()
        if conflict_error.code == "ROW_VERSION_CONFLICT":
            store[action_key] = {
                "status": "conflict_reload_required",
                "error_message": conflict_error.message,
            }
        else:
            # Other conflict (e.g. IDEMPOTENCY_KEY_REUSE or resource conflict)
            self.complete_action(action_key)

    def clear_action(self, action_key: str) -> None:
        """Explicitly discards an action (e.g. on user reload / reset)."""
        store = self._get_store()
        store.pop(action_key, None)

    def is_conflict_reload_required(self, action_key: str) -> bool:
        """Checks if a previous attempt resulted in ROW_VERSION_CONFLICT."""
        store = self._get_store()
        action = store.get(action_key)
        return bool(action and action.get("status") == "conflict_reload_required")

    def execute_mutation(
        self,
        action_key: str,
        operation: str,
        payload: Any,
        mutation_fn: Callable[[str], Any],
    ) -> Any:
        """
        Executes a mutation callable under the managed idempotency lifecycle.
        Automatically handles unknown-outcome retries and ROW_VERSION_CONFLICT.
        """
        key = self.get_or_create_action(action_key, payload, operation)
        try:
            result = mutation_fn(key)
            self.complete_action(action_key)
            return result
        except (TimeoutError, BackendUnavailableError):
            self.record_unknown_outcome(action_key)
            raise
        except ConflictError as ex:
            self.handle_conflict(action_key, ex)
            raise
        except Exception:
            self.complete_action(action_key)
            raise
