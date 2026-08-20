from job_market.normalization import (
    canonical_location_key,
    is_city_level_name,
    normalize_city_name,
    normalize_city_names,
)
from job_market.schemas import LocationRecord


def test_city_identity_is_stable_when_optional_geography_is_missing() -> None:
    detailed = LocationRecord(
        code="CT_11",
        name="北京",
        country_name="中国大陆",
        state_name="北京",
    )
    name_only = LocationRecord(code="city:北京", name="北京")

    assert canonical_location_key(detailed) == canonical_location_key(name_only)


def test_city_name_normalization_handles_width_case_and_whitespace() -> None:
    assert normalize_city_name("  ＬＯＮＤＯＮ  ") == "london"


def test_city_name_normalization_removes_chinese_city_suffix() -> None:
    assert normalize_city_name("北京市") == "北京"
    assert normalize_city_name("上海") == "上海"
    assert normalize_city_name("New York City") == "new york city"


def test_city_name_normalization_extracts_city_from_region_and_workplace_labels() -> None:
    assert normalize_city_name("深圳总部") == "深圳"
    assert normalize_city_name("深圳市总部") == "深圳"
    assert normalize_city_name("四川省·成都") == "成都"
    assert normalize_city_name("四川省·成都市") == "成都"
    assert normalize_city_name("四川省成都市高新区") == "成都"
    assert normalize_city_name("深圳市南山区") == "深圳"
    assert normalize_city_name("广东省深圳市") == "深圳"
    assert normalize_city_name("中国大陆/北京/北京") == "北京"
    assert normalize_city_name("四川省 · 成都") == "成都"


def test_city_name_normalization_does_not_invent_a_city_from_regions() -> None:
    assert normalize_city_name("四川省") == "四川省"
    assert normalize_city_name("全国") == "全国"
    assert is_city_level_name("四川省") is False
    assert is_city_level_name("全国") is False
    assert normalize_city_names("/") == []
    assert is_city_level_name("/") is False
    assert is_city_level_name("四川省·成都") is True


def test_city_name_normalization_splits_city_lists_and_keeps_city_hierarchy() -> None:
    assert normalize_city_names("厦门市/福州市/漳州") == ["厦门", "福州", "漳州"]
    assert normalize_city_names("福建省·福州市/漳州市/厦门市/泉州市") == [
        "福州",
        "漳州",
        "厦门",
        "泉州",
    ]
    assert normalize_city_names("泉州市·晋江市") == ["泉州"]
    assert normalize_city_names("江苏省·南京市/徐州市/南通市/淮安市") == [
        "南京",
        "徐州",
        "南通",
        "淮安",
    ]
