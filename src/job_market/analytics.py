from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


class AnalyticsRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def company_trend(
        self,
        *,
        company_key: str,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._query(
            "daily_company_stats",
            company_key=company_key,
            channel=channel,
        )

    def category_distribution(
        self,
        *,
        company_key: str,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._query(
            "daily_category_stats",
            company_key=company_key,
            snapshot_date=snapshot_date,
            channel=channel,
        )

    def market_category_distribution(
        self,
        *,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._query(
            "daily_market_category_stats",
            snapshot_date=snapshot_date,
            channel=channel,
        )

    def city_distribution(
        self,
        *,
        company_key: str | None = None,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        view = "daily_city_stats" if company_key is not None else "daily_market_city_stats"
        return self._query(
            view,
            company_key=company_key,
            snapshot_date=snapshot_date,
            channel=channel,
        )

    def _query(self, view: str, **filters: object) -> list[dict[str, Any]]:
        allowed_views = {
            "daily_company_stats",
            "daily_category_stats",
            "daily_city_stats",
            "daily_market_category_stats",
            "daily_market_city_stats",
        }
        if view not in allowed_views:
            raise ValueError(f"Unsupported analytics view: {view}")
        active_filters = {key: value for key, value in filters.items() if value is not None}
        clauses = [f"{key} = :{key}" for key in active_filters]
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        statement = text(f"SELECT * FROM {view}{where} ORDER BY snapshot_date")
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(statement, active_filters).mappings()
            ]
