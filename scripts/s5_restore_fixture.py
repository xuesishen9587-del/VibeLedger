"""Synthetic OPS-01 fixture, executed over stdin inside disposable backend images only."""
import hashlib
import json
import os
from datetime import date
from unittest.mock import Mock, patch
from uuid import UUID

from app.auth.context import AuthContext
from app.config import get_settings
from app.db import get_connection
from app.repositories import simplified_schema as schema, spending as repo
from app.services import spending_schedules as schedules, statement_import as statements

assert get_settings().ENVIRONMENT == "test"
assert get_settings().DB_SCHEMA == "vibeledger_test_container"
HH = UUID("11111111-1111-4111-8111-111111111111")
USER = UUID("22222222-2222-4222-8222-222222222222")
ACCOUNT = UUID("33333333-3333-4333-8333-333333333333")
actor = AuthContext("browser", USER, HH, "owner")
document = b"%PDF-synthetic-ops-01"

def fingerprint():
    with get_connection() as conn:
        result = {}
        for table in ("ingestion_requests", "statement_lines", "schedule_occurrences", "transactions", "audit_events"):
            data = repo.rows(conn, f"SELECT * FROM {table} ORDER BY id")
            assert data, table
            result[table] = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        return result

if os.environ.get("OPS_RESTORE_VERIFY") != "1":
    with get_connection() as conn:
        schema.create_household(conn, "Synthetic OPS-01", started_on=date(2026, 1, 1), household_id=HH)
        schema.create_user(conn, "synthetic-ops", "synthetic-ops@example.invalid", "Synthetic", user_id=USER)
        schema.add_household_member(conn, HH, USER, "owner")
        category = schema.create_category(conn, HH, "Other", "expense", is_fallback=True)["id"]
        schema.create_account(conn, HH, "Synthetic card", "total", "credit", "CNY",
                              opened_on=date(2026, 1, 1), statement_import_enabled=True, account_id=ACCOUNT)
        conn.commit()
        terms = dict(name="OPS monthly", kind="installment", amount_per_period="50.00", currency="CNY",
                     period_count=3, start_month="2026-01-01", day_of_month=31, merchant="Synthetic monthly",
                     category_id=str(category), acknowledged_due_through="2026-01-01")
        with patch.object(schedules, "local_today", return_value=date(2026, 1, 1)):
            schedules.create(conn, actor, "ops-schedule-create", terms)
    parser = Mock(unique_id_namespace=None)
    parser.parse.return_value = dict(account_hint="Synthetic card", account_currency="CNY", account_confidence=.99,
        period_start="2026-02-01", period_end="2026-02-28", processed_pages=[1], actual_page_count=1,
        expected_line_count=1, complete=True, lines=[dict(occurred_on="2026-02-03", amount="12.00", currency="CNY",
        merchant="Synthetic statement", kind="expense", category="Other",
        confidence={k:.99 for k in ("amount","currency","date","intent","category")})])
    draft, _ = statements.upload(get_connection, actor, ACCOUNT, "ops-statement-upload", document, None, parser)
    saved, _ = statements.confirm(get_connection, actor, draft["request_id"], draft["row_version"])
    assert saved["status"] == "committed", saved
    with patch.object(schedules, "local_today", return_value=date(2026, 3, 31)):
        assert schedules.run_due(get_connection) == {"processed": 3}
else:
    before = fingerprint()
    with patch.object(schedules, "local_today", return_value=date(2026, 3, 31)):
        assert schedules.run_due(get_connection) == {"processed": 0}
    parser = Mock()
    result, _ = statements.upload(get_connection, actor, ACCOUNT, "ops-statement-upload", document, None, parser)
    assert result["status"] == "committed"
    assert statements.confirm(get_connection, actor, result["request_id"], 0)[0] == result
    parser.parse.assert_not_called()
    assert fingerprint() == before, "Restore replay mutated evidence"
print(json.dumps(fingerprint(), sort_keys=True))
