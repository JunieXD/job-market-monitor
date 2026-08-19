import hashlib
import unicodedata

from job_market.schemas import LocationRecord


def normalize_city_name(name: str) -> str:
    """Normalize a source city label without inventing geographic facts."""

    return " ".join(unicodedata.normalize("NFKC", name).strip().casefold().split())


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
