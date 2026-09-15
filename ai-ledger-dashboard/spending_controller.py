"""Session-local spending commands with explicit recovery of unknown outcomes."""
from uuid import uuid4
from api_client import ApiError
from settings_controller import MutationModifiedPendingError, _compute_fingerprint


class SpendingActions:
    def __init__(self, state, client):
        self.state, self.client = state, client

    def execute(self, slot, method, path, body):
        fingerprint = _compute_fingerprint(f"{method} {path}", body)
        prior = self.state.get(slot)
        if prior and prior["fingerprint"] != fingerprint:
            raise MutationModifiedPendingError("上次保存结果尚未确定，请先重试上次保存。")
        command = prior or {"key": str(uuid4()), "fingerprint": fingerprint,
                            "method": method, "path": path, "body": body}
        self.state[slot] = command
        try:
            result = self.client.request(method, path, json_data=body, headers={"Idempotency-Key": command["key"]})
        except ApiError as exc:
            if 400 <= exc.status_code < 500:
                self.state.pop(slot, None)
            raise
        self.state.pop(slot, None)
        return result

    def retry(self, slot):
        previous = self.state[slot]
        return self.execute(slot, previous["method"], previous["path"], previous["body"])
