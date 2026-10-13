"""Transform an externally decoded production workflow. Never print its private config.

Input/output must stay outside Git. Output is unsigned; sign on a Mac before import.
The acceptance profile uses the same workflow with a synthetic token and a separate key file.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import plistlib
from uuid import uuid4

PREFIX = "is.workflow.actions."
TEST_TOKEN = "vibeledger-local-acceptance-only"


def token(name):
    return {"Value": {"Type": "Variable", "VariableName": name},
            "WFSerializationType": "WFTextTokenAttachment"}


def token_text(*parts):
    value, attachments = "", {}
    for part in parts:
        if isinstance(part, tuple):
            attachments[f"{{{len(value)}, 1}}"] = {"Type": "Variable", "VariableName": part[0]}
            value += "\ufffc"
        else:
            value += part
    return {"Value": {"string": value, **({"attachmentsByRange": attachments} if attachments else {})},
            "WFSerializationType": "WFTextTokenString"}


def json_fields(fields):
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFKey": token_text(key), "WFItemType": 0,
         "WFValue": token_text(value)}
        for key, value in fields.items()]}, "WFSerializationType": "WFDictionaryFieldValue"}


def url_input(output):
    """WFURL is a token STRING parameter, even when it holds one URL output.

    Match the working production export: a URL action's typed magic variable
    inside the string, not a bare WFTextTokenAttachment accepted by our simulator.
    """
    return {"Value": {"string": "\ufffc", "attachmentsByRange": {"{0, 1}": deepcopy(output["Value"])}},
            "WFSerializationType": "WFTextTokenString"}


def validate_http_bindings(workflow):
    """Check native parameter shape/producer type, not just UUID existence.

    This is structural validation against the production export, not an iOS runtime.
    """
    actions = workflow["WFWorkflowActions"]
    for index, action in enumerate(actions):
        if action["WFWorkflowActionIdentifier"] != PREFIX + "downloadurl":
            continue
        value = action["WFWorkflowActionParameters"].get("WFURL", {})
        state = value.get("Value", {})
        attachments = state.get("attachmentsByRange", {})
        attachment = attachments.get("{0, 1}", {})
        producer = actions[index - 1] if index else {}
        params = producer.get("WFWorkflowActionParameters", {})
        if not (value.get("WFSerializationType") == "WFTextTokenString"
                and state.get("string") == "\ufffc" and set(attachments) == {"{0, 1}"}
                and attachment.get("Type") == "ActionOutput" and attachment.get("OutputName") == "URL"
                and attachment.get("OutputUUID") == params.get("UUID")
                and producer.get("WFWorkflowActionIdentifier") == PREFIX + "url"
                and params.get("WFURLActionURL", {}).get("WFSerializationType") == "WFTextTokenString"):
            raise ValueError(f"HTTP action {index + 1} lacks a native URL action binding")


class Builder:
    def __init__(self, original):
        self.original, self.actions, self.remap = original, [], {}

    def add(self, name, **parameters):
        parameters.setdefault("UUID", str(uuid4()).upper())
        self.actions.append({"WFWorkflowActionIdentifier": PREFIX + name,
                             "WFWorkflowActionParameters": parameters})
        return {"Value": {"Type": "ActionOutput", "OutputUUID": parameters["UUID"],
                          "OutputName": "URL" if name == "url" else "Result"},
                "WFSerializationType": "WFTextTokenAttachment"}

    def clone(self, index):
        action = deepcopy(self.original[index - 1])
        params = action["WFWorkflowActionParameters"]
        if "UUID" in params:
            old = params["UUID"]
            params["UUID"] = str(uuid4()).upper()
            self.remap[old] = params["UUID"]
        def remap(value):
            if isinstance(value, dict):
                return {k: self.remap.get(v, v) if k == "OutputUUID" else remap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [remap(v) for v in value]
            return value
        action = remap(action)
        self.actions.append(action)
        return action

    def set(self, name, value):
        self.add("setvariable", WFVariableName=name, WFInput=value)

    def field(self, key, name=None, source="RESPONSE"):
        output = self.add("getvalueforkey", WFDictionaryKey=key, WFInput=token(source))
        if name:
            self.set(name, output)
        return output

    def show(self, value):
        self.add("previewdocument", WFInput=value)

    def message(self, text):
        self.show(self.add("gettext", WFTextActionText=text))

    def stop(self):
        self.add("exit")

    def condition(self, value, equals=None, *, number=None, greater=False):
        group = str(uuid4()).upper()
        params = {"GroupingIdentifier": group, "WFControlFlowMode": 0,
                  "WFInput": {"Type": "Variable", "Variable": value}, "WFCondition": 2 if greater else 4}
        if number is not None:
            params["WFNumberValue"] = str(number)
        else:
            params["WFConditionalActionString"] = equals
        self.add("conditional", **params)
        return group

    def otherwise(self, group):
        self.add("conditional", GroupingIdentifier=group, WFControlFlowMode=1)

    def end(self, group, name="conditional"):
        self.add(name, GroupingIdentifier=group, WFControlFlowMode=2)

    def http(self, suffix, fields=None, *, method="POST", initial=False):
        url = self.add("url", WFURLActionURL=token_text(("API_BASE_URL",), *suffix))
        params = {"WFURL": url_input(url), "WFHTTPHeaders": deepcopy(self.original[98]["WFWorkflowActionParameters"]["WFHTTPHeaders"])}
        if method != "GET":
            params["WFHTTPMethod"] = method
        if initial:
            params["WFHTTPBodyType"] = "JSON"
            params["WFJSONValues"] = deepcopy(self.original[98]["WFWorkflowActionParameters"]["WFJSONValues"])
            items = params["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
            for item in items:
                if item["WFKey"]["Value"]["string"] == "client_version":
                    item["WFValue"] = token_text("expense-shortcut-v2-review")
            items.extend(json_fields({"note": ("NOTE",)})["Value"]["WFDictionaryFieldValueItems"])
        elif fields is not None:
            params.update(WFHTTPBodyType="JSON", WFJSONValues=json_fields(fields))
        output = self.add("downloadurl", **params)
        self.set("RESPONSE", self.add("detect.dictionary", WFInput=output))

    def menu(self, items, prompt, callbacks):
        group = str(uuid4()).upper()
        self.add("choosefrommenu", GroupingIdentifier=group, WFControlFlowMode=0,
                 WFMenuItems=items, WFMenuPrompt=prompt)
        for title, callback in zip(items, callbacks):
            self.add("choosefrommenu", GroupingIdentifier=group, WFControlFlowMode=1, WFMenuItemTitle=title)
            callback()
        self.end(group, "choosefrommenu")

    def cancel_key(self):
        self.http(["/ingestion-requests/by-key/", ("PENDING_KEY",), "/cancel"])

    def cancel_draft(self):
        self.http(["/ingestion-requests/", ("REQUEST_ID",), "/reject"], {"expected_version": ("ROW_VERSION",)})

    def recover(self):
        self.http(["/ingestion-requests/by-key/", ("PENDING_KEY",)], method="GET")
        self.field("error", "ERROR")
        code = self.field("code", source="ERROR")
        group = self.condition(code, "REQUEST_NOT_FOUND")
        self.cancel_key()  # A tombstone, never a second upload or a new key.
        self.end(group)

    def supplement(self):
        answer = self.add("ask", WFAskActionPrompt=token_text(("SUPPLEMENT_PROMPT",)),
                          WFInputType="Text", WFAskActionDefaultAnswer="", WFAskActionAllowsMultiline=True)
        self.set("CORRECTION_NOTE", answer)
        group = self.condition(token("CORRECTION_NOTE"), "")
        # Empty correction stays local and returns to the menu.
        self.otherwise(group)
        self.http(["/ingestion-requests/", ("REQUEST_ID",), "/revise"],
                  {"expected_version": ("ROW_VERSION",), "correction_note": ("CORRECTION_NOTE",)})
        self.end(group)

    def confirm(self):
        self.http(["/ingestion-requests/", ("REQUEST_ID",), "/confirm"], {"expected_version": ("ROW_VERSION",)})

    def later(self):
        self.stop()  # Keep the pending key for the next run.


def build(workflow, *, acceptance=False):
    original = workflow["WFWorkflowActions"]
    # Refuse to silently patch a different export/layout.
    expected = {1: "gettext", 3: "file", 7: "filter.files", 90: "takescreenshot", 98: "url", 99: "downloadurl", 155: "downloadurl"}
    if len(original) != 176 or any(original[i-1]["WFWorkflowActionIdentifier"] != PREFIX + name for i, name in expected.items()):
        raise ValueError("Unsupported production export; inspect its layout before transforming.")
    b = Builder(original)
    for i in range(1, 9):
        action = b.clone(i)
        if acceptance and i == 1:
            action["WFWorkflowActionIdentifier"] = PREFIX + "ask"
            action["WFWorkflowActionParameters"] = {"UUID": action["WFWorkflowActionParameters"]["UUID"],
                "WFAskActionPrompt": "本地验收服务器地址（电脑局域网 IP，含 /api/v1）", "WFInputType": "Text",
                "WFAskActionDefaultAnswer": "http://192.168.1.100:8089/api/v1"}
        if acceptance and i == 3:
            action["WFWorkflowActionIdentifier"] = PREFIX + "gettext"
            action["WFWorkflowActionParameters"] = {"UUID": action["WFWorkflowActionParameters"]["UUID"],
                                                     "WFTextActionText": TEST_TOKEN}
        if acceptance and i == 7:
            filters = action["WFWorkflowActionParameters"]["WFContentItemFilter"]["Value"]["WFActionParameterFilterTemplates"]
            filters[0]["Values"]["String"] = "vibeledger-v2-acceptance-key"
    count = b.actions[-1]["WFWorkflowActionParameters"]["UUID"]
    count_ref = {"Value": {"Type": "ActionOutput", "OutputUUID": count, "OutputName": "Count"}, "WFSerializationType": "WFTextTokenAttachment"}
    # Production BASE_URL is an origin; the acceptance input already includes /api/v1.
    # Materialize a URL in both profiles, without changing the private production origin.
    api_url = b.add("url", WFURLActionURL=token_text(("BASE_URL",), *([] if acceptance else ["/api/v1"])))
    b.set("API_BASE_URL", api_url)
    multiple = b.condition(count_ref, number=1, greater=True)
    b.message("发现多个待处理请求文件，请先核对文件；本次未上传截图。")
    b.stop()
    b.end(multiple)
    pending = b.condition(count_ref, number=0, greater=True)
    item = b.add("getitemfromlist", WFInput={"Value": {"Type": "ActionOutput", "OutputUUID": b.remap[original[6]["WFWorkflowActionParameters"]["UUID"]], "OutputName": "Files"}, "WFSerializationType": "WFTextTokenAttachment"})
    b.set("PENDING_FILE", item)
    b.set("PENDING_KEY", b.add("detect.text", WFInput=item))
    b.recover()
    b.otherwise(pending)
    # Capture BEFORE the optional input sheet so the sheet never appears in the image.
    for i in range(90, 98):
        b.clone(i)
    b.set("NOTE", b.add("gettext", WFTextActionText=""))
    answer = b.add("ask", WFAskActionPrompt="备注（可选，留空直接继续；保存为交易备注）", WFInputType="Text",
                   WFAskActionDefaultAnswer="", WFAskActionAllowsMultiline=True)
    b.set("NOTE", answer)
    # Normalize absent input to an explicit empty JSON string.
    empty = b.condition(token("NOTE"), "")
    b.set("NOTE", b.add("gettext", WFTextActionText=""))
    b.end(empty)
    for i in range(82, 90):
        action = b.clone(i)
        if acceptance and i == 87:
            action["WFWorkflowActionParameters"]["WFName"] = "vibeledger-v2-acceptance-key.txt"
    b.set("PENDING_KEY", token("NEW_KEY"))
    b.http(["/expenses"], initial=True)
    b.end(pending)
    loop = str(uuid4()).upper()
    b.add("repeat.count", GroupingIdentifier=loop, WFControlFlowMode=0, WFRepeatCount=20)
    b.field("status", "STATUS")
    for status in ("committed", "rejected", "failed"):
        group = b.condition(token("STATUS"), status)
        b.field("display_summary", "SUMMARY")
        b.show(token("SUMMARY"))
        b.add("file.delete", WFInput=token("PENDING_FILE"))
        b.stop()
        b.end(group)
    draft = b.condition(token("STATUS"), "needs_confirmation")
    for field, name in (("request_id", "REQUEST_ID"), ("row_version", "ROW_VERSION"), ("display_summary", "SUMMARY"),
                        ("confirmation_ready", "READY"), ("next_action", "NEXT_ACTION"), ("supplement_prompt", "SUPPLEMENT_PROMPT")):
        b.field(field, name)
    web = b.condition(token("NEXT_ACTION"), "review_in_web")
    b.menu(["稍后在网页处理", "取消"], token_text(("SUMMARY",), "\n", ("SUPPLEMENT_PROMPT",)), [b.later, b.cancel_draft])
    b.otherwise(web)
    action = b.condition(token("NEXT_ACTION"), "confirm")
    ready = b.condition(token("READY"), number=1)
    b.menu(["确认保存", "补充信息", "取消"], token_text(("SUMMARY",)), [b.confirm, b.supplement, b.cancel_draft])
    b.otherwise(ready)
    b.menu(["重新查询", "稍后恢复", "取消"], "确认条件已变化，请重新查询。", [b.recover, b.later, b.cancel_key])
    b.end(ready)
    b.otherwise(action)
    supplement = b.condition(token("NEXT_ACTION"), "supplement")
    b.menu(["补充信息", "取消"], token_text(("SUMMARY",), "\n", ("SUPPLEMENT_PROMPT",)), [b.supplement, b.cancel_draft])
    b.otherwise(supplement)
    b.menu(["重新查询", "稍后恢复", "取消"], "请求结果尚未确定，请保留原请求恢复。", [b.recover, b.later, b.cancel_key])
    b.end(supplement)
    b.end(action)
    b.end(web)
    b.otherwise(draft)
    b.menu(["重新查询", "稍后恢复", "取消"], "请求尚未结束或网络返回异常，请保留原请求恢复。", [b.recover, b.later, b.cancel_key])
    b.end(draft)
    b.end(loop, "repeat.count")
    b.message("本次操作已暂停；请求仍保留，下次运行可继续。")
    result = deepcopy(workflow)
    result["WFWorkflowActions"] = b.actions
    validate_http_bindings(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Decoded private plist outside Git (not the AEA container)")
    parser.add_argument("output", type=Path, help="Unsigned .shortcut outside Git")
    parser.add_argument("--acceptance", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if any(p.resolve().is_relative_to(repo) for p in (args.input, args.output)):
        parser.error("Private workflows must stay outside the repository.")
    data = args.input.read_bytes()
    if data[:4] == b"AEA1":
        parser.error("Decode the AEA container in a trusted local environment first; do not dump it to logs.")
    result = build(plistlib.loads(data), acceptance=args.acceptance)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(plistlib.dumps(result, fmt=plistlib.FMT_BINARY))
    print(f"Generated unsigned candidate: {len(result['WFWorkflowActions'])} actions; Mac signing and iPhone acceptance required.")


if __name__ == "__main__":
    main()
