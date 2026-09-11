import asyncio
from datetime import UTC, datetime, timedelta


class BudgetExhausted(RuntimeError):
    pass


class BudgetTracker:
    def __init__(self, budget, final_stage_token_reserve=0, deadline_reserve_s=0):
        self.budget = budget
        self.deadline_reserve_s = deadline_reserve_s
        self.condition = asyncio.Condition()
        self.reserved = 0
        self.final_stage_token_reserve = final_stage_token_reserve

    @property
    def deadline_at(self):
        return self.budget.deadline_at - timedelta(seconds=self.deadline_reserve_s)

    async def reserve(self, tokens, *, stage=None):
        # Final stages share the operator's protected allowance. Gates may still
        # verify blockers immediately using the verification tier.
        protected = (
            self.final_stage_token_reserve
            if stage not in {"system_context", "verification"}
            else 0
        )
        ceiling = self.budget.token_ceiling - protected
        async with self.condition:
            while True:
                remaining = (self.deadline_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0 or self.budget.tokens_used + tokens > ceiling:
                    raise BudgetExhausted("Review budget exhausted")
                if self.budget.tokens_used + self.reserved + tokens <= ceiling:
                    self.reserved += tokens
                    return
                # In-flight calls may return unused reservations. Do not turn
                # temporary contention into permanently missing coverage.
                try:
                    await asyncio.wait_for(self.condition.wait(), remaining)
                except TimeoutError:
                    raise BudgetExhausted("Review budget exhausted") from None

    async def settle(self, reserved, used):
        async with self.condition:
            self.reserved -= reserved
            self.budget.tokens_used += used
            self.condition.notify_all()
