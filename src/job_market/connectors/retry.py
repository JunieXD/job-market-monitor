from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from job_market.observability import log_event


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    source: str,
    operation_name: str,
    attempts: int = 3,
    base_delay_seconds: float = 1.0,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == attempts:
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1))
            log_event(
                "request_retry",
                level="warning",
                source=source,
                operation=operation_name,
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=delay,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise AssertionError("retry loop exited unexpectedly")
