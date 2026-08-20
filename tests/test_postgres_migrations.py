import os
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateSchema, DropSchema

from job_market.db import create_schema
from job_market.models import Base


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is only configured by the PostgreSQL CI job",
)
def test_postgres_migrations_match_models_and_views() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"jmm_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))

    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        create_schema(engine)
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            drift = compare_metadata(
                MigrationContext.configure(connection),
                Base.metadata,
            )
        assert revision == "0016"
        assert drift == []
        columns = {item["name"]: item for item in inspect(engine).get_columns("jobs")}
        assert columns["external_code"]["nullable"] is True
        assert columns["published_at"]["nullable"] is True
        assert columns["is_hot"]["nullable"] is True
        assert columns["source_updated_at"]["nullable"] is True
        assert columns["source_status"]["nullable"] is True
        assert columns["category_id"]["nullable"] is True
        assert columns["category_name"]["nullable"] is True
        assert columns["interview_location_names"]["nullable"] is False
        assert {
            "source_channels",
            "job_version_source_categories",
            "crawl_run_field_stats",
        }.issubset(inspect(engine).get_table_names())
        assert set(inspect(engine).get_view_names()) == {
            "daily_category_stats",
            "daily_city_stats",
            "daily_company_stats",
            "daily_market_category_stats",
            "daily_market_city_stats",
        }
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()
