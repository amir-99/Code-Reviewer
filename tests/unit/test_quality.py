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
