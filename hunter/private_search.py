"""Private search origin loaded only from the runtime secret.

The HMAC binds persistent pickup evidence to an origin without publishing an
enumerable postcode hash. It is not evidence of an actual seller location.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


SECRET_NAME = "HUNTER_PRIVATE_SEARCH"
PROFILE_PATTERN = re.compile(r"hmac-sha256:v1:[0-9a-f]{64}\Z")
_POSTCODE_PATTERN = re.compile(r"[0-9]{5}\Z")


class PrivateSearchError(ValueError):
    """A missing/malformed private runtime setting; messages never echo it."""


def valid_postcode(value: Any) -> bool:
    return isinstance(value, str) and _POSTCODE_PATTERN.fullmatch(value) is not None


def valid_profile(value: Any) -> bool:
    return isinstance(value, str) and PROFILE_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True)
class PrivateSearch:
    postcode: str = field(repr=False)
    _key: bytes = field(repr=False)

    def profile_for(self, postcode: str | None = None) -> str:
        origin = self.postcode if postcode is None else postcode
        if not valid_postcode(origin):
            raise PrivateSearchError("Private search postcode has an invalid format.")
        message = b"fundgrube:pickup-search:v1\0DE\0" + origin.encode("ascii")
        return "hmac-sha256:v1:" + hmac.new(self._key, message, hashlib.sha256).hexdigest()

    @property
    def profile(self) -> str:
        return self.profile_for()


def load_private_search(environ: Mapping[str, str], *, required: bool = False) -> PrivateSearch | None:
    """Read one bounded secret; no static config/default can satisfy it."""
    raw = environ.get(SECRET_NAME, "")
    if not raw:
        if required:
            raise PrivateSearchError("HUNTER_PRIVATE_SEARCH is required before a live pickup search.")
        return None
    if not isinstance(raw, str) or len(raw) > 4096:
        raise PrivateSearchError("HUNTER_PRIVATE_SEARCH must be a bounded JSON object.")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise PrivateSearchError("HUNTER_PRIVATE_SEARCH must be valid JSON with unique fields.") from None
    if (not isinstance(value, dict) or set(value) != {"postcode", "profile_key"}
            or not valid_postcode(value.get("postcode"))
            or not isinstance(value.get("profile_key"), str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", value["profile_key"]) is None):
        raise PrivateSearchError("HUNTER_PRIVATE_SEARCH requires a five-digit postcode and a 64-hex-character profile_key.")
    return PrivateSearch(value["postcode"], bytes.fromhex(value["profile_key"]))


def bind_pickup_context(item: Mapping[str, Any], private: PrivateSearch | None, *, cached: bool = False) -> dict:
    """Migrate only internal query provenance; preserve actual seller fields.

    Legacy query origins are converted with the same secret key, including
    mismatching old origins. Missing private context never validates old pickup.
    """
    result = dict(item)
    legacy = result.pop("pickup_search_postcode", None)
    result.pop("pickup_search_city", None)
    profile = result.get("pickup_search_profile")
    if "pickup_search_profile" not in result and private is not None and valid_postcode(legacy):
        profile = private.profile_for(legacy)
    if valid_profile(profile):
        result["pickup_search_profile"] = profile
    else:
        result.pop("pickup_search_profile", None)
        if result.get("pickup_search_confirmed") is True:
            result["pickup_search_confirmed"] = False
            result.pop("pickup_search_radius_km", None)
            result.pop("pickup_search_observed_at", None)
    # Direct-source radius claims from a different/unknown origin cannot be
    # reused after a profile change. A new live query can establish them again.
    if cached and result.get("local_pickup_confirmed") is True:
        if private is None or profile != private.profile:
            result["local_pickup_confirmed"] = False
            result.pop("pickup_verified_at", None)
            result["distance_km"] = None
    return result


def matching_origin(item: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    """Prefer HMAC context; legacy comparison is only for old input formats."""
    expected = profile.get("pickup_search_profile")
    actual = item.get("pickup_search_profile")
    if expected is not None or actual is not None:
        return valid_profile(expected) and valid_profile(actual) and hmac.compare_digest(actual, expected)
    postcode = profile.get("postcode")
    return valid_postcode(postcode) and item.get("pickup_search_postcode") == postcode
