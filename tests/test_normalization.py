from job_market.normalization import canonical_location_key, normalize_city_name
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
