from collections import defaultdict
from typing import Any

from job_market.schemas import JobRecord, SourceFieldStatRecord


def profile_source_fields(jobs: list[JobRecord]) -> list[SourceFieldStatRecord]:
    """Summarize raw source-field availability without retaining example values."""

    row_count = len(jobs)
    counters: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "present_count": 0,
            "non_null_count": 0,
            "non_empty_count": 0,
            "type_counts": defaultdict(int),
        }
    )
    for job in jobs:
        for path, value in _flatten(job.source_payload):
            counter = counters[path]
            counter["present_count"] += 1
            value_type = _json_type(value)
            counter["type_counts"][value_type] += 1
            if value is not None:
                counter["non_null_count"] += 1
                if not _is_empty(value):
                    counter["non_empty_count"] += 1

    return [
        SourceFieldStatRecord(
            field_path=path,
            row_count=row_count,
            present_count=counter["present_count"],
            non_null_count=counter["non_null_count"],
            non_empty_count=counter["non_empty_count"],
            type_counts=dict(sorted(counter["type_counts"].items())),
        )
        for path, counter in sorted(counters.items())
    ]


def _flatten(payload: dict[str, Any], *, max_depth: int = 2) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []

    def visit(value: Any, prefix: str, depth: int) -> None:
        if prefix:
            values.append((prefix, value))
        if depth >= max_depth or not isinstance(value, dict):
            return
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            visit(child, path, depth + 1)

    visit(payload, "", 0)
    return values


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_empty(value: Any) -> bool:
    return value == "" or value == [] or value == {}
