from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class CoverageSummary(BaseModel):
    snapshot_date: date | None
    configured_source_channel_count: int = Field(ge=0)
    standard_snapshot_count: int = Field(ge=0)
    successful_source_channel_count: int = Field(ge=0)
    absence_authoritative_source_channel_count: int = Field(ge=0)
    non_authoritative_successful_run_count: int = Field(ge=0)
    failed_run_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)


class AnalyticsMeta(BaseModel):
    snapshot_date: date | None
    timezone: str = "Asia/Shanghai"
    filters: dict[str, Any] = Field(default_factory=dict)
    coverage: CoverageSummary
    metric_definition: str


class AnalyticsEnvelope(BaseModel):
    data: list[dict[str, Any]]
    meta: AnalyticsMeta
