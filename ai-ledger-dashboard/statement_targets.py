"""Bounded target browsing for statement review; selected IDs survive page changes."""
from api_client import ApiError


class TargetBrowser:
    def __init__(self, state, client, path, *, lookup_parameter=None):
        self.state, self.client, self.path = state, client, path
        self.lookup_parameter = lookup_parameter

    def load(self, filters=None, *, more=False):
        filters = {k: v for k, v in (filters or {}).items() if v is not None and v != ""}
        previous = self.state.get("filters")
        if more and (previous != filters or not self.state.get("next_cursor")):
            return
        params = {**filters, "limit": 50}
        if more:
            params["cursor"] = self.state["next_cursor"]
        # Update only after success: an interrupted read does not lose existing choices.
        page = self.client.request("GET", self.path, params=params)
        items = dict(self.state.get("items", {})) if more else {}
        items.update({r["id"]: r for r in page["items"]})
        self.state.update(items=items, filters=filters, next_cursor=page.get("next_cursor"))

    def choices(self, selected_ids=()):
        result = dict(self.state.get("items", {}))
        missing = []
        for identity in sorted(set(selected_ids) - {None, ""}):
            if identity in result:
                continue
            try:
                if self.lookup_parameter:
                    page = self.client.request("GET", self.path, params={self.lookup_parameter:identity,"limit":1})
                    if page["items"]:
                        result[identity] = page["items"][0]
                    else:
                        missing.append(identity)
                else:
                    result[identity] = self.client.request("GET", self.path + "/" + identity)
            except ApiError as exc:
                if exc.status_code != 404:
                    raise
                missing.append(identity)
        return result, missing
