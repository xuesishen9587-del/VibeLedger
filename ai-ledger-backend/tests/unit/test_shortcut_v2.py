"""Workflow contract simulation; deliberately not proof of Apple's iOS runtime."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import plistlib
from http.client import RemoteDisconnected
from threading import Thread
from unittest import TestCase
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
import json


def module(name):
    path = Path(__file__).resolve().parents[3] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


shortcut = module("shortcut_v2")
mock_api = module("shortcut_acceptance_server")
P = shortcut.PREFIX


def fixture():
    """Synthetic source layout; contains no production metadata or private export."""
    actions = [{"WFWorkflowActionIdentifier": P + "nothing", "WFWorkflowActionParameters": {"UUID": str(i)}} for i in range(1, 177)]
    def action(index, name, **params):
        actions[index-1] = {"WFWorkflowActionIdentifier": P + name, "WFWorkflowActionParameters": {"UUID": str(index), **params}}
    def out(i):
        return {"Value": {"Type": "ActionOutput", "OutputUUID": str(i)}}
    action(1, "gettext", WFTextActionText="http://test.invalid")
    action(2, "setvariable", WFVariableName="BASE_URL", WFInput=out(1))
    action(3, "file", WFFile="SYNTHETIC_TOKEN_FILE")
    action(4, "detect.text", WFInput=out(3))
    action(5, "setvariable", WFVariableName="DEVICE_TOKEN", WFInput=out(4))
    action(6, "file.getfoldercontents", WFFolder="SYNTHETIC_FOLDER")
    action(7, "filter.files", WFContentItemFilter={"Value": {"WFActionParameterFilterTemplates": [{"Values": {"String": "pending-key"}}]}})
    action(8, "count", Input=out(7))
    action(82, "date")
    action(83, "format.date", WFDate=out(82))
    action(84, "number.random")
    action(85, "gettext", WFTextActionText="synthetic-key-1234")
    action(86, "setvariable", WFVariableName="NEW_KEY", WFInput=out(85))
    action(87, "setitemname", WFInput=shortcut.token("NEW_KEY"), WFName="pending-key.txt")
    action(88, "documentpicker.save", WFInput=out(87))
    action(89, "setvariable", WFVariableName="PENDING_FILE", WFInput=out(88))
    action(90, "takescreenshot")
    action(91, "date")
    action(92, "format.date", WFDate=out(91))
    action(93, "setvariable", WFVariableName="CAPTURED_AT", WFInput=out(92))
    action(94, "image.resize", WFImage=out(90))
    action(95, "image.convert", WFInput=out(94))
    action(96, "base64encode", WFInput=out(95))
    action(97, "setvariable", WFVariableName="IMAGE_BASE64", WFInput=out(96))
    action(98, "url", WFURLActionURL=shortcut.token_text(("BASE_URL",), "/api/v1/expenses"))
    # Non-private golden parameter shape transcribed from working production action 99.
    action(99, "downloadurl", WFURL={"Value": {"string": "\ufffc", "attachmentsByRange": {
        "{0, 1}": {"OutputUUID": "98", "Type": "ActionOutput", "OutputName": "URL"}}},
        "WFSerializationType": "WFTextTokenString"},
        WFHTTPHeaders=shortcut.json_fields({"Authorization": ("DEVICE_TOKEN",)}),
           WFJSONValues=shortcut.json_fields({"idempotency_key": ("NEW_KEY",), "captured_at": ("CAPTURED_AT",),
               "client_version": "old", "image": ("IMAGE_BASE64",)}))
    action(155, "downloadurl")
    return {"WFWorkflowActions": actions}


class Halt(Exception):
    pass


class Simulator:
    def __init__(self, workflow, state, answers=(), choices=(), files=None):
        shortcut.validate_http_bindings(workflow)
        shortcut.validate_condition_bindings(workflow)
        self.actions = workflow["WFWorkflowActions"]
        self.state, self.answers, self.choices = state, iter(answers), iter(choices)
        self.files = {} if files is None else files
        self.vars, self.outputs, self.summaries, self.menus, self.requests = {}, {}, [], [], []
        self.screenshots = 0

    def value(self, value):
        if isinstance(value, dict):
            if value.get("Type") == "Variable" and "Variable" in value:
                return self.value(value["Variable"])
            if "Value" in value:
                return self.value(value["Value"])
            if value.get("Type") == "Variable":
                return self.vars.get(value["VariableName"], "")
            if value.get("Type") == "ActionOutput":
                return self.outputs.get(value["OutputUUID"], "")
            if "string" in value:
                result = value["string"]
                for key, attachment in sorted(value.get("attachmentsByRange", {}).items(), reverse=True):
                    offset = int(key.split(",")[0][1:])
                    result = result[:offset] + str(self.value(attachment)) + result[offset+1:]
                return result
        return value

    def fields(self, params):
        return {self.value(i["WFKey"]): self.value(i["WFValue"]) for i in params["Value"]["WFDictionaryFieldValueItems"]}

    def block(self, start, end):
        i = start
        while i < end:
            a = self.actions[i]; p = a["WFWorkflowActionParameters"]; name = a["WFWorkflowActionIdentifier"][len(P):]
            result = None
            if p.get("WFControlFlowMode") == 0:
                group = p["GroupingIdentifier"]
                finish = next(j for j in range(i+1, end) if self.actions[j]["WFWorkflowActionParameters"].get("GroupingIdentifier") == group and self.actions[j]["WFWorkflowActionParameters"].get("WFControlFlowMode") == 2)
                splits = [j for j in range(i+1, finish) if self.actions[j]["WFWorkflowActionParameters"].get("GroupingIdentifier") == group and self.actions[j]["WFWorkflowActionParameters"].get("WFControlFlowMode") == 1]
                if name == "repeat.count":
                    for _ in range(p["WFRepeatCount"]):
                        self.block(i+1, finish)
                elif name == "conditional":
                    left = self.value(p["WFInput"])
                    code = p["WFCondition"]
                    if code in (100, 101):
                        present = left is not None and left != ""
                        yes = present if code == 100 else not present
                    elif code == 2:
                        yes = left > float(p["WFNumberValue"])
                    else:
                        yes = left == p["WFConditionalActionString"]
                    split = splits[0] if splits else finish
                    if yes: self.block(i+1, split)
                    elif splits: self.block(split+1, finish)
                elif name == "choosefrommenu":
                    prompt = self.value(p["WFMenuPrompt"])
                    self.menus.append((prompt, p["WFMenuItems"]))
                    choice = next(self.choices)
                    if choice not in p["WFMenuItems"]: raise AssertionError(f"Unavailable choice: {choice}")
                    index = p["WFMenuItems"].index(choice)
                    self.block(splits[index]+1, (splits+[finish])[index+1])
                i = finish+1
                continue
            if name == "gettext": result = self.value(p["WFTextActionText"])
            elif name == "setvariable": self.vars[p["WFVariableName"]] = self.value(p["WFInput"])
            elif name == "file": result = "synthetic-token"
            elif name in ("file.getfoldercontents", "filter.files"): result = list(self.files)
            elif name == "count": result = len(self.value(p["Input"]))
            elif name == "getitemfromlist": result = self.value(p["WFInput"])[0]
            elif name == "detect.text":
                inp = self.value(p["WFInput"])
                inp = self.files.get(inp, inp) if isinstance(inp, str) else inp
                result = "" if inp is None else str(inp)
            elif name == "number": result = float(self.value(p["WFNumberActionNumber"]))
            elif name in ("image.resize", "image.convert", "base64encode", "detect.dictionary"): result = self.value(p.get("WFInput", p.get("WFImage")))
            elif name == "date" or name == "format.date": result = "2026-10-01T12:00:00+08:00"
            elif name == "number.random": result = 123456
            elif name == "takescreenshot": self.screenshots += 1; result = "synthetic-image"
            elif name == "ask": result = next(self.answers)
            elif name == "setitemname": result = p["WFName"]; self.files[result] = self.value(p["WFInput"])
            elif name == "documentpicker.save": result = self.value(p["WFInput"])
            elif name == "file.delete":
                if p.get("WFDeleteImmediatelyDelete") is not True:
                    raise AssertionError("Pending cleanup requires native Delete Immediately=true")
                self.files.pop(self.value(p["WFInput"]))
            elif name == "previewdocument": self.summaries.append(self.value(p["WFInput"]))
            elif name == "url": result = self.value(p["WFURLActionURL"])
            elif name == "getvalueforkey":
                source = self.value(p["WFInput"]); result = source.get(p["WFDictionaryKey"], "") if isinstance(source, dict) else ""
            elif name == "downloadurl":
                url = self.value(p["WFURL"]); method = p.get("WFHTTPMethod", "GET")
                body = self.fields(p["WFJSONValues"]) if "WFJSONValues" in p else {}
                self.requests.append((method, url, deepcopy(body)))
                if url.endswith("/expenses"):
                    result, drop = self.state.capture(body)
                    if drop: raise OSError("simulated lost response")
                elif "/by-key/" in url:
                    result = self.state.by_key(url.split("/by-key/")[1].removesuffix("/cancel"), cancel=url.endswith("/cancel"))
                else:
                    identity, operation = url.split("/")[-2:]; result = self.state.action(identity, operation, body)
            elif name == "exit": raise Halt()
            else: raise AssertionError(f"Unhandled simulated action: {name}")
            self.outputs[p["UUID"]] = result
            i += 1

    def run(self):
        try: self.block(0, len(self.actions))
        except Halt: pass
        return self


class ShortcutFlowTests(TestCase):
    def setUp(self):
        self.workflow = shortcut.build(fixture())
        self.state = mock_api.State()

    def test_auto_save_one_upload_and_empty_note(self):
        flow = Simulator(self.workflow, self.state, answers=[""]).run()
        self.assertEqual(len(flow.requests), 1)
        self.assertEqual(flow.requests[0][2]["note"], "")
        self.assertFalse(flow.menus)
        self.assertFalse(flow.files)
        self.assertEqual(flow.screenshots, 1)
        self.assertIn("金额：28.50 CNY", flow.summaries[0])

    def test_note_is_not_correction_and_summary_has_six_fields(self):
        flow = Simulator(self.workflow, self.state, answers=["测试备注"]).run()
        self.assertEqual(flow.requests[0][2]["note"], "测试备注")
        self.assertNotIn("correction_note", flow.requests[0][2])
        for label in ("金额", "商户", "分类", "支付账户", "日期", "分期", "备注：测试备注"):
            self.assertIn(label, flow.summaries[0])
        self.assertIsInstance(flow.summaries[0], str)

    def test_missing_supplement_loops_back_and_confirms_latest_version(self):
        self.state.scenario = "missing"
        flow = Simulator(self.workflow, self.state, answers=["原备注", "38.50元，2026年10月1日，测试银行卡1234"],
                         choices=["补充信息", "确认保存"]).run()
        self.assertNotIn("确认保存", flow.menus[0][1])
        self.assertIn("金额", flow.menus[0][0])
        self.assertIn("金额：38.50 CNY", flow.menus[1][0])
        self.assertEqual([r[1].split("/")[-1] for r in flow.requests], ["expenses", "revise", "confirm"])
        self.assertEqual(set(flow.requests[1][2]), {"expected_version", "correction_note"})
        self.assertEqual(flow.requests[2][2]["expected_version"], "2")
        self.assertIn("备注：原备注", flow.summaries[0])
        self.assertEqual(self.state.counts["transactions"], 1)
        self.assertFalse(flow.files)

    def test_valid_low_confidence_draft_still_offers_explicit_confirmation(self):
        self.state.scenario = "revise"
        flow = Simulator(self.workflow, self.state, answers=["", "修改金额"], choices=["补充信息", "确认保存"]).run()
        self.assertIn("确认保存", flow.menus[0][1])
        self.assertEqual(self.state.counts["uploads"], 1)
        self.assertEqual(self.state.counts["revisions"], 1)

    def test_empty_correction_does_not_call_server(self):
        self.state.scenario = "missing"
        flow = Simulator(self.workflow, self.state, answers=["", ""], choices=["补充信息", "取消"]).run()
        self.assertEqual(self.state.counts["revisions"], 0)
        self.assertEqual(len(flow.requests), 2)
        self.assertFalse(flow.files)

    def test_installment_only_shows_notice_and_web_or_cancel(self):
        self.state.scenario = "installment"
        flow = Simulator(self.workflow, self.state, answers=[""], choices=["稍后在网页处理"]).run()
        self.assertIn("检测到分期，请在网页端处理。", flow.menus[0][0])
        self.assertNotIn("确认保存", flow.menus[0][1])
        self.assertIn("取消", flow.menus[0][1])
        self.assertTrue(flow.files)
        self.assertEqual(self.state.counts["transactions"], 0)

    def test_lost_response_recovers_original_key_without_reupload(self):
        self.state.scenario = "lost_response"
        files = {}
        flow = Simulator(self.workflow, self.state, answers=[""], files=files)
        with self.assertRaises(OSError): flow.run()
        self.assertTrue(files)
        recovered = Simulator(self.workflow, self.state, files=files).run()
        self.assertEqual(len(recovered.requests), 1)
        self.assertEqual(recovered.requests[0][0], "GET")
        self.assertFalse(files)
        self.assertEqual(recovered.screenshots, 0)
        self.assertEqual(self.state.counts["uploads"], 1)
        self.assertEqual(self.state.counts["transactions"], 1)

    def test_not_found_uses_tombstone_and_processing_can_be_cancelled(self):
        files = {"pending-key.txt": "synthetic-key-1234"}
        flow = Simulator(self.workflow, self.state, files=files).run()
        self.assertEqual([r[0] for r in flow.requests], ["GET", "POST"])
        self.assertTrue(flow.requests[1][1].endswith("/cancel"))
        self.assertFalse(files)
        saved, _ = self.state.capture({"idempotency_key": "synthetic-key-1234", "note": ""})
        self.assertEqual(saved["status"], "rejected")
        self.state = mock_api.State(); self.state.scenario = "processing"
        flow = Simulator(self.workflow, self.state, answers=[""], choices=["取消"]).run()
        self.assertEqual(self.state.counts["transactions"], 0)
        self.assertFalse(flow.files)

    def test_acceptance_is_separate_and_binary_roundtrips(self):
        candidate = shortcut.build(fixture(), acceptance=True)
        self.assertEqual(plistlib.loads(plistlib.dumps(candidate)), candidate)
        actions = candidate["WFWorkflowActions"]
        self.assertEqual(actions[2]["WFWorkflowActionIdentifier"], P + "gettext")
        self.assertNotIn("WFFile", actions[2]["WFWorkflowActionParameters"])
        self.assertEqual(actions[2]["WFWorkflowActionParameters"]["WFTextActionText"], shortcut.TEST_TOKEN)
        filenames = [a["WFWorkflowActionParameters"].get("WFName") for a in actions]
        self.assertIn("vibeledger-v2-acceptance-key.txt", filenames)
        self.assertNotIn("pending-key.txt", filenames)

    def test_terminal_cleanup_explicitly_deletes_pending_immediately(self):
        for acceptance in (False, True):
            with self.subTest(acceptance=acceptance):
                workflow = shortcut.build(fixture(), acceptance=acceptance)
                actions = plistlib.loads(plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY))["WFWorkflowActions"]
                deletes = [a["WFWorkflowActionParameters"] for a in actions
                           if a["WFWorkflowActionIdentifier"] == P + "file.delete"]
                self.assertEqual(len(deletes), 3)  # committed, rejected, failed only
                for params in deletes:
                    self.assertIs(params.get("WFDeleteImmediatelyDelete"), True)
                    self.assertEqual(params["WFInput"], shortcut.token("PENDING_FILE"))

    def test_every_http_uses_production_native_url_parameter_shape(self):
        expected = fixture()["WFWorkflowActions"][98]["WFWorkflowActionParameters"]["WFURL"]
        for acceptance in (False, True):
            workflow = shortcut.build(fixture(), acceptance=acceptance)
            workflow = plistlib.loads(plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY))
            endpoints = set()
            for index, action in enumerate(workflow["WFWorkflowActions"]):
                if action["WFWorkflowActionIdentifier"] != P + "downloadurl":
                    continue
                url = deepcopy(action["WFWorkflowActionParameters"]["WFURL"])
                producer = workflow["WFWorkflowActions"][index-1]
                uuid = producer["WFWorkflowActionParameters"]["UUID"]
                self.assertEqual(url["Value"]["attachmentsByRange"]["{0, 1}"]["OutputUUID"], uuid)
                url["Value"]["attachmentsByRange"]["{0, 1}"]["OutputUUID"] = "98"
                self.assertEqual(url, expected)
                endpoints.add(producer["WFWorkflowActionParameters"]["WFURLActionURL"]["Value"]["string"])
            self.assertEqual(endpoints, {"\ufffc/expenses", "\ufffc/ingestion-requests/by-key/\ufffc",
                "\ufffc/ingestion-requests/by-key/\ufffc/cancel", "\ufffc/ingestion-requests/\ufffc/reject",
                "\ufffc/ingestion-requests/\ufffc/confirm", "\ufffc/ingestion-requests/\ufffc/revise"})

    def test_native_validator_rejects_old_binding_even_with_correct_uuid(self):
        for wrong in ("bare_attachment", "wrong_output_name", "text_producer"):
            with self.subTest(wrong=wrong):
                workflow = deepcopy(self.workflow)
                index = next(i for i, a in enumerate(workflow["WFWorkflowActions"]) if a["WFWorkflowActionIdentifier"] == P + "downloadurl")
                action = workflow["WFWorkflowActions"][index]["WFWorkflowActionParameters"]
                if wrong == "bare_attachment":
                    action["WFURL"] = {"Value": action["WFURL"]["Value"]["attachmentsByRange"]["{0, 1}"],
                                       "WFSerializationType": "WFTextTokenAttachment"}
                elif wrong == "wrong_output_name":
                    action["WFURL"]["Value"]["attachmentsByRange"]["{0, 1}"]["OutputName"] = "Result"
                else:
                    workflow["WFWorkflowActions"][index-1]["WFWorkflowActionIdentifier"] = P + "gettext"
                with self.assertRaisesRegex(ValueError, "native URL action binding"):
                    Simulator(workflow, self.state)

    def test_all_string_conditions_match_production_get_text_then_if_shape(self):
        # Non-private shape of production action 50, with UUID and literal normalized.
        expected = {"WFControlFlowMode": 0, "WFCondition": 4, "WFConditionalActionString": "LITERAL",
            "WFInput": {"Type": "Variable", "Variable": {"Value": {"Type": "ActionOutput",
                "OutputUUID": "TEXT_UUID", "OutputName": "Text"}, "WFSerializationType": "WFTextTokenAttachment"}}}
        literals, named_inputs = set(), set()
        for acceptance in (False, True):
            workflow = shortcut.build(fixture(), acceptance=acceptance)
            actions = plistlib.loads(plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY))["WFWorkflowActions"]
            for index, action in enumerate(actions):
                p = action["WFWorkflowActionParameters"]
                if action["WFWorkflowActionIdentifier"] != P + "conditional" or p.get("WFControlFlowMode") != 0:
                    continue
                if p["WFCondition"] not in (4, 100, 101):
                    continue
                producer = actions[index-1]
                self.assertEqual(producer["WFWorkflowActionIdentifier"], P + "detect.text")
                ref = p["WFInput"]["Variable"]["Value"]
                self.assertEqual(ref["OutputUUID"], producer["WFWorkflowActionParameters"]["UUID"])
                self.assertEqual(ref["OutputName"], "Text")
                source = producer["WFWorkflowActionParameters"]["WFInput"]["Value"]
                if source.get("Type") == "Variable": named_inputs.add(source["VariableName"])
                if p["WFCondition"] == 4:
                    literals.add(p["WFConditionalActionString"])
                    normalized = {k: deepcopy(v) for k, v in p.items() if k not in ("UUID", "GroupingIdentifier")}
                    normalized["WFConditionalActionString"] = "LITERAL"
                    normalized["WFInput"]["Variable"]["Value"]["OutputUUID"] = "TEXT_UUID"
                    self.assertEqual(normalized, expected)
                else:
                    self.assertNotIn("WFConditionalActionString", p)
                    self.assertNotIn("WFNumberValue", p)
        self.assertEqual(literals, {"REQUEST_NOT_FOUND", "committed", "rejected", "failed", "needs_confirmation",
                                    "review_in_web", "confirm", "supplement"})
        self.assertTrue({"STATUS", "NEXT_ACTION", "NOTE", "CORRECTION_NOTE"} <= named_inputs)

    def test_condition_validator_rejects_native_type_and_operand_mismatches(self):
        mutations = ("raw_dictionary", "generic_result", "missing_literal", "empty_literal", "numeric_operand_on_text",
                     "text_operand_on_number", "old_numeric_equality", "literal_on_presence", "missing_input_wrapper")
        for wrong in mutations:
            with self.subTest(wrong=wrong):
                workflow = deepcopy(self.workflow)
                code = 2 if wrong in ("text_operand_on_number", "old_numeric_equality") else 101 if wrong == "literal_on_presence" else 4
                index = next(i for i, a in enumerate(workflow["WFWorkflowActions"]) if a["WFWorkflowActionIdentifier"] == P + "conditional"
                    and a["WFWorkflowActionParameters"].get("WFControlFlowMode") == 0 and a["WFWorkflowActionParameters"]["WFCondition"] == code)
                p = workflow["WFWorkflowActions"][index]["WFWorkflowActionParameters"]
                if wrong == "raw_dictionary":
                    # The UUID still exists, but its producer is Get Dictionary Value.
                    p["WFInput"]["Variable"] = deepcopy(workflow["WFWorkflowActions"][index-1]["WFWorkflowActionParameters"]["WFInput"])
                elif wrong == "generic_result": p["WFInput"]["Variable"]["Value"]["OutputName"] = "Result"
                elif wrong == "missing_literal": p.pop("WFConditionalActionString")
                elif wrong == "empty_literal": p["WFConditionalActionString"] = ""
                elif wrong == "numeric_operand_on_text": p.pop("WFConditionalActionString"); p["WFNumberValue"] = "1"
                elif wrong == "text_operand_on_number": p.pop("WFNumberValue"); p["WFConditionalActionString"] = "0"
                elif wrong == "old_numeric_equality": p["WFCondition"] = 4
                elif wrong == "literal_on_presence": p["WFConditionalActionString"] = ""
                else: p["WFInput"] = p["WFInput"]["Variable"]
                with self.assertRaisesRegex(ValueError, "native typed condition"):
                    Simulator(workflow, self.state)

    def test_readiness_uses_number_greater_than_zero_and_keeps_false_guard(self):
        for flag, choice in ((True, "确认保存"), (False, "稍后恢复")):
            with self.subTest(flag=flag):
                self.state = mock_api.State(); self.state.scenario = "revise"
                capture = self.state.capture
                def response(payload):
                    result, drop = capture(payload)
                    result["confirmation_ready"] = flag
                    return result, drop
                self.state.capture = response
                flow = Simulator(self.workflow, self.state, answers=[""], choices=[choice]).run()
                self.assertEqual("确认保存" in flow.menus[0][1], flag)
                self.assertEqual(self.state.counts["transactions"], int(flag))
                self.assertEqual(bool(flow.files), not flag)
        actions = self.workflow["WFWorkflowActions"]
        numbers = [a for a in actions if a["WFWorkflowActionIdentifier"] == P + "number"]
        self.assertEqual(len(numbers), 3)
        self.assertTrue(any(a["WFWorkflowActionParameters"]["WFNumberActionNumber"]["Value"].get("VariableName") == "READY" for a in numbers))

    def test_null_optional_inputs_use_presence_checks_without_extra_calls(self):
        flow = Simulator(self.workflow, self.state, answers=[None]).run()
        self.assertEqual(flow.requests[0][2]["note"], "")
        self.state = mock_api.State(); self.state.scenario = "missing"
        flow = Simulator(self.workflow, self.state, answers=["", None], choices=["补充信息", "取消"]).run()
        self.assertEqual(self.state.counts["revisions"], 0)
        self.assertEqual(len(flow.requests), 2)

    def test_consumed_output_names_are_native_in_both_profiles(self):
        def references(value):
            if isinstance(value, dict):
                if value.get("Type") == "ActionOutput": yield value
                for item in value.values(): yield from references(item)
            elif isinstance(value, list):
                for item in value: yield from references(item)
        for acceptance in (False, True):
            workflow = shortcut.build(fixture(), acceptance=acceptance)
            names = [r.get("OutputName") for r in references(workflow)]
            self.assertNotIn("Result", names)
            self.assertTrue({"Text", "Number", "Dictionary Value", "URL", "Dictionary", "Contents of URL", "Ask for Input"} <= set(names))
            if acceptance:
                self.assertEqual(workflow["WFWorkflowActions"][1]["WFWorkflowActionParameters"]["WFInput"]["Value"]["OutputName"], "Ask for Input")
                self.assertEqual(workflow["WFWorkflowActions"][3]["WFWorkflowActionParameters"]["WFInput"]["Value"]["OutputName"], "Text")

    def test_api_prefix_is_present_once_in_all_request_paths_in_both_profiles(self):
        expected = ["/api/v1/expenses", "/api/v1/ingestion-requests/{id}/revise", "/api/v1/ingestion-requests/{id}/confirm"]
        for acceptance in (False, True):
            with self.subTest(acceptance=acceptance):
                self.state = mock_api.State(); self.state.scenario = "missing"
                answers = ["http://test.invalid/api/v1"] if acceptance else []
                workflow = shortcut.build(fixture(), acceptance=acceptance)
                flow = Simulator(workflow, self.state, answers=answers+["", "修订金额"], choices=["补充信息", "确认保存"]).run()
                identity = next(iter(self.state.receipts.values()))["id"]
                self.assertEqual([urlsplit(r[1]).path.replace(identity, "{id}") for r in flow.requests], expected)
                saved_key = next(iter(self.state.receipts))
                key_file = "vibeledger-v2-acceptance-key.txt" if acceptance else "pending-key.txt"
                flow = Simulator(workflow, self.state, answers=answers, files={key_file: saved_key}).run()
                self.assertEqual(urlsplit(flow.requests[0][1]).path, "/api/v1/ingestion-requests/by-key/"+saved_key)
                self.state = mock_api.State(); self.state.scenario = "missing"
                flow = Simulator(workflow, self.state, answers=answers+[""], choices=["取消"]).run()
                self.assertTrue(urlsplit(flow.requests[-1][1]).path.startswith("/api/v1/ingestion-requests/"))
                self.assertTrue(flow.requests[-1][1].endswith("/reject"))
                flow = Simulator(workflow, self.state, answers=answers, files={key_file: "absent-key"}).run()
                self.assertEqual([urlsplit(r[1]).path for r in flow.requests], [
                    "/api/v1/ingestion-requests/by-key/absent-key", "/api/v1/ingestion-requests/by-key/absent-key/cancel"])

    def test_stale_confirm_preserves_key_then_requires_fresh_summary_review(self):
        self.state.scenario = "revise"
        action = self.state.action
        first = True
        def changed(identity, operation, payload):
            nonlocal first
            if first and operation == "confirm":
                first = False
                row = next(iter(self.state.receipts.values()))
                row["version"] += 1
                row["draft"]["original_amount"] = "48.50"
            return action(identity, operation, payload)
        self.state.action = changed
        flow = Simulator(self.workflow, self.state, answers=[""], choices=["确认保存", "重新查询", "确认保存"]).run()
        self.assertEqual([r[0] for r in flow.requests], ["POST", "POST", "GET", "POST"])
        self.assertIn("金额：48.50 CNY", flow.menus[2][0])
        self.assertEqual(flow.requests[-1][2]["expected_version"], "2")
        self.assertEqual(self.state.counts["transactions"], 1)
        self.assertFalse(flow.files)

    def test_multiple_pending_files_prevents_upload_or_deletion(self):
        files = {"pending-key-a.txt": "key-a", "pending-key-b.txt": "key-b"}
        flow = Simulator(self.workflow, self.state, files=files).run()
        self.assertEqual(len(files), 2)
        self.assertFalse(flow.requests)
        self.assertEqual(flow.screenshots, 0)

    def test_unknown_response_is_not_shown_as_json_and_keeps_pending(self):
        def malformed(payload):
            return {"error": {"code": "unexpected", "details": "synthetic private details"}, "next_action": "done"}, False
        self.state.capture = malformed
        flow = Simulator(self.workflow, self.state, answers=[""], choices=["稍后恢复"]).run()
        self.assertTrue(flow.files)
        self.assertFalse(flow.summaries)
        self.assertNotIn("synthetic private details", flow.menus[0][0])

    def test_real_http_lost_response_then_get_returns_receipt(self):
        server = mock_api.ThreadingHTTPServer(("127.0.0.1", 0), mock_api.Handler)
        server.state = mock_api.State(); server.state.scenario = "lost_response"
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        origin = f"http://127.0.0.1:{server.server_port}/api/v1"
        try:
            request = Request(origin + "/expenses", data=json.dumps({"idempotency_key": "http-test-key", "note": ""}).encode(),
                headers={"Authorization": "Bearer " + shortcut.TEST_TOKEN, "Content-Type": "application/json"})
            with self.assertRaises((OSError, URLError, RemoteDisconnected)):
                urlopen(request, timeout=2)
            recovered = urlopen(Request(origin + "/ingestion-requests/by-key/http-test-key", headers={"Authorization": "Bearer " + shortcut.TEST_TOKEN}), timeout=2)
            with recovered:
                self.assertEqual(json.load(recovered)["status"], "committed")
            self.assertEqual(server.state.counts["uploads"], 1)
            self.assertEqual(server.state.counts["transactions"], 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_real_http_not_found_then_cancel_prevents_late_capture(self):
        server = mock_api.ThreadingHTTPServer(("127.0.0.1", 0), mock_api.Handler)
        server.state = mock_api.State()
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        origin = f"http://127.0.0.1:{server.server_port}/api/v1"
        headers = {"Authorization": "Bearer " + shortcut.TEST_TOKEN, "Content-Type": "application/json"}
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(origin + "/ingestion-requests/by-key/absent-test-key", headers=headers), timeout=2)
            with error.exception as response:
                self.assertEqual(response.code, 404)
                self.assertEqual(json.load(response)["error"]["code"], "REQUEST_NOT_FOUND")
            with urlopen(Request(origin + "/ingestion-requests/by-key/absent-test-key/cancel", data=b"{}", headers=headers), timeout=2) as response:
                self.assertEqual(json.load(response)["status"], "rejected")
            payload = json.dumps({"idempotency_key": "absent-test-key", "note": ""}).encode()
            with urlopen(Request(origin + "/expenses", data=payload, headers=headers), timeout=2) as response:
                self.assertEqual(json.load(response)["status"], "rejected")
            self.assertEqual(server.state.counts["uploads"], 0)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
