import gzip
import json

from job_market.raw_store import RawStore
from job_market.schemas import Channel


def test_raw_store_writes_compressed_payload(tmp_path) -> None:
    snapshot = RawStore(tmp_path, "run-1", "bytedance_cn").save(
        channel=Channel.CAMPUS,
        partition="all",
        offset=0,
        payload={"code": 0, "data": {"job_post_list": []}},
    )

    path = tmp_path / snapshot.path
    assert path.exists()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert json.load(handle)["code"] == 0


def test_raw_store_separates_sources(tmp_path) -> None:
    snapshot = RawStore(tmp_path, "run-2", "alibaba_cn").save(
        channel=Channel.CAMPUS,
        partition="batch-示例",
        offset=10,
        payload={"success": True, "content": {"datas": []}},
    )

    assert snapshot.path.startswith("alibaba_cn/")
    assert (tmp_path / snapshot.path).exists()
