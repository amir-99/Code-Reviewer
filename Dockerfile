FROM docker:28.5.1-cli@sha256:9190b0613792e658a7783cf14b2d5ace5941bb68ede7276922ea36ee457d76ad AS dockercli
FROM zricethezav/gitleaks:v8.24.3@sha256:e1b35e12a8c6fa8901f060459cfb6b2fc4c484d3afbe3b029733a3bbfab07055 AS gitleaks
FROM ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 AS uv
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS base
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY reviewer ./reviewer
COPY alembic.ini ./
COPY config ./config
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home reviewer && mkdir -p /var/lib/reviewer/audit /var/lib/reviewer/repos && chown -R 10001:10001 /var/lib/reviewer
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health/ready', timeout=3)"]
CMD ["uvicorn", "reviewer.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

FROM base AS test
USER root
RUN uv sync --frozen
COPY tests ./tests
USER 10001
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS production
