import hashlib
import re
import unicodedata

from job_market.schemas import LocationRecord

_LOCATION_PART_RE = re.compile(r"[·•・‧∙/／|｜,，、;；]+|[-－—]+")
_REGION_PREFIX_RE = re.compile(
    r"^[\u4e00-\u9fff]{2,20}(?:省|自治区|特别行政区|自治州|地区|盟)"
    r"[\s·•・‧∙/／|｜,，、;；:：\-－—]*(?P<city>.+)$"
)
_CITY_WITH_DISTRICT_RE = re.compile(
    r"^(?P<city>.+?市)(?:.+(?:区|县|旗|镇|街道))$"
)
_REGION_SUFFIXES = (
    "特别行政区",
    "自治区",
    "自治州",
    "地区",
    "省",
    "盟",
)
_WORKPLACE_SUFFIXES = (
    "经济技术开发区",
    "经济开发区",
    "工业园区",
    "科技园区",
    "产业园区",
    "研发中心",
    "技术中心",
    "运营中心",
    "分公司",
    "子公司",
    "办事处",
    "代表处",
    "总部",
    "高新区",
    "经开区",
    "开发区",
    "新区",
)
_NON_CITY_REGIONS = frozenset(
    {
        "中国",
        "中国大陆",
        "中国内地",
        "全国",
        "全国各地",
        "境内",
        "海外",
        "全球",
        "华东",
        "华南",
        "华北",
        "华中",
        "西南",
        "西北",
        "东北",
    }
)


def _is_region_part(value: str) -> bool:
    return value in _NON_CITY_REGIONS or value.endswith(_REGION_SUFFIXES)


def _strip_workplace_suffixes(value: str) -> str:
    candidate = value
    changed = True
    while changed:
        changed = False
        for suffix in _WORKPLACE_SUFFIXES:
            if candidate.endswith(suffix) and len(candidate) > len(suffix):
                candidate = candidate[: -len(suffix)].rstrip(
                    " ·•・‧∙/／|｜,，、;；:-－—"
                )
                changed = True
                break
    return candidate


def _city_part(value: str) -> str:
    parts = [part.strip() for part in _LOCATION_PART_RE.split(value) if part.strip()]
    if len(parts) > 1:
        region_indexes = [
            index for index, part in enumerate(parts[:-1]) if _is_region_part(part)
        ]
        if region_indexes:
            tail = parts[region_indexes[-1] + 1 :]
            marked_city = [part for part in tail if part.endswith("市")]
            if len(marked_city) == 1:
                return marked_city[0]
            if len(tail) == 1 or len(set(tail)) == 1 or tail[-1].endswith(
                _WORKPLACE_SUFFIXES
            ):
                return tail[0]
    match = _REGION_PREFIX_RE.match(value)
    if match is not None:
        return match.group("city")
    return value


def normalize_city_name(name: str) -> str:
    """Normalize a source location label to a city-level display name.

    Source labels remain unchanged in the fact tables. This function only
    derives the city dimension used for matching and display. It removes
    unambiguous province/region prefixes and workplace or district suffixes,
    while leaving province-only and otherwise ambiguous labels untouched.
    """

    normalized = " ".join(
        unicodedata.normalize("NFKC", name).strip().casefold().split()
    )
    if not any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return normalized

    candidate = _city_part(normalized)
    candidate = _strip_workplace_suffixes(candidate)
    district_match = _CITY_WITH_DISTRICT_RE.match(candidate)
    if district_match is not None:
        candidate = district_match.group("city")
    if candidate.endswith("特别行政区") and len(candidate) > len("特别行政区"):
        candidate = candidate[: -len("特别行政区")]
    if len(candidate) > 1 and candidate.endswith("市"):
        candidate = candidate[:-1]
    return candidate.rstrip(" ·•・‧∙/／|｜,，、;；:-－—")


def is_city_level_name(name: str) -> bool:
    """Return whether a label contains a concrete city for auto-mapping."""

    normalized = normalize_city_name(name)
    return bool(normalized) and normalized not in _NON_CITY_REGIONS and not _is_region_part(
        normalized
    )


def canonical_location_key(location: LocationRecord) -> str:
    # Some sources expose only a city label while others also expose country
    # and state. Including optional metadata in identity would split the same
    # city across companies. Ambiguous same-name cities can be overridden by a
    # published manual mapping without modifying source facts.
    normalized_name = normalize_city_name(location.name)
    if not normalized_name:
        raise ValueError("Cannot canonicalize an empty city name")
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()
    return f"city-name-{digest[:24]}"
