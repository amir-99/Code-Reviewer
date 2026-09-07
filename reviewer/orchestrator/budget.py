import asyncio
from datetime import UTC, datetime


class BudgetExhausted(RuntimeError):
    pass


class BudgetTracker:
    def __init__(self, budget):
        self.budget = budget
        self.lock = asyncio.Lock()
        self.reserved = 0

    async def reserve(self, tokens):
        async with self.lock:
            if (
                datetime.now(UTC) >= self.budget.deadline_at
                or self.budget.tokens_used + self.reserved + tokens
                > self.budget.token_ceiling
            ):
                raise BudgetExhausted("Review budget exhausted")
            self.reserved += tokens

    async def settle(self, reserved, used):
        async with self.lock:
            self.reserved -= reserved
            self.budget.tokens_used += used
