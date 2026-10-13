"""Synthetic, in-memory phone acceptance API. No database, Gemini or production access.

python3 scripts/shortcut_acceptance_server.py --bind 0.0.0.0
Open http://<Mac LAN IP>:8089 in a browser to choose a scenario and see counters.
Use only synthetic screenshots/notes; receipts disappear when the server exits.
"""
import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
from threading import RLock
from uuid import uuid4

TEST_TOKEN = "vibeledger-local-acceptance-only"
SCENARIOS = ("auto", "missing", "revise", "installment", "lost_response", "processing")
spec = importlib.util.spec_from_file_location("expense_review", Path(__file__).resolve().parents[1] /
    "ai-ledger-backend/app/services/expense_review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class State:
    def __init__(self):
        self.lock = RLock()
        self.scenario = "auto"
        self.receipts = {}
        self.counts = {"uploads": 0, "revisions": 0, "confirms": 0, "recoveries": 0, "transactions": 0}

    def view(self, row):
        if row["status"] == "needs_confirmation":
            return {"status": row["status"], "request_id": row["id"], "row_version": row["version"],
                    "draft": deepcopy(row["draft"]), **review.presentation(row["draft"])}
        if row["status"] == "processing":
            return {"status": "processing", "request_id": row["id"], "row_version": row["version"],
                    "display_summary": "识别正在处理中，请保留本次请求，稍后运行以恢复。",
                    "confirmation_ready": False, "next_action": "recover", "supplement_prompt": ""}
        return {"status": row["status"], "request_id": row["id"], "row_version": row["version"],
                "display_summary": review.summary(row["draft"], saved=True) if row["status"] == "committed" else "已取消本次记账。",
                **review.terminal_fields()}

    def capture(self, payload):
        key = payload["idempotency_key"]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if key in self.receipts:
                row = self.receipts[key]
                if row.get("hash") not in (None, digest):
                    return {"error": {"code": "IDEMPOTENCY_KEY_REUSE"}}, False
                return self.view(row), False
            self.counts["uploads"] += 1
            draft = {"original_amount": "28.50", "original_currency": "CNY", "merchant": "测试商户",
                     "category": {"name": "测试分类"}, "from_account": {"name": "测试银行卡 1234"},
                     "occurred_on": "2026-10-01", "payment_mode": "one_off", "remarks": payload.get("note") or None,
                     "confirmation_ready": True, "warnings": []}
            status = "committed"
            if self.scenario in ("missing", "revise"):
                status = "needs_confirmation"
                if self.scenario == "missing":
                    draft.update(original_amount=None, occurred_on=None, from_account=None, confirmation_ready=False,
                                 warnings=[{"code": k} for k in ("INVALID_AMOUNT", "INVALID_DATE", "MISSING_ACCOUNT")])
                else:
                    draft["warnings"] = [{"code": "UNCERTAIN_AMOUNT"}]
            elif self.scenario == "installment":
                status = "needs_confirmation"
                draft.update(payment_mode="installment", total_periods=12, confirmation_ready=False)
            elif self.scenario == "processing":
                status = "processing"
            row = {"id": str(uuid4()), "version": 1, "draft": draft, "status": status, "hash": digest}
            self.receipts[key] = row
            self.counts["transactions"] += status == "committed"
            return self.view(row), self.scenario == "lost_response"

    def by_key(self, key, cancel=False):
        with self.lock:
            row = self.receipts.get(key)
            if not row:
                if not cancel:
                    return {"error": {"code": "REQUEST_NOT_FOUND"}}
                row = {"id": str(uuid4()), "version": 1, "draft": {}, "status": "rejected", "hash": None}
                self.receipts[key] = row
            if cancel and row["status"] not in ("committed", "rejected", "failed"):
                row["status"] = "rejected"
            self.counts["recoveries"] += not cancel
            return self.view(row)

    def action(self, identity, operation, payload):
        with self.lock:
            row = next((r for r in self.receipts.values() if r["id"] == identity), None)
            if not row:
                return {"error": {"code": "REQUEST_NOT_FOUND"}}
            if row["status"] in ("committed", "rejected", "failed"):
                return self.view(row)
            if int(payload.get("expected_version", -1)) != row["version"]:
                return {"error": {"code": "DRAFT_CHANGED"}}
            if operation == "revise":
                if not str(payload.get("correction_note", "")).strip():
                    return {"error": {"code": "INVALID_REQUEST"}}
                # Fixed synthetic outcome: this test service does not parse natural language.
                row["draft"].update(original_amount="38.50", occurred_on="2026-10-01",
                    from_account={"name": "测试银行卡 1234"}, confirmation_ready=True, warnings=[])
                self.counts["revisions"] += 1
            elif operation == "confirm":
                self.counts["confirms"] += 1
                if row["draft"]["confirmation_ready"]:
                    row["status"] = "committed"
                    self.counts["transactions"] += 1
            elif operation == "reject":
                row["status"] = "rejected"
            row["version"] += 1
            return self.view(row)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # No paths, headers, images or notes in logs.

    def send_json(self, result):
        # Always HTTP 200 in this mock; real 4xx/5xx iOS behavior is a separate test boundary.
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = ("<!doctype html><meta charset=utf-8><title>Shortcut 本地验收</title><h1>仅测试数据</h1>"
                "<p>选择场景前，先在手机取消上一个未结束请求。计数累计，不读取生产配置。</p>" +
                "".join(f'<form method=post action=/scenario/{s}><button>{s}</button></form>' for s in SCENARIOS) +
                '<p><a href=/metrics>查看计数</a> · <a href=/sample>手机样例账单</a></p>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/sample":
            body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                    "<title>合成账单</title><h1>仅供 Shortcut 本地验收</h1><p>测试商户</p><h2>¥28.50</h2>"
                    "<p>测试分类 · 测试银行卡 1234</p><p>2026-10-01 · 一次性付款</p>"
                    "<p>无真实交易；此服务不做 OCR 或 Gemini 识别。</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/metrics":
            with self.server.state.lock:
                self.send_json({"scenario": self.server.state.scenario, **self.server.state.counts})
            return
        if not self.authorized():
            return
        prefix = "/api/v1/ingestion-requests/by-key/"
        if self.path.startswith(prefix):
            self.send_json(self.server.state.by_key(self.path[len(prefix):]))
        else:
            self.send_json({"error": {"code": "REQUEST_NOT_FOUND"}})

    def authorized(self):
        if self.headers.get("Authorization") == "Bearer " + TEST_TOKEN:
            return True
        self.send_json({"error": {"code": "TEST_TOKEN_REQUIRED"}})
        return False

    def do_POST(self):
        if self.path.startswith("/scenario/"):
            scenario = self.path.removeprefix("/scenario/")
            if scenario in SCENARIOS:
                with self.server.state.lock:
                    self.server.state.scenario = scenario
            self.send_response(303)
            self.send_header("Location", "/metrics")
            self.end_headers()
            return
        if not self.authorized():
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 8_000_000:
            self.send_json({"error": {"code": "TEST_INPUT_TOO_LARGE"}})
            return
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v1/expenses":
            result, drop = self.server.state.capture(payload)
            if drop:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
        elif self.path.startswith("/api/v1/ingestion-requests/by-key/") and self.path.endswith("/cancel"):
            result = self.server.state.by_key(self.path.split("/")[-2], cancel=True)
        else:
            identity, operation = self.path.split("/")[-2:]
            result = self.server.state.action(identity, operation, payload)
        self.send_json(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.state = State()
    print(f"Synthetic acceptance service on port {args.port}. No production calls. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
