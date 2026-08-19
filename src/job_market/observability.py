from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

MAX_LOG_STRING_LENGTH = 2000


def log_event(
    event: str,
    *,
    level: str = "info",
    stream: TextIO | None = None,
    **fields: Any,
) -> None:
    """Emit one bounded JSON log event without source payload contents."""

    payload = {
        "time": datetime.now(UTC).isoformat(),
        "level": level,
        "event": event,
        **{key: _bounded(value) for key, value in fields.items()},
    }
    output = stream or (sys.stderr if level in {"warning", "error"} else sys.stdout)
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        file=output,
        flush=True,
    )


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_LOG_STRING_LENGTH]
    if isinstance(value, dict):
        return {str(key): _bounded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded(item) for item in value[:100]]
    return value
