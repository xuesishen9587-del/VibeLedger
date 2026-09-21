# VibeLedger current handoff

Current merged main: `e5f3cbd7c14c6d7fb430b59e5090b41a5bc02cc3`.
Accepted S5 head: `a6aa1f660d5aa0e6393547be5fe1eb277e1d28bb`.

S1–S5 are complete under the owner-approved acceptance scope. Accepted staging is
`vibeledger-s34acc-backend` / `vibeledger-s34acc-web`, schema
`vibeledger_s34acc_20260914`. Old services remain preserved.

S6 preparation is authorized; production execution/client cutover is not.
See the [reviewable S6 sheet](docs/deployment/S6_PRODUCTION_CUTOVER.md) for proposed
resources, operator SQL, approval gates and unresolved private inputs. S6 is not complete.

The owner's 2026-09-20 instruction adjusts manual acceptance to the testing already
done: one user, one clear expense Shortcut capture, and an 88-row MariBank PDF.
Do not ask for more manual samples or a second-user sign-off. Record this limitation
honestly; automated S5 integrity/auth/recovery/ops acceptance is still required.

See [readiness](docs/deployment/DEPLOYMENT_READINESS.md),
[S5 evidence](docs/deployment/S5_ACCEPTANCE.md), and
[current staging runbook](docs/deployment/STAGING_DEPLOYMENT.md) for final state.
Historical checkpoints are available in Git; they do not override this handoff.
