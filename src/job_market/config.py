from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = (
        "postgresql+psycopg://job_market:change-me-before-deploying@postgres:5432/job_market"
    )
    raw_data_dir: Path = Path("/data/raw")
    bytedance_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    alibaba_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    tencent_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    meituan_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    jd_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    netease_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    xiaomi_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    huawei_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    kuaishou_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    kuaishou_campus_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    baidu_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    didi_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    bilibili_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    pdd_request_delay_seconds: float = Field(default=0.5, ge=0.5, le=30)
    ant_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    ctrip_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    qihu360_request_delay_seconds: float = Field(default=0.5, ge=0.5, le=30)
    oppo_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    vivo_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    xiaohongshu_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    iqiyi_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    cainiao_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    alibaba_international_request_delay_seconds: float = Field(
        default=1.5,
        ge=0.5,
        le=30,
    )
    aliyun_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    beike_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    tongcheng_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    lenovo_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30)
    crawl_channel_timeout_seconds: int = Field(default=2700, ge=60, le=14400)
    source_stale_after_hours: int = Field(default=36, ge=24, le=720)
    abandoned_run_after_minutes: int = Field(default=180, ge=1, le=1440)
    raw_min_free_gib: float = Field(default=5.0, ge=0.1, le=1024)
    missing_runs_before_close: int = Field(default=2, ge=2, le=30)
    daily_crawl_hour: int = Field(default=3, ge=0, le=23)
    daily_crawl_minute: int = Field(default=15, ge=0, le=59)
    daily_crawl_timezone: str = "Asia/Shanghai"
    crawl_block_nonessential_resources: bool = True
    crawl_block_service_workers: bool = True
    headless: bool = True
