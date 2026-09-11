from test_findings import finding

from reviewer.telemetry.quality import quality


async def test_precision_and_fabrication_queryable(store):
    review = await store.accept(7, 2, "a" * 40, "quality")
    f = finding()
    f.severity_final = "REQUIRED"
    await store.save_findings(review, [f])
    await store.outcome(
        review.project_id, 2, f.fingerprint, "actioned", "fixed", "system"
    )
    rows = await quality(store, 7)
    assert rows[0]["precision"] == 1 and rows[0]["fabrication_rate"] == 0
    assert rows[0]["prompt_version"] == "1.0.0" and rows[0]["model"] == "fake"


async def test_persistent_performance_metrics_survive_separate_scrapes(store):
    from reviewer.telemetry.quality import prometheus

    review = await store.accept(7, 2, "a" * 40, "timings")
    for data in [
        {"name": "purpose", "status": "started"},
        {"name": "purpose", "status": "completed"},  # Historical row
        {"name": "purpose", "status": "completed", "duration_ms": 1250},
    ]:
        await store.append_event(review.id, "agent", data)
    await store.append_event(
        review.id,
        "llm_attempt",
        {
            "role": "purpose",
            "outcome": "success",
            "duration_ms": 500,
            "budget_wait_ms": 250,
        },
    )
    first = (await prometheus(store)).decode()
    second = (await prometheus(store)).decode()
    # Fresh registries reconstruct retained observations, never double-counting.
    for output in [first, second]:
        assert (
            'reviewer_operation_duration_seconds_sum{kind="agent",name="purpose",status="completed"} 1.25'
            in output
        )
        assert (
            'reviewer_operation_duration_seconds_count{kind="agent",name="purpose",status="completed"} 1.0'
            in output
        )
        assert (
            'reviewer_operation_duration_seconds_sum{kind="wait",name="Token budget reservation",status="completed"} 0.25'
            in output
        )
        assert review.id not in output
