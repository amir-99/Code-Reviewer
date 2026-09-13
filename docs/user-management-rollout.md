# User-management rollout — 2026-09-13

The five implementation stages are delivered: account persistence and lifecycle,
centralized ownership authorization, pinned personal credentials, role-specific UI,
and regression/migration/deployment validation.

## Validation performed

- Frozen Python 3.12 dependency synchronization and Ruff checks passed.
- Host offline suite: 225 passed, one gitleaks test skipped because the host lacks
  the binary. The network-disabled Docker test image passed all 226 tests.
- Frontend module tests and JavaScript syntax checks passed; five proxy tests passed.
- Chromium checks passed for admin read-only controls, account management visibility,
  user-only review lists, token-field clearing, logout/account switching, and a
  390-pixel mobile viewport without horizontal overflow.
- Restored a private backup into a separate PostgreSQL validation database, applied
  migration 0007, and ran Alembic drift checks. All 13 legacy reviews were retained
  with null owners and the system trigger classification.
- Built and deployed API, worker and frontend together. PostgreSQL, Redis, API,
  worker and frontend are healthy; the migrator exited successfully. Live Alembic
  checks report no pending schema changes.
- Created the initial `admin` account with display name `admin`, using the existing
  ADMIN_TOKEN value as its password at the user's explicit request. The value was
  never printed. The legacy bearer token does not authorize dashboard routes.

## Deployment configuration and remaining checks

The session origin is `https://review.blubank.ai`, for the dashboard mounted under
`/agentic/`. Encryption material is stored in the ignored environment file. Database
and environment/key backups are retained under the ignored, private
`.private-backups/` directory. No existing review history or volumes were deleted.

Local production API and remote HTTPS smoke checks passed: login, secure cookies,
all 13 existing reviews readable by the admin, denial of trigger/replay/recheck and
integration-profile access, logout revocation, and legacy bearer-token rejection.
The supplied Nginx configuration routes `/agentic/api/` directly to the backend and
`/agentic/` to the frontend. At the user's supplied upstream targets, API and frontend
bind only to `192.168.30.161:8092` and `192.168.30.161:8093`. Compose defaults remain
localhost. Automatic approval review rejected an all-interface binding; the narrower,
operator-specified interface was approved and applied successfully.

No personal live-integration review or billable model call was performed during
rollout. Users must create their own integration profiles. Production precision,
fabrication, latency and approved-egress gates remain unmeasured; the regression
suite and dashboard smoke tests do not establish those quality results.
