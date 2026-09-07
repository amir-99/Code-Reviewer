from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from reviewer.api.admin import authenticate

router = APIRouter()


@router.get("/health/live")
async def live(request: Request):
    return {
        "status": "ok",
        "milestone": request.app.state.settings.milestone,
        "ai_analysis": request.app.state.settings.milestone != "M0",
    }


@router.get("/health/ready")
async def ready(request: Request):
    try:
        async with request.app.state.store.sessions() as session:
            await session.execute(text("SELECT 1 FROM alembic_version"))
        await request.app.state.queue.ping()
    except Exception:
        raise HTTPException(503, "Dependencies unavailable") from None
    return {"status": "ready"}


@router.get("/metrics")
async def metrics(request: Request):
    await authenticate(request)
    from reviewer.telemetry.quality import prometheus

    return Response(
        generate_latest() + await prometheus(request.app.state.store),
        headers={"content-type": CONTENT_TYPE_LATEST},
    )
