import hashlib
import unicodedata

from job_market.schemas import LocationRecord


def normalize_city_name(name: str) -> str:
    """Normalize a source city label without inventing geographic facts."""

    normalized = " ".join(
        unicodedata.normalize("NFKC", name).strip().casefold().split()
    )
    # Chinese portals alternate between "北京" and "北京市". The suffix is a
    # display convention, not a distinct city. Restrict this rule to labels
    # containing CJK characters so foreign names ending in the Latin letter
    # sequence "shi" are never altered.
    if (
        len(normalized) > 1
        and normalized.endswith("市")
        and any("\u4e00" <= char <= "\u9fff" for char in normalized)
    ):
        normalized = normalized[:-1].rstrip()
    return normalized


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
