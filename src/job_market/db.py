from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text

from job_market.config import Settings
from job_market.models import Base

LEGACY_REVISION = "0001"
LEGACY_TABLES = {
    "crawl_runs",
    "job_locations",
    "job_observations",
    "job_versions",
    "jobs",
    "locations",
    "raw_snapshots",
    "sources",
}
ANALYTICS_VIEWS = {
    "daily_category_stats",
    "daily_city_stats",
    "daily_company_stats",
    "daily_market_category_stats",
    "daily_market_city_stats",
}
SCHEMA_ADVISORY_LOCK_KEY = 1_905_202_027


def make_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def create_schema(engine: Engine) -> None:
    config = _migration_config()

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": SCHEMA_ADVISORY_LOCK_KEY},
            )
        config.attributes["connection"] = connection
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" not in tables and tables:
            missing = LEGACY_TABLES - tables
            unexpected = tables - LEGACY_TABLES
            if missing or unexpected:
                raise RuntimeError(
                    "Refusing to stamp an unknown database schema: "
                    f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
                )
            command.stamp(config, LEGACY_REVISION)
        command.upgrade(config, "head")


def check_schema(engine: Engine) -> dict[str, object]:
    config = _migration_config()
    expected_revision = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_revision = context.get_current_revision()
        drift = [str(item) for item in compare_metadata(context, Base.metadata)]
        actual_views = set(inspect(connection).get_view_names())
    missing_views = sorted(ANALYTICS_VIEWS - actual_views)
    unexpected_views = sorted(actual_views - ANALYTICS_VIEWS)
    return {
        "ok": (
            current_revision == expected_revision
            and not drift
            and not missing_views
            and not unexpected_views
        ),
        "current_revision": current_revision,
        "expected_revision": expected_revision,
        "model_drift": drift,
        "missing_views": missing_views,
        "unexpected_views": unexpected_views,
    }


def _migration_config() -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).with_name("migrations")),
    )
    return config
