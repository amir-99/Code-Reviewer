# Review Room

A responsive operator dashboard built with native JavaScript modules and CSS.
No frontend framework, bundler, CDN, or runtime npm dependencies are required.
The Docker build packages the assets with a small Python HTTP server and a
streaming proxy to the reviewer API.

From the repository root, with the existing `.env` configured:

```sh
docker compose -f compose.yml -f compose.frontend.yml up -d --build
```

Open http://127.0.0.1:8093 and enter the configured `ADMIN_TOKEN`. The API retains
its existing localhost port 8092. The optional Compose file adds a fifth healthy
long-running service. It does not mount credentials in the frontend container.
The token is held only in tab memory and attached as a Bearer header, including
on fetch-based SSE requests. Reloading or disconnecting clears it.

For standalone packaging:

```sh
docker build -t ai-code-reviewer-frontend:local frontend
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:8093:8080 \
  -e REVIEWER_API_URL=http://your-internal-api:8080 \
  ai-code-reviewer-frontend:local
```

`REVIEWER_API_URL` is a server-side, operator-configured HTTP(S) endpoint, with an
optional base path. It must be reachable from the container. Browser requests
use `/api/admin/...` on the same origin; no CORS configuration is needed. The
proxy forwards authentication and SSE cursors without buffering the stream.
There is no public host binding in Compose. Use an authenticated TLS ingress if
operators need access outside the host.

For local development (Python 3.12; no npm install required):

```sh
REVIEWER_API_URL=http://127.0.0.1:8092 PORT=8093 python3 frontend/server.py
node --check frontend/app.js
node --test frontend/sse.test.js
```

The dashboard supports recent reviews, manual triggers, Jira story/epic and
Confluence overrides, report modes, stored findings, plain-text reports, and a
live activity feed. Draft is the default report mode; a drafted run leaves its
comments on the merge request as GitLab draft notes, which only the reviewer
account sees until someone publishes them.

**Recheck comments** re-judges the threads a review published, at the merge
request's current head. It calls `POST /admin/reviews/{id}/recheck`, which runs
no stages and publishes no report. The review is already terminal, so its event
stream is finished: the dashboard polls the review until the answers appear and
then shows each verdict, what changed, and whether the thread was resolved.
Nothing appears if the review has no open reviewer threads or the project
disables recheck. Project enforcement rules
still control publication and commit statuses. Activity is bounded to the latest
300 displayed entries; all retained events remain available through the API.

## SSE API

Apply Alembic migration `0005` before starting the updated API and worker.

`GET /admin/reviews` now returns `{"reviews": [...]}` when `event_id` is absent.
It accepts `limit` (1–100, default 50) and `offset` (default 0). The existing
`?event_id=...` lookup and queued response retain their behavior.

`GET /admin/reviews/{id}/events` requires the existing admin Bearer token.
Optional `Last-Event-ID` resumes after a review-local integer sequence. An
unknown review returns 404; invalid cursors return 400. The stream contains:

| Event | Data |
| --- | --- |
| `snapshot` | Current review detail, findings and report; at connection and completion |
| `activity` | `id`, `kind`, `at`, and `data`; SSE `id` equals the durable sequence |
| `complete` | Terminal review `state`; the client should stop reconnecting |

Activity `kind` is `state`, `agent`, `unit`, or `tool`. State payloads contain
`state`. Other payloads contain a fixed operational `name`, `status`, unique
`activity_id`, and optional `parent_id` linking nested calls. Status is
`started`, `completed`, `partial`, `failed`, or `cancelled`. A completed tool
invocation is not proof the analyzer found no defects. Parallel stage agents
inherit separate activity parents. These are the existing deterministic stage
agents and their work units, not an additional agent framework.

Events contain no prompts, code, credentials, arguments, outputs, exception
messages, or private model reasoning. Tools include context collection, Git,
secret scanning, static analysis, the LLM gateway, and independent verification.
Activities are best effort: a database error must not change review decisions.
State events are committed with the state transition. Process termination can
leave a started activity without a completion event; the UI labels that absence.
Older reviews have no retrospective tool trace.

Events are persisted in PostgreSQL for cross-process delivery and reconnects.
The API polls once per second, drains events in batches of 200, sends heartbeat
comments every 15 seconds, and releases database sessions between polls. Browser
reconnects wait two seconds. Events currently follow review lifetime; no automatic
activity-row retention policy is installed. Size and retain this table according
to deployment volume. SSE consumes one API connection and one frontend proxy
thread per open tab; large deployments should capacity-test concurrent streams.
