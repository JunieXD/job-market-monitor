from collections import namedtuple

from job_market.runtime_checks import RuntimeChecker


def test_runtime_check_rejects_missing_raw_directory(tmp_path) -> None:
    report = RuntimeChecker(
        tmp_path / "missing",
        minimum_free_gib=5,
    ).run()

    assert report["ok"] is False
    assert report["violations"] == ["raw_data_dir_missing"]


def test_runtime_check_rejects_low_free_space(tmp_path, monkeypatch) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "job_market.runtime_checks.shutil.disk_usage",
        lambda _path: usage(20 * 1024**3, 18 * 1024**3, 2 * 1024**3),
    )

    report = RuntimeChecker(tmp_path, minimum_free_gib=5).run()

    assert report["ok"] is False
    assert report["free_gib"] == 2
    assert report["violations"] == ["raw_data_disk_space_low"]

