# Deployment Readiness checkpoint

This bounded checkpoint prepares `experiment/astra-simplified` for isolated hosted
and real-device acceptance, starting from
`9ef170ca8d9383834e7f64184743108c22b2d902`. S1/S2 remain accepted; S3/S4 implementation
is substantially complete but hosted acceptance remains pending. S5 has not begun.

## Packaging and verification

Both backend Dockerfiles, the Dashboard image and every GitHub CI job use Python
3.13. Deploy the simplified backend with **Dockerfile.target** (`app.main:app`).
The root backend Dockerfile retains its legacy entry point; changing or removing
that entry point is outside this checkpoint.

Dashboard packaging copies the complete application directory, with a Docker ignore
file excluding local credentials, virtual environments, caches and tests. This
includes modules loaded only after login and when changing pages.

From the repository root, with Docker available:

```text
docker build -f ai-ledger-backend/Dockerfile.target -t vibeledger-backend:readiness ai-ledger-backend
docker build -t vibeledger-dashboard:readiness ai-ledger-dashboard
python scripts/container_smoke.py
```

The smoke check uses the built images without mounting application source. It
creates an isolated Docker network and PostgreSQL 17 container, applies the current
simplified migrations to a disposable test schema, starts the default backend and
Dashboard entry points, and verifies:

- Backend `/health` returns the expected service identity.
- Backend `/ready` returns HTTP 200 and `database: ok` against the migrated schema.
- Dashboard `/_stcore/health` returns `ok`.
- Every packaged top-level Dashboard runtime module imports, and a Streamlit
  AppTest session executes the packaged `app.py` and renders the login fields.
- Both running containers use Python 3.13 and remain running after the checks.

The check deliberately supplies no Gemini credential: readiness must report
`status: degraded` and `gemini: unavailable`. This validates packaging and database
readiness without claiming a live Gemini test. Fake public Auth configuration is
used only to render the login form; no sign-in or external model call occurs.
Disposable containers and the network are removed on success or failure.

GitHub's required aggregate check includes this container job alongside all five
existing test suites: backend unit, PostgreSQL integration, migration, concurrency
and Dashboard. A failed, cancelled or skipped container job fails the aggregate.

## Isolated hosted acceptance prerequisites

Local checkpoint verification passed with Python **3.13.15** in both images:
255 backend unit, 185 integration, 5 migration, 9 concurrency and 99 Dashboard
tests (553 total), plus both image builds and the complete container smoke check.
No Python 3.13 dependency incompatibility was found.

Use separate hosted services and a fresh simplified schema, with runtime secrets
outside Git. Configure the backend database/schema, Gemini key and Supabase
issuer/audience/asymmetric algorithms/JWKS URL, and configure the Dashboard backend
URL and Supabase URL/publishable key. Provision household membership and device
credentials through the existing setup procedures. Verify hosted `/ready`, then
exercise login/refresh, household pages, real screenshots/statements, Gemini and
the iPhone Shortcut against those isolated services.

This checkpoint does not deploy services, provision real credentials, grant S3/S4
acceptance, perform production cutover, or begin S5 feature/removal work.
