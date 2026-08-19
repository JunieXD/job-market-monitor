import json
from io import StringIO

from job_market.observability import log_event


def test_json_log_event_bounds_strings_and_lists() -> None:
    output = StringIO()

    log_event(
        "test_event",
        stream=output,
        error="x" * 3000,
        items=list(range(150)),
    )

    payload = json.loads(output.getvalue())
    assert payload["event"] == "test_event"
    assert len(payload["error"]) == 2000
    assert len(payload["items"]) == 100
