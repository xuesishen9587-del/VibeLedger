# VibeLedger current handoff

Branch: `experiment/astra-simplified`; PR #17 open. Accepted baseline:
`278f143ab3fa4ab24f5a543a11f8405fce051ebf`.

S1–S4 and real hosted Expense/Balance/Statement Gemini flows are user-accepted.
S5 is complete under the amended manual acceptance scope: retire superseded runtime, add authenticated daily schedule
execution and visible browser catch-up, align packaging/contracts/runbooks, and
accepted only `vibeledger-s34acc-backend` / `vibeledger-s34acc-web` staging.
S6, production fresh cutover and PR merge remain unauthorized.

The owner's 2026-09-20 instruction adjusts manual acceptance to the testing already
done: one user, one clear expense Shortcut capture, and an 88-row MariBank PDF.
Do not ask for more manual samples or a second-user sign-off. Record this limitation
honestly; automated S5 integrity/auth/recovery/ops acceptance is still required.

See [readiness](docs/deployment/DEPLOYMENT_READINESS.md),
[S5 evidence](docs/deployment/S5_ACCEPTANCE.md), and
[current staging runbook](docs/deployment/STAGING_DEPLOYMENT.md) for final state.
Historical checkpoints are available in Git; they do not override this handoff.
