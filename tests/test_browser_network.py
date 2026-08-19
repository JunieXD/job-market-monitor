from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from job_market.browser_network import BLOCKED_URL_PATTERNS, BrowserNetworkMetrics


async def test_network_policy_uses_cdp_without_disabling_browser_cache() -> None:
    metrics = BrowserNetworkMetrics()
    context = SimpleNamespace(route=AsyncMock())
    session = SimpleNamespace(on=Mock(), send=AsyncMock())
    context.new_cdp_session = AsyncMock(return_value=session)
    page = SimpleNamespace(context=context)

    await metrics.install_policy(context)
    await metrics.attach_page(page)

    context.route.assert_not_awaited()
    assert session.send.await_args_list[0].args == ("Network.enable",)
    assert session.send.await_args_list[1].args == (
        "Network.setBlockedURLs",
        {"urls": list(BLOCKED_URL_PATTERNS)},
    )


async def test_network_metrics_separate_blocked_and_real_failures() -> None:
    metrics = BrowserNetworkMetrics()
    metrics._record_request({"requestId": "image-1", "type": "Image"})
    metrics._record_request({"requestId": "xhr-1", "type": "XHR"})
    metrics._record_response({})
    metrics._record_finished({"requestId": "xhr-1", "encodedDataLength": 123.6})
    metrics._record_failed({"requestId": "image-1", "errorText": "net::ERR_FAILED"})
    metrics._record_failed({"requestId": "xhr-2", "errorText": "net::ERR_TIMED_OUT"})

    assert await metrics.snapshot() == {
        "request_count": 2,
        "response_count": 1,
        "received_bytes": 124,
        "failed_request_count": 1,
        "blocked_request_count": 1,
        "attachment_error_count": 0,
        "request_counts_by_type": {"image": 1, "xhr": 1},
        "received_bytes_by_type": {"xhr": 124},
    }


async def test_network_metrics_report_background_attachment_failure() -> None:
    metrics = BrowserNetworkMetrics()
    page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock()))
    page.context.new_cdp_session.side_effect = RuntimeError("page already closed")

    metrics._schedule_attach(page)
    await metrics.snapshot()

    assert metrics.attachment_error_count == 1
