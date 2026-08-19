from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from playwright.async_api import BrowserContext, CDPSession, Page

BLOCKED_RESOURCE_TYPES = frozenset({"font", "image", "media"})
BLOCKED_URL_PATTERNS = tuple(
    f"*.{extension}*"
    for extension in (
        "avif",
        "avi",
        "bmp",
        "eot",
        "gif",
        "ico",
        "jpeg",
        "jpg",
        "m4a",
        "mov",
        "mp3",
        "mp4",
        "ogg",
        "otf",
        "png",
        "svg",
        "ttf",
        "wav",
        "webm",
        "webp",
        "woff",
        "woff2",
    )
)


class BrowserNetworkMetrics:
    """Collect encoded browser traffic without retaining response bodies."""

    def __init__(self) -> None:
        self.request_count = 0
        self.response_count = 0
        self.received_bytes = 0
        self.failed_request_count = 0
        self.blocked_request_count = 0
        self.attachment_error_count = 0
        self._sessions: list[CDPSession] = []
        self._attached_pages: set[int] = set()
        self._attach_tasks: set[asyncio.Task[None]] = set()
        self._request_resource_types: dict[str, str] = {}
        self._request_counts_by_type: Counter[str] = Counter()
        self._received_bytes_by_type: Counter[str] = Counter()
        self._block_nonessential_resources = False

    async def install_policy(self, _: BrowserContext) -> None:
        # Playwright routing disables the browser HTTP cache. Apply URL blocking
        # through CDP instead so repeated list-page navigations can reuse scripts.
        self._block_nonessential_resources = True

    async def attach_page(self, page: Page) -> None:
        page_identity = id(page)
        if page_identity in self._attached_pages:
            return
        session = await page.context.new_cdp_session(page)
        session.on("Network.requestWillBeSent", self._record_request)
        session.on("Network.responseReceived", self._record_response)
        session.on("Network.loadingFinished", self._record_finished)
        session.on("Network.loadingFailed", self._record_failed)
        await session.send("Network.enable")
        if self._block_nonessential_resources:
            await session.send(
                "Network.setBlockedURLs",
                {"urls": list(BLOCKED_URL_PATTERNS)},
            )
        self._sessions.append(session)
        self._attached_pages.add(page_identity)

    def watch_new_pages(self, context: BrowserContext) -> None:
        context.on("page", self._schedule_attach)

    async def snapshot(self) -> dict[str, Any]:
        if self._attach_tasks:
            await asyncio.gather(*tuple(self._attach_tasks), return_exceptions=True)
        return {
            "request_count": self.request_count,
            "response_count": self.response_count,
            "received_bytes": self.received_bytes,
            "failed_request_count": self.failed_request_count,
            "blocked_request_count": self.blocked_request_count,
            "attachment_error_count": self.attachment_error_count,
            "request_counts_by_type": dict(sorted(self._request_counts_by_type.items())),
            "received_bytes_by_type": dict(
                sorted(self._received_bytes_by_type.items())
            ),
        }

    def _schedule_attach(self, page: Page) -> None:
        task = asyncio.create_task(self.attach_page(page))
        self._attach_tasks.add(task)
        task.add_done_callback(self._finish_attach)

    def _finish_attach(self, task: asyncio.Task[None]) -> None:
        self._attach_tasks.discard(task)
        if task.cancelled():
            return
        if task.exception() is not None:
            self.attachment_error_count += 1

    def _record_request(self, event: dict[str, Any]) -> None:
        self.request_count += 1
        request_id = event.get("requestId")
        resource_type = event.get("type")
        if isinstance(request_id, str) and isinstance(resource_type, str):
            normalized_type = resource_type.lower()
            self._request_resource_types[request_id] = normalized_type
            self._request_counts_by_type[normalized_type] += 1

    def _record_response(self, _: dict[str, Any]) -> None:
        self.response_count += 1

    def _record_finished(self, event: dict[str, Any]) -> None:
        encoded_length = event.get("encodedDataLength", 0)
        request_id = event.get("requestId")
        resource_type = (
            self._request_resource_types.pop(request_id, None)
            if isinstance(request_id, str)
            else None
        )
        if isinstance(encoded_length, (int, float)) and encoded_length > 0:
            rounded_length = round(encoded_length)
            self.received_bytes += rounded_length
            self._received_bytes_by_type[resource_type or "unknown"] += rounded_length

    def _record_failed(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        resource_type = (
            self._request_resource_types.pop(request_id, None)
            if isinstance(request_id, str)
            else None
        )
        if resource_type in BLOCKED_RESOURCE_TYPES:
            self.blocked_request_count += 1
            return
        if event.get("blockedReason") or event.get("errorText") == "net::ERR_BLOCKED_BY_CLIENT":
            self.blocked_request_count += 1
            return
        self.failed_request_count += 1
