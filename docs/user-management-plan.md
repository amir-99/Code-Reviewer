# User management and UI implementation plan

Status: implemented; validation and deployment details are recorded in `docs/user-management-rollout.md`.

## 1. Confirmed behavior

There are exactly two interactive roles: `admin` and `user`.

- Admins manage accounts and view every review and report. They cannot run, replay, recheck, cancel, edit, delete, or publish reviews, including by submitting requests directly to the API.
- Users manage their own profile and integration tokens, start reviews, and view only their own review history, findings, reports, activity, cost, and token breakdowns. Replay and recheck are restricted to their own reviews.
- A user's profile contains Gateway, GitLab, Confluence, and Jira access tokens. Admins cannot read, replace, test, or use another account's integration tokens.
- The UI is redesigned around these roles. The shared admin-token login screen is replaced by account login.
- Ownership is immutable after a review is admitted. Disabling an account retains its historical reviews for admin inspection.

| Capability | Admin | User |
|---|---|---|
| View review lists, reports, findings, activity and spend | All reviews | Own reviews |
| Start, replay or recheck a review | No | Own reviews only |
| Maintain integration tokens | No | Own tokens only |
| Create, disable/reactivate accounts and change roles | Yes | No |
| Initiate account recovery and revoke login sessions | Yes | Own session/logout controls |
| Reveal saved integration tokens | No | No; replace/remove instead |
| Impersonate another account or transfer review ownership | No | No |

“Own reviews” is a dashboard/API access rule. Existing GitLab report modes still apply: publishing a comment makes it visible according to GitLab's own project permissions. Preserve silent, advisory, gating, applied, draft, and frontend-only reporting behavior.

## 2. Current implementation and required backend work

`reviewer/api/admin.py` authenticates every dashboard request using one installation token. `reviews` has no owner. Manual triggers use `requested_by="admin"`, and the API and worker construct shared integration clients from installation credentials. The frontend proxy only supports the existing admin GET/POST routes and does not forward session cookies.

Ownership must therefore be implemented in persistence, authorization, job admission and execution as well as the UI. Previous-review lookup, recheck finding lookup, Git mirrors, publication receipts, and reused context currently rely on project/MR identity and require an ownership audit.

Keep the existing Python state machine, worker-owned admission, durable recovery, model resolution, cost auditing and fail-open review decisions.

## 3. Accounts and login

Use admin-created local accounts for the first release; no public registration or external identity provider is assumed. Store a unique normalized login, display name, role, account status and timestamps. Users set their password through an expiring, single-use activation flow. Bootstrap the first admin through an operator CLI, not an unauthenticated web endpoint or a default password.

Use Argon2id password hashes and opaque server-side sessions. Persist only hashes of session and activation/reset tokens. Rotate sessions on login; revoke sessions after password reset, disablement and role changes. Apply login throttling and generic authentication failures. Cookie sessions use HttpOnly, Secure in HTTPS deployments, SameSite and CSRF protection for writes. Keep any localhost HTTP development exception explicit and narrowly scoped. These choices follow [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) and [session management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

Admins initiate recovery but do not choose or retrieve a user's password. An admin-assisted recovery must not become a route to using the user's saved credentials: require user-controlled verification, or revoke saved integrations and require the recovered user to reconnect them. Delivery of activation/recovery links is an operator-configured process; this plan assumes no email service.

Prevent disabling or demoting the last active admin. Promotion to admin removes review execution privileges and invalidates sessions; it does not grant permission to use previously saved integrations. Record account-management events without secret values. Account management never exposes review mutation controls.

## 4. User token profiles and execution

Add an encrypted, versioned credential store keyed by user and integration. Store ciphertext, key identifier, version, lifecycle state and non-secret validation metadata. Keep the encryption key outside PostgreSQL under operator control, with backup and rotation procedures. API responses expose only configured/missing/invalid status and update/check timestamps. Never return the saved token, including to its owner. This follows [OWASP secrets-management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html).

The profile supports save/replace, remove and connection checks. Checks go only to operator-configured endpoints; users cannot supply arbitrary gateway or document-service URLs. Show sanitized failures. Gateway validation should use a supported non-billable authentication endpoint if available; an actual model-call test must be explicitly labelled as billable.

Gateway and GitLab credentials are required for an AI review. Jira and Confluence fields are supported but can be missing: preserve degraded requirement collection, and identify missing credentials in preflight and review results. Never silently substitute installation credentials for a user's missing or invalid token.

Construct API lookup clients and worker clients from an immutable per-run credential context. A request's authenticated identity, never a body-supplied owner ID, determines ownership. Resolve the MR using that user's GitLab access. Their GitLab token also supplies repository read access through the existing credential helper; never add a Git push operation. Existing publication uses that user's GitLab identity and permissions, rather than the installation bot identity.

Redis jobs carry owner IDs and credential-version references, never secret values. Persist those references with the run before stages execute. Resume/recovery uses the pinned versions; token replacement applies to new runs. Explicit removal/revocation stops future use of the revoked version, including queued work and later publication. Check account eligibility again in workers and before external writes. Already dispatched requests cannot be recalled; stop subsequent work and clean up conservatively without generating a failing commit status from an authentication failure. Retain genuinely undelivered statuses for the existing retry path, without substituting another principal.

Separate per-user/per-principal repository and document caches and prior-review reuse. Preserve shared gateway capacity limits and effective mirror locking when replacing shared clients with per-run clients. Register saved token values with redaction so they cannot appear in logs, prompts, snapshots, events or audit blobs.

## 5. Ownership and concurrency

Add `owner_user_id` (nullable for system/legacy reviews), `trigger_source`, and non-secret credential/principal references. Persist trigger ownership before enqueueing, so event-ID polling remains authorized even before a Review exists. A small durable trigger record/outbox can support this without moving review admission into the API.

Apply authorization to every path: listing and pagination, event-ID lookup, review detail, SSE connection/reconnect, findings, report, spend, quality aggregates, model-selection metadata, audit metadata, replay and recheck. Users receive a generic not-found response for another user's review. Admin read access does not imply write access. Stream authorization must respond to session revocation, not just check the initial connection. Keep raw audit blobs private rather than introducing a new download surface.

Scope previous/latest-published review lookup, fingerprint fallback, context reuse, recheck jobs and results to the owner and publishing principal. A recheck must never locate another user's latest review simply because the MR matches. Reconcile GitLab notes against actual authors and persisted publication identities while preserving fingerprint markers, idempotency, conservative resolution and MR-wide inline ceilings.

Preserve the global partial unique index allowing one active review per project/MR. Same-owner manual reruns retain existing supersession. A different user's request must not cancel or replace the active review: reject it durably as a conflict with a generic message, without revealing the other account. This is an intentional refinement of manual supersession for account isolation. Real forge head changes and merge/close events retain system lifecycle authority. Concurrent personal/system publication and recheck actions must share an MR mutation lock and revalidate the head and author before writing.

Legacy and webhook-created reviews remain system-owned and visible only to admins. System is a trigger/principal classification, not a third login role. Existing webhook secrets and service credentials remain confined to automation. Never infer an application account owner from a forge author ID. Existing `/ai` commands keep forge authorization and must not spend a user's tokens or mutate their owned review through the service-account path.

## 6. UI redesign

Shared shell: account identity, clear role badge, theme control and logout. Retain the existing frontend technology; no framework migration is necessary for this feature.

User navigation: **My reviews**, **New review**, **Profile**. My reviews shows only owned runs with state, MR, date, cost and token usage. New review preserves the current MR, requirements, model and report-mode controls and adds credential-readiness feedback. Profile shows identity/password controls and four integration cards with status, replacement fields, remove and connection-check actions. Fields clear after save and never preload a stored token.

Admin navigation: **All reviews**, **Users**, **Account**. All reviews adds owner and system/legacy filters. Users provides search, account creation, role/status management, recovery and session revocation. Do not show integration secrets or a “run as user” action.

The review detail page retains Activity, Findings, Recheck, Spend and Report. Keep total cost, input/output tokens and model/step breakdowns, including rechecks. Show the owner in admin views. Admin pages contain no execution or publication buttons. User pages expose actions only for owned reviews; capability flags returned by the API drive visibility, with independent server checks.

Handle expired sessions, disabled accounts, missing/invalid tokens, admission conflicts, queued actions and empty review lists explicitly. Logout/account switching must abort streams and requests and clear cached review data. Ignore responses from an earlier session. Retain keyboard navigation, labelled forms and usable mobile tables.

Update `frontend/server.py` to allow the new specific API routes/methods, forward session cookies and CSRF headers, and correctly relay Set-Cookie without broadening the proxy to arbitrary upstream destinations. Preserve SSE streaming and no-store responses.

## 7. Delivery sequence

1. **Persistence and account foundation:** add new Alembic migrations for accounts, sessions, credentials, trigger ownership and review ownership; implement bootstrap, activation, login/logout and account management. Preserve legacy rows.
2. **Authorization and ownership:** introduce authenticated principals and centralized read/execute policies; protect every review surface, worker entrypoint and event lookup. Resolve same-MR concurrency and system-trigger boundaries before exposing user login.
3. **Credential execution:** replace shared personal-run clients with per-run credential contexts; implement token lifecycle, access checks, cache separation, recovery and publication identity scoping.
4. **Frontend redesign:** implement role-specific navigation, account screens, token cards, review filters and read-only admin details; update proxy/session handling and preserve report/spend features.
5. **Validation and rollout:** exercise the complete two-user/admin flows, migrate a copy of existing data, bootstrap an admin, deploy API/worker/frontend together and smoke-test role isolation. Remove the shared ADMIN_TOKEN bypass from human review routes; keep any necessary machine authentication separate and explicitly scoped.

Implement these as focused changes with behavioral tests, not as an immediate partial rollout where the UI suggests isolation before the backend enforces it. Update README, deployment configuration examples and the relevant PRD/AGENTS invariants for per-user credentials and cross-owner admission.

## 8. Acceptance and rollout checks

Use an admin plus users A and B, including two owners reviewing the same MR. Verify direct API calls as well as UI controls: admin can read both users' reports but every review mutation is denied; A cannot read or act on B's reviews through IDs, event polling, SSE, rechecks, aggregate metrics or stale browser state.

Verify each outgoing fake integration request uses the correct owner's token; token values never appear in queue payloads, snapshots, logs, HTTP responses or audit metadata. Cover missing credentials, rotation, removal, disablement, password recovery, account role changes, session revocation, worker restart and job redelivery.

Exercise ownership-aware admission races, same-owner supersession, cross-owner conflicts, publication identity changes, system hooks and MR commands. Confirm report/recheck cost totals remain correct and do not combine owners' runs. Keep the one-active-review index, fail-open behavior, draft/silent semantics, idempotency and conservative thread resolution.

Run frozen Python dependency setup, the full offline Python suite, frontend behavior/proxy tests, Ruff checks and browser checks for both roles. Schema/dependency/packaging changes require the repository's Compose, migration, test-image and healthy-service checks. Real token smoke tests are separate from offline tests and use only approved internal endpoints.

Rollout prerequisites are operator-provided credential-encryption key management, initial-admin setup, activation/recovery delivery and the deployment's HTTPS/session-cookie configuration. Back up the database and encryption material before migration. Rollback must never expose newly private reviews through the old shared-token UI; disable access or use an ownership-aware rollback build. Do not delete review history, credentials or volumes as part of rollout.
