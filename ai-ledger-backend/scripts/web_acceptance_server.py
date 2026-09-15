"""Local browser acceptance fixture: real API/JWT/DB, deterministic PDF parser.

Never imported by the product. Requires an explicitly local test database.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
url = urlsplit(os.environ.get("DATABASE_URL", ""))
if os.environ.get("ENVIRONMENT") != "test" or url.hostname not in ("localhost", "127.0.0.1") or "test" not in url.path:
    raise RuntimeError("Use ENVIRONMENT=test and a local test database.")
os.environ["ENV_FILE_PATH"] = str(Path(__file__).with_name("nonexistent-test.env"))
os.environ["AUTH_PUBLIC_KEY"] = "web-acceptance-only-not-a-production-signing-key"
os.environ["AUTH_ISSUER"] = "https://fixture.supabase.co/auth/v1"
os.environ["AUTH_AUDIENCE"] = "authenticated"
os.environ["AUTH_ALGORITHMS"] = '["HS256"]'
os.environ.pop("AUTH_JWKS_URL", None)

import jwt
import uvicorn
from app import config
from tests.support.db_helper import create_test_schema, drop_test_schema
from app.db import get_connection
from app.repositories import simplified_schema as schema

test_schema = create_test_schema()
config.settings.DB_SCHEMA = test_schema
os.environ["DB_SCHEMA"] = test_schema
user_id, household_id = uuid4(), uuid4()
try:
    conn = get_connection(test_schema)
    schema.create_household(conn, "浏览器验收测试家庭", household_id=household_id)
    schema.create_user(conn, str(user_id), "test@example.com", "验收用户", user_id=user_id)
    schema.add_household_member(conn, household_id, user_id, "owner")
    schema.create_category(conn, household_id, "其他", "expense", is_fallback=True)
    category = schema.create_category(conn, household_id, "日常", "expense")
    account = schema.create_account(conn, household_id, "验收钱包", "仅测试钱包", "cash", "CNY", opened_on=datetime.now(timezone.utc).date())
    with conn.cursor() as cur:
        cur.execute("UPDATE accounts SET statement_import_enabled=true WHERE id=%s", (account["id"],))
    conn.commit()
    conn.close()

    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode({"sub": str(user_id), "iss": os.environ["AUTH_ISSUER"], "aud": "authenticated", "iat": now, "exp": now + 3600}, os.environ["AUTH_PUBLIC_KEY"], algorithm="HS256")
    fixture_path = Path(__file__).resolve().parents[2] / "ai-ledger-web/live-fixture.local.json"
    fixture_path.write_text(json.dumps({"account_id": str(account["id"]), "category_id": str(category["id"]), "session": {
        "access_token": token, "refresh_token": "fixture-refresh", "expires_in": 3600, "expires_at": now + 3600,
        "token_type": "bearer", "user": {"id": str(user_id), "aud": "authenticated", "email": "test@example.com", "app_metadata": {}, "user_metadata": {}}
    }}), encoding="utf-8")

    from app.main import create_app
    from app.api.routes.statements import get_statement_parser
    from app.api.routes.transactions import get_fx_provider

    class Parser:
        def parse(self, *_):
            day = datetime.now(timezone.utc).date().isoformat()
            return {"account_hint": "验收钱包", "account_currency": "CNY", "account_confidence": .99,
                "period_start": day, "period_end": day, "complete": True, "processed_pages": [1], "actual_page_count": 1, "expected_line_count": 42,
                "lines": [{"occurred_on": day, "amount": "-100.00" if i == 40 else "-10.00", "currency": "CNY",
                    "merchant": f"验收商户 {i+1}", "kind": "repayment" if i == 40 else "refund" if i == 41 else "expense", "category": "日常",
                    "confidence": {k: .99 for k in ("amount", "currency", "date", "intent", "category")}} for i in range(42)]}

    app = create_app()
    app.dependency_overrides[get_statement_parser] = lambda: Parser()
    app.dependency_overrides[get_fx_provider] = lambda: None
    uvicorn.run(app, host="127.0.0.1", port=8019, log_level="warning")
finally:
    drop_test_schema(test_schema)
