# Toolchains and analyzers for the review sandbox: Go, Python and Node.js in one
# image. The worker runs it only through the sandbox daemon, with no network,
# a read-only root filesystem and an unprivileged user, so everything an
# analyzer needs is baked in here. Build it where there is network access:
#   docker build -t 127.0.0.1:5000/reviewer-sandbox:1 sandbox
# then push it and pin the pushed digest in config/projects.json.
FROM golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS go
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node
FROM ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 AS uv
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

RUN apt-get update && apt-get install -y --no-install-recommends make ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=go /usr/local/go /usr/local/go
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && pip install --no-cache-dir ruff==0.13.0 pytest==8.4.2

# ESLint with the TypeScript parser, configured here and not by the reviewed
# repository, so a change under review cannot weaken or disable the check.
WORKDIR /opt/sandbox
COPY eslint.config.mjs ./
RUN npm install --no-audit --no-fund --ignore-scripts --no-package-lock \
    eslint@9.36.0 typescript@5.9.2 typescript-eslint@8.44.0 \
    && chmod -R a+rX /opt/sandbox

ENV PATH="/opt/sandbox/node_modules/.bin:/usr/local/go/bin:$PATH" \
    GOTOOLCHAIN=local GOTELEMETRY=off GOFLAGS=-buildvcs=false
WORKDIR /
