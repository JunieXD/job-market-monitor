from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from job_market.analytics import AnalyticsRepository
from job_market.analytics_contracts import (
    AnalyticsEnvelope,
    AnalyticsMeta,
    CoverageSummary,
)
from job_market.config import Settings
from job_market.db import make_engine
from job_market.health import SourceHealthChecker


def create_app(
    engine: Engine | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    owns_engine = engine is None
    app_engine = engine or make_engine(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owns_engine:
            app_engine.dispose()

    app = FastAPI(
        title="就业市场监测器 API",
        description="提供岗位趋势、分类、城市、岗位明细和数据覆盖信息的只读接口。",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.engine = app_engine
    app.state.settings = app_settings

    @app.get("/healthz", tags=["运行状态"])
    def healthz(request: Request) -> dict[str, str]:
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="数据库不可用") from exc
        return {"status": "ok"}

    @app.get("/api/v1/meta/companies", tags=["元数据"])
    def companies(request: Request) -> dict[str, list[dict[str, Any]]]:
        rows = _query_rows(
            request.app.state.engine,
            """
            SELECT c.key, c.name, COUNT(DISTINCT s.id) AS source_count
            FROM companies AS c
            LEFT JOIN sources AS s ON s.company_id = c.id AND s.enabled
            GROUP BY c.key, c.name
            ORDER BY c.name
            """,
        )
        return {"data": rows}

    @app.get("/api/v1/meta/sources", tags=["元数据"])
    def sources(request: Request) -> dict[str, list[dict[str, Any]]]:
        rows = _query_rows(
            request.app.state.engine,
            """
            SELECT s.key, s.display_name, s.company_name, s.base_url,
                   s.scope_name, s.timezone, sc.channel, sc.status
            FROM sources AS s
            LEFT JOIN source_channels AS sc ON sc.source_id = s.id
            WHERE s.enabled
            ORDER BY s.key, sc.channel
            """,
        )
        return {"data": rows}

    @app.get("/api/v1/meta/channels", tags=["元数据"])
    def channels(request: Request) -> dict[str, list[dict[str, Any]]]:
        rows = _query_rows(
            request.app.state.engine,
            """
            SELECT DISTINCT channel
            FROM source_channels
            WHERE status = 'active'
            ORDER BY channel
            """,
        )
        return {"data": rows}

    @app.get("/api/v1/meta/categories", tags=["元数据"])
    def categories(request: Request) -> dict[str, list[dict[str, Any]]]:
        rows = _query_rows(
            request.app.state.engine,
            """
            SELECT sc.external_id, sc.name, s.key AS source_key,
                   s.company_name, sc.parent_id
            FROM source_categories AS sc
            JOIN sources AS s ON s.id = sc.source_id
            WHERE s.enabled
            ORDER BY s.key, sc.name
            """,
        )
        return {"data": rows}

    @app.get("/api/v1/meta/locations", tags=["元数据"])
    def locations(request: Request) -> dict[str, list[dict[str, Any]]]:
        rows = _query_rows(
            request.app.state.engine,
            """
            SELECT l.code, l.name, s.key AS source_key, s.company_name,
                   l.country_name, l.state_name
            FROM locations AS l
            JOIN sources AS s ON s.id = l.source_id
            WHERE s.enabled
            ORDER BY l.name, s.key
            """,
        )
        return {"data": rows}

    @app.get("/api/v1/overview", response_model=AnalyticsEnvelope, tags=["分析"])
    def overview(
        request: Request,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        coverage = analytics.coverage(snapshot_date)
        selected_date = coverage["snapshot_date"]
        filters = {"snapshot_date": selected_date, "channel": channel}
        rows = analytics._query(
            "daily_company_stats",
            snapshot_date=selected_date,
            channel=channel,
        )
        return _envelope(
            rows,
            coverage=coverage,
            filters=filters,
            metric_definition="daily_company_source_breakdown",
        )

    @app.get("/api/v1/trends/market", response_model=AnalyticsEnvelope, tags=["分析"])
    def market_trend(
        request: Request,
        channel: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        rows = analytics.company_trends(
            channel=channel,
            start_date=start_date,
            end_date=end_date,
        )
        coverage = analytics.coverage(end_date)
        return _envelope(
            rows,
            coverage=coverage,
            filters={
                "channel": channel,
                "start_date": start_date,
                "end_date": end_date,
            },
            metric_definition="daily_company_source_trend",
        )

    @app.get("/api/v1/trends/companies", response_model=AnalyticsEnvelope, tags=["分析"])
    def company_trend(
        request: Request,
        company_key: str | None = None,
        channel: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        rows = analytics.company_trends(
            company_key=company_key,
            channel=channel,
            start_date=start_date,
            end_date=end_date,
        )
        coverage = analytics.coverage(end_date)
        return _envelope(
            rows,
            coverage=coverage,
            filters={
                "company_key": company_key,
                "channel": channel,
                "start_date": start_date,
                "end_date": end_date,
            },
            metric_definition="daily_company_source_trend",
        )

    @app.get(
        "/api/v1/distributions/categories",
        response_model=AnalyticsEnvelope,
        tags=["分析"],
    )
    def category_distribution(
        request: Request,
        company_key: str | None = None,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        if company_key is None:
            rows = analytics.market_category_distribution(
                snapshot_date=snapshot_date,
                channel=channel,
            )
            metric = "market_category_distribution"
        else:
            rows = analytics.category_distribution(
                company_key=company_key,
                snapshot_date=snapshot_date,
                channel=channel,
            )
            metric = "source_category_distribution"
        coverage = analytics.coverage(snapshot_date)
        return _envelope(
            rows,
            coverage=coverage,
            filters={
                "company_key": company_key,
                "snapshot_date": snapshot_date,
                "channel": channel,
            },
            metric_definition=metric,
        )

    @app.get(
        "/api/v1/distributions/cities",
        response_model=AnalyticsEnvelope,
        tags=["分析"],
    )
    def city_distribution(
        request: Request,
        company_key: str | None = None,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        rows = analytics.city_distribution(
            company_key=company_key,
            snapshot_date=snapshot_date,
            channel=channel,
        )
        coverage = analytics.coverage(snapshot_date)
        return _envelope(
            rows,
            coverage=coverage,
            filters={
                "company_key": company_key,
                "snapshot_date": snapshot_date,
                "channel": channel,
            },
            metric_definition=(
                "company_city_distribution"
                if company_key is not None
                else "market_city_distribution"
            ),
        )

    @app.get(
        "/api/v1/companies/{company_key}/summary",
        response_model=AnalyticsEnvelope,
        tags=["分析"],
    )
    def company_summary(
        company_key: str,
        request: Request,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        coverage = analytics.coverage(snapshot_date)
        selected_date = coverage["snapshot_date"]
        rows = [
            {
                "trend": analytics.company_trends(
                    company_key=company_key,
                    channel=channel,
                ),
                "categories": analytics.category_distribution(
                    company_key=company_key,
                    snapshot_date=selected_date,
                    channel=channel,
                ),
                "cities": analytics.city_distribution(
                    company_key=company_key,
                    snapshot_date=selected_date,
                    channel=channel,
                ),
            }
        ]
        return _envelope(
            rows,
            coverage=coverage,
            filters={
                "company_key": company_key,
                "snapshot_date": selected_date,
                "channel": channel,
            },
            metric_definition="company_summary",
        )

    @app.get("/api/v1/jobs", response_model=AnalyticsEnvelope, tags=["岗位"])
    def jobs(
        request: Request,
        company_key: str | None = None,
        source_key: str | None = None,
        channel: str | None = None,
        status: str = "active",
        query: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100000),
    ) -> AnalyticsEnvelope:
        filters = ["j.status = :status"]
        params: dict[str, Any] = {"status": status, "limit": limit, "offset": offset}
        if company_key is not None:
            filters.append("c.key = :company_key")
            params["company_key"] = company_key
        if source_key is not None:
            filters.append("s.key = :source_key")
            params["source_key"] = source_key
        if channel is not None:
            filters.append("j.channel = :channel")
            params["channel"] = channel
        if query is not None and query.strip():
            filters.append("(j.title LIKE :query OR j.description LIKE :query)")
            params["query"] = f"%{query.strip()}%"
        where = " AND ".join(filters)
        engine = request.app.state.engine
        rows = _query_rows(
            engine,
            f"""
            SELECT j.external_id, s.key AS source_key, c.key AS company_key,
                   c.name AS company_name, j.channel, j.title, j.source_url,
                   j.published_at, j.source_updated_at, j.status,
                   j.recruitment_count, j.first_seen_at, j.last_seen_at
            FROM jobs AS j
            JOIN sources AS s ON s.id = j.source_id
            JOIN companies AS c ON c.id = s.company_id
            WHERE {where}
            ORDER BY j.last_seen_at DESC, j.id DESC
            LIMIT :limit OFFSET :offset
            """,
            params,
        )
        total = _query_scalar(
            engine,
            f"""
            SELECT COUNT(*)
            FROM jobs AS j
            JOIN sources AS s ON s.id = j.source_id
            JOIN companies AS c ON c.id = s.company_id
            WHERE {where}
            """,
            params,
        )
        analytics = AnalyticsRepository(engine)
        return _envelope(
            rows,
            coverage=analytics.coverage(),
            filters={
                "company_key": company_key,
                "source_key": source_key,
                "channel": channel,
                "status": status,
                "query": query,
            },
            metric_definition="current_job_list",
            pagination={"limit": limit, "offset": offset, "total": int(total or 0)},
        )

    @app.get(
        "/api/v1/jobs/{source_key}/{external_id}",
        tags=["岗位"],
    )
    def job_detail(source_key: str, external_id: str, request: Request) -> dict[str, Any]:
        rows = _query_rows(
            request.app.state.engine,
            """
            SELECT j.external_id, s.key AS source_key, c.key AS company_key,
                   c.name AS company_name, j.channel, j.title, j.description,
                   j.requirements, j.source_url, j.published_at,
                   j.source_updated_at, j.source_status, j.recruitment_count,
                   j.degree_code, j.degree_name, j.experience_min_years,
                   j.experience_max_years, j.graduation_start_at,
                   j.graduation_end_at, j.department_code, j.department_name,
                   j.interview_location_names, j.status, j.first_seen_at,
                   j.last_seen_at, j.last_changed_at, j.closed_at
            FROM jobs AS j
            JOIN sources AS s ON s.id = j.source_id
            JOIN companies AS c ON c.id = s.company_id
            WHERE s.key = :source_key AND j.external_id = :external_id
            """,
            {"source_key": source_key, "external_id": external_id},
        )
        if not rows:
            raise HTTPException(status_code=404, detail="岗位不存在")
        return rows[0]

    @app.get("/api/v1/quality/source-health", tags=["质量"])
    def source_health(request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        return SourceHealthChecker(
            request.app.state.engine,
            stale_after=timedelta(hours=settings.source_stale_after_hours),
        ).run()

    @app.get(
        "/api/v1/quality/coverage",
        response_model=AnalyticsEnvelope,
        tags=["质量"],
    )
    def coverage(
        request: Request,
        snapshot_date: date | None = None,
    ) -> AnalyticsEnvelope:
        analytics = AnalyticsRepository(request.app.state.engine)
        result = analytics.coverage(snapshot_date)
        return _envelope(
            [result],
            coverage=result,
            filters={"snapshot_date": snapshot_date},
            metric_definition="source_coverage",
        )

    return app


def _envelope(
    rows: list[dict[str, Any]],
    *,
    coverage: dict[str, Any],
    filters: dict[str, Any],
    metric_definition: str,
    pagination: dict[str, int] | None = None,
) -> AnalyticsEnvelope:
    coverage_model = CoverageSummary.model_validate(coverage)
    selected_date = coverage_model.snapshot_date
    return AnalyticsEnvelope(
        data=rows,
        meta=AnalyticsMeta(
            snapshot_date=selected_date,
            filters=filters,
            coverage=coverage_model,
            metric_definition=metric_definition,
            pagination=pagination,
        ),
    )


def _query_rows(
    engine: Engine,
    statement: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text(statement), params or {}).mappings()
        ]


def _query_scalar(
    engine: Engine,
    statement: str,
    params: dict[str, Any] | None = None,
) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(statement), params or {}).scalar_one()
