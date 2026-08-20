import hashlib
import re
import unicodedata

from job_market.china_cities import CHINA_CITY_ALIASES
from job_market.schemas import LocationRecord

_LOCATION_PART_RE = re.compile(r"[·•・‧∙/／|｜,，、;；]+|[-－—]+")
_LOCATION_LIST_RE = re.compile(r"[/／|｜,，、;；]+")
_LOCATION_HIERARCHY_RE = re.compile(r"[·•・‧∙]+")
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
_CITY_SUFFIX_EXCEPTIONS = frozenset({"芒市"})


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
    parts = [
        part.strip()
        for part in _LOCATION_HIERARCHY_RE.split(value)
        if part.strip()
    ]
    if len(parts) > 1:
        region_indexes = [
            index for index, part in enumerate(parts[:-1]) if _is_region_part(part)
        ]
        if region_indexes:
            tail = parts[region_indexes[-1] + 1 :]
            marked_city = [part for part in tail if part.endswith("市")]
            if len(marked_city) == 1:
                return marked_city[0]
            if marked_city:
                return marked_city[0]
            if len(tail) == 1 or len(set(tail)) == 1 or tail[-1].endswith(
                _WORKPLACE_SUFFIXES
            ):
                return tail[0]
        marked_city = [part for part in parts if part.endswith("市")]
        if marked_city:
            return marked_city[0]
    match = _REGION_PREFIX_RE.match(value)
    if match is not None:
        return match.group("city")
    return value


def _normalize_text(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).strip().casefold().split())


def _normalize_single_city_name(value: str) -> str | None:
    candidate = _strip_workplace_suffixes(_city_part(value))
    district_match = _CITY_WITH_DISTRICT_RE.match(candidate)
    if district_match is not None:
        candidate = district_match.group("city")
    if candidate.endswith("特别行政区") and len(candidate) > len("特别行政区"):
        candidate = candidate[: -len("特别行政区")]
    if (
        len(candidate) > 1
        and candidate.endswith("市")
        and candidate not in _CITY_SUFFIX_EXCEPTIONS
    ):
        candidate = candidate[:-1]
    candidate = candidate.rstrip(" ·•・‧∙/／|｜,，、;；:-－—")
    if not candidate or candidate in _NON_CITY_REGIONS or _is_region_part(candidate):
        return None
    return CHINA_CITY_ALIASES.get(candidate, candidate)


def normalize_city_names(name: str) -> list[str]:
    """Return concrete city names represented by a source location label.

    Source labels remain unchanged in the fact tables. This function only
    derives the city dimensions used for matching and display. A slash or
    comma-separated label can represent multiple cities; a middle dot denotes
    an administrative hierarchy such as ``泉州市·晋江市`` and resolves to the
    city-level segment. Province-only and broad-region labels return no cities.
    """

    normalized = _normalize_text(name)
    # Placeholder-only labels such as "/" are source facts, but they do not
    # identify a city and must not become a displayed city dimension.
    if normalized and not any(char.isalnum() for char in normalized):
        return []
    if not any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return [normalized] if normalized else []

    names: list[str] = []
    for part in _LOCATION_LIST_RE.split(normalized):
        candidate = _normalize_single_city_name(part.strip())
        if candidate and candidate not in names:
            names.append(candidate)
    return names


def normalize_city_name(name: str) -> str:
    """Normalize an unambiguous source location label to one city name.

    For compatibility with existing callers, a multi-city label returns its
    first city. Code that needs complete coverage must use
    :func:`normalize_city_names` instead.
    """

    normalized = _normalize_text(name)
    names = normalize_city_names(normalized)
    return names[0] if names else normalized


def is_city_level_name(name: str) -> bool:
    """Return whether a label contains a concrete city for auto-mapping."""

    return bool(normalize_city_names(name))


def canonical_city_key(city_name: str) -> str:
    digest = hashlib.sha256(city_name.encode("utf-8")).hexdigest()
    return f"city-name-{digest[:24]}"


def canonical_location_key(location: LocationRecord) -> str:
    # Some sources expose only a city label while others also expose country
    # and state. Including optional metadata in identity would split the same
    # city across companies. Ambiguous same-name cities can be overridden by a
    # published manual mapping without modifying source facts.
    names = normalize_city_names(location.name)
    if len(names) != 1:
        raise ValueError("Location does not resolve to exactly one city")
    return canonical_city_key(names[0])
