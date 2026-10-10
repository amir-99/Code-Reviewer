"""Absolute phase cutoffs; recovery never renews a review's time budget."""

from datetime import timedelta


def cutoff(bundle, config, stage):
    total = config.review.timeout_s
    publication = min(config.publication_reserve_s, total / 10)
    finalization = min(config.finalization_reserve_s, total / 4)
    if stage in {"verification", "recheck"}:
        reserve = publication
    else:
        reserve = max(finalization, publication)
    return bundle.budget.deadline_at - timedelta(seconds=reserve)
