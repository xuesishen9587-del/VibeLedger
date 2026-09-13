# Consumer login setup and acceptance

Status: implementation instructions for the simplified Dashboard (2026-09-13).
No service, Supabase account, signing key, or deployment was changed by this work.
S3 real-document acceptance and S4 independent/household acceptance remain open.
The historical HMAC/token-pasting runbook does not configure this Dashboard.

## Operator configuration

1. On the intended Supabase project, disable public signup and provision the two
   household users. Map their verified Auth user UUIDs to the existing
   `users.auth_subject` values and household memberships using the operator setup
   process. Never trust a client-supplied household or create membership at login.
2. Verify the project's actual asymmetric signing mode. Configure the backend with
   its exact issuer, audience, algorithm and public key-set endpoint. Do not switch
   signing keys or overwrite existing user mappings without reviewing that project's
   existing consumers. The example uses ES256; use RS256 only if the project does.
3. Configure the Dashboard with the same project's HTTPS URL and **publishable** key.
   This implementation accepts `sb_publishable_...`; secret/service-role keys and
   legacy key text are intentionally rejected. No financial database or Gemini
   secret belongs in the Dashboard.

Backend configuration (placeholders only):

```dotenv
AUTH_ISSUER=https://<project-ref>.supabase.co/auth/v1
AUTH_AUDIENCE=authenticated
AUTH_ALGORITHMS=["ES256"]
AUTH_JWKS_URL=https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
```

Dashboard configuration:

```dotenv
BACKEND_URL=https://<backend-host>
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_<public-project-key>
DASHBOARD_TIMEZONE=Asia/Singapore
```

The Dashboard's normal entry point ignores AUTH_TOKEN and offers no token textbox.
Backend static/HMAC verification remains for explicit nonproduction testing and
legacy isolated staging; production browser requests require pinned JWKS settings.
A configured JWKS verifier rejects HS256, an unpinned issuer/key endpoint, and an
unknown/wrong signature, issuer, audience or expiry. It does not fall back to HMAC.

## Runtime behavior

The Dashboard sends password and refresh grants to Supabase Auth directly over
HTTPS using the existing HTTP dependency. No custom backend password endpoint or
signup flow is introduced. It checks the backend's membership resolution before
establishing the financial session. Passwords are cleared after form submission;
access/refresh tokens remain in that Streamlit server-side session only. Each API
request checks refresh readiness, without sharing a global authenticated client.

Refresh begins within 60 seconds of expiry. Transient provider failure retains the
last refresh token and pending financial commands for retry. A rejected refresh
requires login; same-subject reauthentication preserves pending command keys, while
switching subjects clears prior data. Logout requests Supabase's **local** scope,
then clears all local session state even if remote signout fails. The UI reports
that failure without echoing credentials. A new/disconnected session may need login.
Do not treat local logout as immediate cryptographic revocation of previously issued
access tokens: Supabase access tokens remain valid until expiry. Disabled local
users and revoked device credentials are checked by the backend on requests.

Public keys are cached for five minutes per backend process. An unknown key ID can
trigger an additional key refresh at most once per 30 seconds. Expired-cache failures
fail closed and retry no faster than every 30 seconds. Fetches use only the configured
HTTPS endpoint, reject redirects, and bound response size and HTTP timeout. Configure
issuer and JWKS together; token-supplied URLs are never used to select key sources.

## Required hosted acceptance

* Both provisioned users sign in independently, refresh, sign out, and sign back in.
  Verify no cached financial data, selected IDs, or bearer clients cross sessions.
* Unknown/nonmember and disabled users have no financial access; direct financial
  Data API grants remain disabled. The code does not change Supabase project grants.
* Verify actual signing-key rotation and project settings, including old-key removal,
  unknown-key throttling and expiry. Local cryptographic fixtures are not proof of
  the hosted project's key configuration.
* Exercise all four destinations, statement import, investment flow confirmation,
  account/category history, and device provision/revocation. Provisioned device tokens
  are shown once for Shortcut setup and must not be pasted into reports or Git.
* Test a lost save response followed by refresh/reauthentication and exact command
  retry. Explicit logout clears local recovery state; review saved records/drafts
  after signing in again.

Password recovery is operator-assisted for these two accounts. No reset-email service,
public registration, deployment or production cutover is authorized by this document.

References: [Supabase password auth](https://supabase.com/docs/guides/auth/passwords),
[Auth HTTP endpoints](https://github.com/supabase/auth/blob/master/README.md),
[signing keys](https://supabase.com/docs/guides/auth/signing-keys),
[local signout and token expiry](https://supabase.com/docs/guides/auth/signout).
