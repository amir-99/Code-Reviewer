from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from reviewer.api import admin, health, manual, webhooks
from reviewer.config.schema import Settings
from reviewer.services.forge.gitlab import GitLab
from reviewer.store.repositories import Store
from reviewer.telemetry import configure


def create_app(settings=None, store=None, queue=None, forge=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        configure()
        app.state.store = store or Store(settings.database_url.get_secret_value())
        app.state.queue = queue or await create_pool(
            RedisSettings.from_dsn(settings.redis_url.get_secret_value())
        )
        # The manual trigger resolves a pasted link before enqueueing, so the
        # API process needs its own forge client alongside the worker's.
        app.state.forge = forge or GitLab(
            settings.gitlab_base_url, settings.gitlab_token.get_secret_value()
        )
        try:
            yield
        finally:
            if queue is None:
                await app.state.queue.aclose()
            if forge is None:
                await app.state.forge.close()
            if store is None:
                await app.state.store.engine.dispose()

    app = FastAPI(title="AI Code Reviewer", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    # Explicit injection supports network-free ASGI integration tests.
    if store is not None:
        app.state.store = store
    if queue is not None:
        app.state.queue = queue
    if forge is not None:
        app.state.forge = forge
    for router in (webhooks.router, admin.router, manual.router, health.router):
        app.include_router(router)
    return app


app = create_app()
