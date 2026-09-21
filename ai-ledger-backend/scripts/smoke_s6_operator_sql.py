"""Rehearse S6 SQL in a newly created local Docker PostgreSQL; no hosted DSN inputs."""
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
DB = "s6_fixture"
USER = "fixture_operator"


def main():
    name = "vibeledger-s6-sql-" + uuid.uuid4().hex[:12]
    container = subprocess.check_output(
        ["docker", "run", "--rm", "-d", "--name", name,
         "-e", "POSTGRES_PASSWORD=local-fixture-only", "-e", f"POSTGRES_USER={USER}",
         "-e", f"POSTGRES_DB={DB}", "postgres:17"], text=True
    ).strip()

    def docker(*args, data=None, ok=True):
        result = subprocess.run(["docker", *args], input=data.encode("utf-8") if data is not None else None, capture_output=True)
        if (result.returncode == 0) != ok:
            raise AssertionError((result.stdout + result.stderr).decode("utf-8"))
        return result.stdout.decode("utf-8").strip()

    def sql(query):
        return docker("exec", container, "psql", "-X", "-At", "-U", USER,
                      "-d", DB, "-v", "ON_ERROR_STOP=1", "-c", query)

    def script(file, authorization, *, ok=True, database=DB, operator=USER, data=None):
        return docker("exec", "-i", container, "psql", "-X", "-U", USER, "-d", DB,
                      "-v", "ON_ERROR_STOP=1", "-v", f"authorization={authorization}",
                      "-v", f"expected_database={database}", "-v", f"expected_operator={operator}",
                      "-f", f"/tmp/plan/docs/deployment/sql/{file}.sql", data=data, ok=ok)

    def count(table):
        return int(sql(f"SELECT count(*) FROM vibeledger_prod_v1.{table}"))

    try:
        # No host ports or network credentials: only this new container is addressed.
        docker("exec", container, "sh", "-c",
               "for i in $(seq 1 30); do pg_isready -U fixture_operator -d s6_fixture >/dev/null && exit 0; sleep 1; done; exit 1")
        for relative in ("docs/deployment/sql/s6_fresh_schema.sql", "docs/deployment/sql/s6_bootstrap.sql",
                         "ai-ledger-backend/migrations/simplified/0001_simplified.sql"):
            target = "/tmp/plan/" + relative
            docker("exec", container, "mkdir", "-p", str(Path(target).parent).replace("\\", "/"))
            docker("cp", str(ROOT / relative), f"{container}:{target}")
        sql("CREATE ROLE anon; CREATE ROLE authenticated; CREATE EXTENSION pgcrypto; CREATE EXTENSION pg_trgm;")
        create = "CREATE_NEW_VIBELEDGER_PROD_V1"
        script("s6_fresh_schema", "NOT_AUTHORIZED", ok=False)
        script("s6_fresh_schema", create, database="wrong", ok=False)
        script("s6_fresh_schema", create, operator="wrong", ok=False)
        script("s6_fresh_schema", create, ok=False)  # Missing required extension rolls back.
        assert sql("SELECT count(*) FROM pg_namespace WHERE nspname='vibeledger_prod_v1'") == "0"
        assert sql("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vibeledger_prod_%'") == "0"
        sql("CREATE EXTENSION citext")
        script("s6_fresh_schema", create)
        script("s6_fresh_schema", create, ok=False)
        sql("CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,email_confirmed_at timestamptz); "
            "INSERT INTO auth.users VALUES ('11111111-1111-4111-8111-111111111111','owner@example.invalid',now()),"
            "('22222222-2222-4222-8222-222222222222','member@example.invalid',now());")
        answers = "\n".join(("Fixture", "SGD", "2026-01-01", "11111111-1111-4111-8111-111111111111",
                             "owner@example.invalid", "Owner", "22222222-2222-4222-8222-222222222222",
                             "member@example.invalid", "Member")) + "\n"
        bootstrap = "BOOTSTRAP_NEW_VIBELEDGER_PROD_V1"
        script("s6_bootstrap", bootstrap, data=answers.replace("member@example.invalid", "wrong@example.invalid"), ok=False)
        assert count("households") == count("users") == count("categories") == 0
        script("s6_bootstrap", bootstrap, data=answers)
        script("s6_bootstrap", bootstrap, data=answers, ok=False)
        assert [count(t) for t in ("households", "users", "household_members", "categories")] == [1, 2, 2, 18]
        for table in ("devices", "accounts", "transactions", "account_snapshots", "investment_period_inputs",
                      "statement_lines", "spending_schedules", "schedule_occurrences", "ingestion_requests", "audit_events"):
            assert count(table) == 0, table
        assert sql("SELECT rolcanlogin FROM pg_roles WHERE rolname='vibeledger_prod_runtime'") == "f"
        for role in ("anon", "authenticated"):
            assert sql(f"SELECT has_schema_privilege('{role}','vibeledger_prod_v1','USAGE')") == "f"
        assert sql("SELECT has_schema_privilege('vibeledger_prod_runtime','vibeledger_prod_v1','CREATE')") == "f"
        for table, privilege, expected in (("transactions", "INSERT", "t"), ("transactions", "DELETE", "f"),
                                           ("audit_events", "INSERT", "t"), ("audit_events", "UPDATE", "f"),
                                           ("schema_migrations", "INSERT", "f")):
            assert sql(f"SELECT has_table_privilege('vibeledger_prod_runtime','vibeledger_prod_v1.{table}','{privilege}')") == expected
        assert count("schema_migrations") == 1
        print("PASS: S6 local SQL authorization, atomic refusal, bootstrap, rerun and privilege checks")
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, capture_output=True)


if __name__ == "__main__":
    main()
