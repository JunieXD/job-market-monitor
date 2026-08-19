import asyncio

import pytest

from job_market.connectors.browser_json import (
    BrowserResponseUnavailableError,
    CapturedJsonResponse,
    next_json_payload,
)


async def test_next_json_payload_returns_captured_object() -> None:
    queue = asyncio.Queue()
    queue.put_nowait(asyncio.create_task(_capture({"status": "ok"})))

    payload = await next_json_payload(queue, timeout_seconds=1, operation="test")

    assert payload == {"status": "ok"}


async def test_next_json_payload_rejects_non_object() -> None:
    queue = asyncio.Queue()
    queue.put_nowait(asyncio.create_task(_capture(["not", "an", "object"])))

    with pytest.raises(RuntimeError, match="not a JSON object"):
        await next_json_payload(queue, timeout_seconds=1, operation="test")


async def test_next_json_payload_reports_empty_queue() -> None:
    queue = asyncio.Queue()

    with pytest.raises(BrowserResponseUnavailableError, match="Timed out"):
        await next_json_payload(queue, timeout_seconds=0.01, operation="test")


async def _capture(payload: object) -> CapturedJsonResponse:
    return CapturedJsonResponse(payload=payload)
