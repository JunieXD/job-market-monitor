from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


def timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Alibaba {field} timestamp is not numeric: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def time_range(
    value: Any,
    field: str,
) -> tuple[datetime | None, datetime | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"Alibaba {field} must be an object: {value!r}")
    return (
        timestamp_ms(value.get("from"), f"{field}.from"),
        timestamp_ms(value.get("to"), f"{field}.to"),
    )


def number_range(value: Any, field: str) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"Alibaba {field} must be an object: {value!r}")
    return (
        _nonnegative_int(value.get("from"), f"{field}.from"),
        _nonnegative_int(value.get("to"), f"{field}.to"),
    )


def coded_label(
    value: Any,
    field: str,
    *,
    string_is_name: bool = False,
) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None, None
        return (None, normalized) if string_is_name else (normalized, None)
    if not isinstance(value, dict):
        raise ValueError(f"Alibaba {field} must be a string or object: {value!r}")
    code = next(
        (str(value[key]).strip() for key in ("code", "value", "id") if value.get(key)),
        None,
    )
    name = next(
        (str(value[key]).strip() for key in ("name", "label") if value.get(key)),
        None,
    )
    if code is None and name is None:
        raise ValueError(f"Alibaba {field} object has no code or name")
    return code, name


def string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"Alibaba {field} must be a list or string: {value!r}")
    return [str(item).strip() for item in value if str(item).strip()]


def unique_strings(value: Any, field: str) -> list[str]:
    return list(dict.fromkeys(string_list(value, field)))


def canonical_position_url(value: Any, fallback: str, base_url: str) -> str:
    raw_url = str(value).strip() if value else fallback
    absolute = urljoin(base_url, raw_url)
    parsed = urlsplit(absolute)
    query = urlencode(
        [(key, item) for key, item in parse_qsl(parsed.query) if key != "track_id"]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Alibaba {field} must be numeric: {value!r}")
    converted = int(value)
    if converted != value or converted < 0:
        raise ValueError(f"Alibaba {field} must be a non-negative integer: {value!r}")
    return converted
