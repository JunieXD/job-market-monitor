import json
from pathlib import Path

from job_market.connectors.alibaba_international import (
    AlibabaInternationalConnector,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cainiao_job.json"


def test_alibaba_international_uses_isolated_source_identity() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AlibabaInternationalConnector.parse_job(raw)

    assert record.source_key == "alibaba_international_social"
    assert record.company_name == "阿里国际数字商业集团"
    assert str(record.source_url) == (
        "https://aidc-jobs.alibaba.com/off-campus/position-detail"
        "?positionId=100009990001&positionType=social"
    )
    assert record.categories[0].name == "技术类-开发"
