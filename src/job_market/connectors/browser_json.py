"""Safely retain JSON responses emitted by browser-rendered career sites."""

import asyncio
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Response


@dataclass(frozen=True)
class CapturedJsonResponse:
    payload: Any | None = None
    error: Exception | None = None


type JsonResponseQueue = asyncio.Queue[asyncio.Task[CapturedJsonResponse]]


class BrowserResponseUnavailableError(RuntimeError):
    """A matching browser response was absent or its body could not be retained."""


def enqueue_json_response(queue: JsonResponseQueue, response: Response) -> None:
    """Start reading a response body while Chromium still retains it."""

    queue.put_nowait(asyncio.create_task(_read_json_response(response)))


async def next_json_payload(
    queue: JsonResponseQueue,
    *,
    timeout_seconds: float,
    operation: str,
) -> dict[str, Any]:
    payload = await next_json_response(
        queue,
        timeout_seconds=timeout_seconds,
        operation=operation,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Response for {operation} is not a JSON object")
    return payload


async def next_json_response(
    queue: JsonResponseQueue,
    *,
    timeout_seconds: float,
    operation: str,
) -> Any:
    """Return any JSON value captured from a page response.

    Most career portals wrap results in an object, but a few legacy portals
    return a top-level array.  Keeping the queue and body-retention behavior
    shared lets those sources validate their own response shape without
    weakening the object-only helper used by existing connectors.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    unreadable_count = 0
    last_error: Exception | None = None
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                capture_task = await queue.get()
                capture = await capture_task
        except TimeoutError:
            break
        if capture.error is not None:
            # Some SPAs issue a replacement request while navigating and
            # Chromium releases the first response body. A later matching
            # response remains authoritative and can still be consumed.
            unreadable_count += 1
            last_error = capture.error
            continue
        return capture.payload

    suffix = (
        f" after {unreadable_count} unreadable matching response(s)"
        if unreadable_count
        else ""
    )
    raise BrowserResponseUnavailableError(
        f"Timed out waiting for {operation}{suffix}"
    ) from last_error


def drain_json_responses(queue: JsonResponseQueue) -> None:
    while not queue.empty():
        task = queue.get_nowait()
        if not task.done():
            task.cancel()


async def _read_json_response(response: Response) -> CapturedJsonResponse:
    try:
        return CapturedJsonResponse(payload=await response.json())
    except Exception as exc:
        return CapturedJsonResponse(error=exc)
