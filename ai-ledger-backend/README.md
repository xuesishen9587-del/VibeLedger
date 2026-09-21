# VibeLedger backend

FastAPI spending, monthly schedules, dated balances, whole-statement imports and
investment estimates. Product rules: [target](../TARGET_DOMAIN_MODEL.md),
[contracts](../docs/architecture/CONTRACTS.md),
[acceptance plan](../docs/architecture/IMPLEMENTATION_PLAN.md).

## Runtime

Install `requirements.txt` with Python 3.13. Set ENVIRONMENT, DATABASE_URL and DB_SCHEMA
explicitly through protected configuration. Run `python -m migrations.runner` for
the selected simplified schema, then `uvicorn app.main:app --port 7860`.
`Dockerfile` is the sole backend image definition. The root prototype and superseded
reconciliation/ledger runtime have been removed; historical migration SQL remains
in Git but is neither executable via lineage selection nor copied into the image.

`/health` is liveness. `/ready` verifies DB connectivity and exact baseline checksum;
missing Gemini configuration yields degraded readiness while manual workflows work.
Gemini defaults to `gemini-3.5-flash-lite`; all transports use bounded JSON schemas
and strict local validation. No raw prompts/responses or secrets in logs.

Browser auth uses the accepted Supabase ES256/JWKS setup. Device auth and original
Shortcut request-key recovery remain supported. The daily internal schedule endpoint
has a separate exact Google OIDC service identity, not browser/device authorization.
See [staging runbook](../docs/deployment/STAGING_DEPLOYMENT.md).

## Checks

- `python -m unittest discover -s tests/unit -p "test_*.py"`
- `python scripts/run_integration_tests.py` (disposable PostgreSQL, ENVIRONMENT=test)
- `python -m unittest discover -s tests/migration -p "test_*.py"`
- `python -m unittest discover -s tests/concurrency -p "test_*.py"`
- `python scripts/export_openapi.py --check`

Regenerate `docs/api/openapi.json` by omitting `--check`. Backend CI also builds the
image and runs actual backup/restore replay proof with synthetic records. Never run
integration cleanup against accepted staging or a shared/system schema.
