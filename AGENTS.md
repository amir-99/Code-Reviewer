# AGENTS.md

## Project and authority

This repository is a standalone AI code review service for GitLab merge requests.
The build specification is `docs/implementation-prd.md`; operator instructions
are in `README.md`. Follow the user's current instructions when they change the
specification. Do not introduce the previous Letta/Open WebUI architecture or its
publication-confirmation workflow into this project.

Confirmed deployment requirements:

- Primary reviewed languages: TypeScript, Go, and Python.
- Jira hierarchy: epic → story → subtask; each story gets a merge request.
- Never force-push. The review service must not commit, push, approve, or merge.
- Model IDs, context limits, credentials, and installation-specific endpoints
  are operator configuration. Do not invent approved values.

The application uses Python 3.12, FastAPI, arq/Redis, async SQLAlchemy,
PostgreSQL 16, Alembic, and Pydantic v2. Use uv and preserve `uv.lock`.
Orchestration is an ordinary Python state machine, not an agent framework.

## Repository map

- `reviewer/api/`: webhook authentication, manual triggering, health,
  administration, and metrics.
- `reviewer/worker.py`: arq startup, event handling, job execution, recovery sweep.
- `reviewer/orchestrator/`: legal transitions, pipeline, stage execution, budgets.
- `reviewer/context/`: requirement linkage, sanitization, bundles, work units.
- `reviewer/services/`: GitLab, Jira, Confluence, Git, LLM, secrets, static tools,
  and tree-sitter symbol indexing; external-service fakes live alongside clients.
- `reviewer/agents/`: stage contracts and versioned prompt templates.
- `reviewer/findings/`: schemas, mechanical validation, deduplication, severity,
  and noise control.
- `reviewer/publish/`: rendering, idempotent publication, commands, re-review,
  and recheck of already-published comments.
- `reviewer/decision/engine.py`: deterministic decision and status policy.
- `reviewer/store/`: persistence, audit objects, repositories, migrations.
- `reviewer/config/` and `config/projects.json`: schemas and project policy.
- `reviewer/telemetry/`, `dashboards/`: logging, tracing, quality metrics, dashboard.
- `tests/`: offline unit/integration tests and generated Git histories.
- `scripts/evaluate_regression.py`: evaluation of operator-supplied labelled MRs.
- `Dockerfile`, `compose.yml`, `compose.static.yml`: runtime and optional sandbox.

## Workflow invariants

- Code owns stage order, coverage checks, transitions, severity, publication,
  and the final decision. Models emit validated structured proposals only.
- Keep legal transitions explicit in `orchestrator/states.py`. Persist progress
  before subsequent work. arq may deliver jobs more than once: preserve event
  deduplication, stage persistence, publication receipts, and recovery behavior.
- Preserve the partial unique index allowing at most one non-terminal review
  per project/MR. Admission and supersession must remain safe under concurrency.
- Manual triggers (`POST /admin/reviews`) enqueue the same job a webhook does.
  Admission, supersession, and recovery keep one durable path: the API process
  resolves and validates the link only, and does not create reviews itself. A
  manual run supersedes an in-flight review for its merge request exactly as a
  new push does. Operator overrides are persisted on the review, so the pipeline
  and a later replay both see the context the run was triggered with.
- Purpose and Design are sequential gates. Correctness, Complexity, Test, and
  Line Review fan out concurrently; System Context follows their aggregation.
- A gate may terminate on a mechanically valid, verified blocker. Model-proposed
  severity alone cannot terminate a review or fail a status.
- Compare reported coverage to dispatched unit IDs. Retry missing coverage once;
  remaining gaps and exhausted budgets make the review partial.
- Partial and failed reviews fail open. Never turn an unavailable dependency or
  internal exception into a failed commit status. Retain undelivered status
  records for retry when GitLab is unreachable.
- Recheck answers every open reviewer thread on a push and on `/ai recheck`.
  Only `fixed` and `obsolete` resolve a thread; a missing, failed or capped
  judgement is never evidence of a fix. Deterministic evidence from the run
  itself outranks the judge, which may only answer more conservatively. Replies carry the head they judged, so
  redelivery re-posts nothing. It obeys enforcement and report mode exactly as
  publication does.
- Preserve `silent`, `advisory`, and `gating` modes; reject `blocking`.
  Silent publishes nothing. Advisory statuses always pass. Gating can fail only
  on the deterministic decision from a complete review.
- The `draft` report mode writes GitLab draft notes: inline pending comments,
  a pending summary, and pending recheck replies carrying the resolution they
  would apply. It never posts a note, resolves a thread, or marks a finding
  published. Deduplicate against the reviewer's own pending drafts so a repeat
  queues nothing twice, and keep silent enforcement writing nothing at all,
  drafts included.
- Keep milestone flags functional. A lower milestone must not imply a complete
  AI review or silently enable later publication/enforcement behavior.

## Repository and integration boundaries

- GitService is the source of code content. Compute local diffs against the
  merge base, and pin worktrees to the reviewed SHA. Compare local changed paths
  with the forge inventory; detect changed heads before publication.
- Inject read-only Git credentials through the credential helper. Never embed
  tokens in clone URLs, command arguments, logs, or stored repository remotes.
- Preserve disabled Git hooks, external diff/text conversion, unsafe protocols,
  and credential-forwarding redirects. Clean up worktrees in `finally` paths;
  retain mirror locking and quota handling.
- Reviewed files are untrusted. Never execute their programs in the API/worker
  runtime. Running this project's own tests is a separate development operation.
- Static commands run only through the sandbox runner: no network, read-only
  repository/root filesystem, unprivileged UID, dropped capabilities, resource
  limits, timeout, and cleanup. Require digest-pinned analyzer images.
- Do not mount the host Docker socket into the default stack or silently enable
  static execution. The optional setup uses an operator-configured sandbox daemon.
- Discover Jira's Epic Link field dynamically; retain the parent-epic fallback.
  Missing requirements degrade the review instead of inventing acceptance criteria.
- Operator-supplied requirements are optional and override discovery rather than
  extending it: an issue key replaces branch/title/commit linkage, an epic key
  replaces the story-derived epic, and hand-supplied pages are read before
  discovered ones. They remain valid for an unlinked change, and an override
  that names document pages or an epic forces a fresh requirement collection
  instead of reusing the previous run's.
- Confluence links must remain restricted to the configured origin, including
  every redirect. Preserve page/version attribution and document budgets.
- Read repository policy from the merge base. Repository-controlled YAML cannot
  select enforcement, commands, images, model endpoints, or credentials.
- Verify webhook tokens in constant time. Keep review work out of the handler.
  Keep Developer-or-higher authorization for `/ai` commands.
- A pasted merge request link is resolved with the reviewer's own token. Accept
  only links whose origin and base path match the configured GitLab instance,
  and only projects already onboarded; reject a closed, merged, or draft merge
  request at the API rather than accepting a job that cannot run.

## Models, prompts, and findings

- All LLM traffic goes through the gateway client. Preserve classification
  headers, token/deadline budgets, transport retries, structured-output validation,
  the single reparse attempt, and an audit record for each attempted call.
- Do not add direct LLM-provider SDKs or a public-provider fallback.
- Run secret scanning before prompt assembly. Redact prompt inputs, model
  outputs, snapshots, and audit blobs. Secret findings are deterministic and
  must not require an LLM call. Scan additional code context before exposing it.
- Preserve untrusted-input framing and the PRD's injection-defense text.
  Agent context requests cannot choose arbitrary network destinations.
- Prompts live under `agents/prompts/{stage}/vX.Y.Z.md`. Changes require a new
  version. Preserve recorded prompt hashes, model IDs, run IDs, and bundle hashes.
- Keep Line Review limited to changed hunks and configured context windows.
- Validate actual files, line ranges, evidence, and symbols before verification.
  Compute anchor SHA, context hash, diff membership, and attribution in code;
  never trust model-supplied values for those fields.
- The independent verifier receives the claim and code evidence, excluding the
  proposer's rationale, confidence, severity, and evidence-note persuasion.
- Severities are exactly `BLOCKER`, `REQUIRED`, `SUGGESTION`, `NIT`, `QUESTION`,
  `FYI`, and `PRAISE`. Preserve category normalization and verification,
  confidence, static-tool, and pre-existing-code caps.
- Preserve the fingerprint algorithm in `findings/dedup.py`. Its normalization
  does not guarantee that arbitrary semantic paraphrases collide.
- Preserve the 15-inline/MR and five-inline/file ceilings, including existing
  unresolved comments. NIT/FYI/QUESTION/PRAISE are summary-only; pre-existing
  findings are limited to two FYIs in touched files. Report overflow counts.
- Publication must reconcile bot-authored fingerprints before posting. Preserve
  the fingerprint marker and machine-assisted footer. Added lines use `new_line`,
  removed lines use `old_line`, and context lines use both.
- An unpositionable finding belongs in the summary. Separate GitLab writes are
  not atomic: do not claim failure can roll back every already-posted comment.
- Re-review must preserve stable finding identities and conservative resolution.
  Incomplete coverage or missing context is not proof a finding was fixed.

## Secrets, storage, and packaging

- Real local credentials belong only in ignored `.env`. Keep `.env.example`
  non-secret. Do not print resolved environment settings, raw upstream failures,
  private content, or audit blobs as routine diagnostics.
- Preserve localhost publication (`127.0.0.1:8092`) and private database/Redis
  ports. Do not broaden exposure without an explicit deployment requirement.
- API, worker, and migrator share one production image. Preserve its unprivileged
  user, read-only deployment, digest pins, and named volumes.
- Add a new Alembic migration for schema changes; do not rewrite an applied
  migration to alter an existing deployment. Never delete volumes or run
  `docker compose down -v` without explicit authorization to delete stored data.
- Keep audit blobs private and redacted. Local retention and internal S3 bucket
  lifecycle policies are distinct. Report unknown model prices as unknown.

## Validation and handoff

Make focused changes and preserve unrelated edits. Add behavioral regression
tests for workflow, persistence, security, or publication changes. Do not add
tests merely to mirror implementation details or validate prose-only edits.

Development checks:

```sh
uv sync --frozen --python 3.12
uv run pytest -q
uv run ruff check reviewer tests scripts
uv run ruff format --check reviewer tests scripts
```

Tests must not require network access, credentials, or live external services.
Use fakes and in-process HTTP transports; use generated local repositories for
Git behavior. Do not disable the suite's socket restrictions to fix a test.
The real gitleaks test runs in the Docker image, where its binary is installed.

After relevant image, dependency, Compose, or migration changes:

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose exec -T api alembic check
docker build --target test -t ai-code-reviewer:test .
docker run --rm --network none ai-code-reviewer:test
```

Expect four healthy long-running services and a successful exited migrator.
Use `docker compose`, not legacy `docker-compose`. Container builds may download
dependencies; offline test execution and live deployment smoke checks are separate.

Report what changed, what was actually tested, and any remaining deployment or
implementation gaps. M9 availability and passing synthetic tests do not establish
the PRD's production definition of done. Precision, fabrication, pilot latency,
and production egress gates require real evaluation data and the approved internal
environment. Never fabricate the historical regression set or claim unmeasured
quality results.
