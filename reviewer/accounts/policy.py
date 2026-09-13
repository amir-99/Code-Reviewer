"""Central review access rules, independent of dashboard visibility."""

from fastapi import HTTPException


async def review_access(request, review_id, execute=False):
    account = request.state.principal
    review = await request.app.state.store.get(review_id)
    if review is None or (
        account.role != "admin" and review.owner_user_id != account.id
    ):
        raise HTTPException(404, "Review not found")
    if execute and (account.role != "user" or review.owner_user_id != account.id):
        raise HTTPException(403, "Review execution is restricted to its owner")
    return review
