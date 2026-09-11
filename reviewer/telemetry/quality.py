from collections import defaultdict

from sqlalchemy import select

from reviewer.store.models import (
    FindingOutcome,
    FindingRow,
    LLMCall,
    Project,
    Review,
    ReviewEvent,
    ReviewStage,
)


async def quality(store, project_id=None):
    async with store.sessions() as session:
        projects = {
            p.id: p.gitlab_project_id
            for p in (await session.scalars(select(Project))).all()
        }
        reviews = {r.id: r for r in (await session.scalars(select(Review))).all()}
        findings = (await session.scalars(select(FindingRow))).all()
        outcomes = (await session.scalars(select(FindingOutcome))).all()
        calls = (await session.scalars(select(LLMCall))).all()
        stages = (await session.scalars(select(ReviewStage))).all()
    groups = defaultdict(
        lambda: {
            "findings": 0,
            "fabrications": 0,
            "actioned": 0,
            "dismissed": 0,
            "tokens": 0,
            "cost": 0.0,
            "cost_unknown": False,
            "latency_ms": 0,
            "examined": 0,
            "skipped": 0,
        }
    )
    feedback = {(x.project_id, x.mr_iid, x.fingerprint): x.outcome for x in outcomes}
    for f in findings:
        project = projects[f.project_id]
        if project_id is not None and project != project_id:
            continue
        prov = f.data["provenance"]
        key = (
            project,
            prov["agent"],
            f.data["category"],
            prov["prompt_version"],
            prov["model"],
        )
        g = groups[key]
        g["findings"] += 1
        if "fabricated_anchor" in (f.data.get("validation") or {}).get("reasons", []):
            g["fabrications"] += 1
        outcome = feedback.get((f.project_id, f.mr_iid, f.fingerprint))
        if outcome in {"actioned", "dismissed"}:
            g[outcome] += 1
    for call in calls:
        review = reviews.get(call.review_id)
        if not review:
            continue
        project = projects[review.project_id]
        if project_id is not None and project != project_id:
            continue
        g = groups[(project, call.stage, "*", call.prompt_version, call.model)]
        g["tokens"] += call.tokens_in + call.tokens_out
        g["cost"] += call.cost or 0
        if call.cost is None:
            g["cost_unknown"] = True
        g["latency_ms"] += call.latency_ms
    for stage in stages:
        review = reviews[stage.review_id]
        project = projects[review.project_id]
        if project_id is not None and project != project_id:
            continue
        g = groups[(project, stage.stage, "*", "*", "*")]
        g["examined"] += len(stage.coverage_json.get("examined", []))
        g["skipped"] += len(stage.coverage_json.get("skipped", []))
    result = []
    for key, g in groups.items():
        labels = dict(
            zip(["project", "stage", "category", "prompt_version", "model"], key)
        )
        result.append(
            {
                **labels,
                **g,
                "precision": g["actioned"] / (g["actioned"] + g["dismissed"])
                if g["actioned"] + g["dismissed"]
                else None,
                "fabrication_rate": g["fabrications"] / g["findings"]
                if g["findings"]
                else None,
                "coverage": g["examined"] / (g["examined"] + g["skipped"])
                if g["examined"] + g["skipped"]
                else None,
            }
        )
    return result


async def prometheus(store):
    from prometheus_client import CollectorRegistry, Gauge, generate_latest

    registry = CollectorRegistry()
    labels = ["project", "stage", "category", "prompt_version", "model"]
    gauges = {
        field: Gauge(
            "reviewer_quality_" + field,
            field.replace("_", " "),
            labels,
            registry=registry,
        )
        for field in (
            "precision",
            "fabrication_rate",
            "coverage",
            "latency_ms",
            "tokens",
            "cost",
        )
    }
    for row in await quality(store):
        for field, gauge in gauges.items():
            if row[field] is not None and not (field == "cost" and row["cost_unknown"]):
                gauge.labels(*(str(row[x]) for x in labels)).set(row[field])
    await performance_metrics(store, registry)
    return generate_latest(registry)


async def performance_metrics(store, registry):
    """Export worker timings from durable events, even in a separate API process.

    Counts cover retained events, not process lifetime. Old events without
    durations are omitted rather than interpreted as zero-latency operations.
    """
    import math

    from prometheus_client import Histogram

    duration = Histogram(
        "reviewer_operation_duration_seconds",
        "Operation wall time from retained activity events; nested times overlap",
        ["kind", "name", "status"],
        buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1200),
        registry=registry,
    )
    async with store.sessions() as session:
        rows = await session.stream(
            select(ReviewEvent.kind, ReviewEvent.data)
            .where(
                ReviewEvent.kind.in_(
                    [
                        "pipeline",
                        "agent",
                        "unit",
                        "tool",
                        "wait",
                        "storage",
                        "llm_attempt",
                    ]
                )
            )
            .execution_options(yield_per=500)
        )
        async for kind, data in rows:
            wait = data.get("budget_wait_ms")
            if (
                kind == "llm_attempt"
                and isinstance(wait, (int, float))
                and math.isfinite(wait)
                and wait >= 0
            ):
                duration.labels(
                    "wait", "Token budget reservation", "completed"
                ).observe(wait / 1000)
            elapsed = data.get("duration_ms")
            if (
                not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed)
                or elapsed < 0
            ):
                continue
            name = data.get("role") if kind == "llm_attempt" else data.get("name")
            status = (
                data.get("outcome") if kind == "llm_attempt" else data.get("status")
            )
            if name and status and status != "started":
                duration.labels(kind, name, status).observe(elapsed / 1000)
