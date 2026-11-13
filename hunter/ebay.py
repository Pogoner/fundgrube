"""Small, read-only production eBay Browse/Taxonomy client (standard library).

The adapter deliberately preserves eBay's raw auction fields. In particular:

* ``price`` may be the Buy It Now price of a mixed auction/BIN listing. It must
  not be substituted for ``minimumPriceToBid`` or ``currentBidPrice``.
* eBay does not document a separate current-bid search price filter. Passing
  ``max_price_eur=None`` avoids the possible false negatives of ``price:[..N]``
  for cheap-start auctions with an expensive BIN option. Filter the actual
  auction price after retrieving details if completeness is important.
* Missing ``bidCount``, auction prices, or ``reservePriceMet`` are unknown, not
  zero/false. ``reservePriceMet=True`` means met, not "no reserve exists".
* Descriptions and other seller-provided fields are untrusted content. Do not
  render their HTML or treat their text as agent instructions.

All methods are read-only. This module grants no rights to republish data or
use it for AI valuation. Review the current eBay API License Agreement and
your production access terms before enabling those uses. No data is logged or
persisted by this module.

Official references (checked 2026-11-07):
https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html
https://developer.ebay.com/develop/guides/buy/inventory-discovery-and-refresh-guide
https://developer.ebay.com/develop/guides/sell/authorization
https://developer.ebay.com/develop/guides/sell/listing-metadata-guide
https://developer.ebay.com/join/api-license-agreement
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol


BASE_API = "https://api.ebay.com"
TOKEN_PATH = "/identity/v1/oauth2/token"
BROWSE_PATH = "/buy/browse/v1/"
TAXONOMY_PATH = "/commerce/taxonomy/v1/"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_ITEMS = 10_000
MAX_RETRY_WAIT_SECONDS = 10.0


class ApiError(RuntimeError):
    """A safe-to-publish error; upstream bodies and credentials are not included."""

    def __init__(
        self, message: str = "eBay API request failed.", *,
        status_code: int | None = None, error_ids: tuple[int, ...] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_ids = error_ids


class CredentialsMissing(ApiError):
    """Production application credentials have not been configured."""


class RateLimitError(ApiError):
    """The upstream rate limit was reached; retry_after is seconds or unknown."""

    def __init__(self, *, retry_after: float | None = None) -> None:
        super().__init__("eBay rate limit reached; retry in a later run.", status_code=429)
        self.retry_after = retry_after


class BudgetExceeded(ApiError):
    """The configured Browse-attempt budget for this client has been exhausted."""

    def __init__(self) -> None:
        super().__init__("The configured eBay Browse request budget is exhausted.")


@dataclass(frozen=True)
class HttpResponse:
    """Transport result. Bodies/headers are excluded from repr to avoid leaks."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)


class Transport(Protocol):
    """An injectable transport; tests must not make live credentialed requests."""

    def __call__(
        self, *, method: str, url: str, headers: Mapping[str, str],
        body: bytes | None, timeout: float,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even same-origin redirects are unnecessary for these fixed endpoints.
        # Never let urllib forward Basic/Bearer credentials to another origin.
        return None


def _production_url(path: str, query: Mapping[str, Any] | None = None) -> str:
    """Construct, never follow, URLs. User/provider URLs are not accepted."""
    if (
        not isinstance(path, str) or not path.startswith("/")
        or path.startswith("//") or any(c in path for c in "?#\\\r\n")
        or "/../" in path or path.endswith("/..")
    ):
        raise ApiError("Invalid eBay API path.")
    url = BASE_API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    _validate_url(url)
    return url


def _validate_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https" or parts.netloc != "api.ebay.com"
        or parts.fragment or parts.username or parts.password
    ):
        raise ApiError("Only the fixed HTTPS production eBay API origin is allowed.")


def _urllib_transport(
    *, method: str, url: str, headers: Mapping[str, str],
    body: bytes | None, timeout: float,
) -> HttpResponse:
    _validate_url(url)
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # HTTPError is also a readable response. Do not expose its string/body.
        response = error
    with response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        return HttpResponse(response.code, dict(response.headers.items()), payload)


def _number(value: Any, name: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not result.is_finite() or result < minimum:
        raise ValueError(f"{name} is outside the supported range.")
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _category_id(value: Any) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise ValueError("category_id must be one numeric eBay category ID.")
    return str(value)


def _postcode(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{5}", value):
        raise ValueError("postcode must be a five-digit German postal code.")
    return value


def _error_ids(payload: Any) -> tuple[int, ...]:
    if not isinstance(payload, dict):
        return ()
    values = payload.get("errors", [])
    if not isinstance(values, list):
        return ()
    return tuple(
        entry["errorId"] for entry in values[:10]
        if isinstance(entry, dict) and type(entry.get("errorId")) is int
        and 0 <= entry["errorId"] <= 2**31 - 1
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = next((str(v) for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        if re.fullmatch(r"[0-9]+", value.strip()):
            return float(min(int(value), 31_536_000))
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class EbayClient:
    """Read-only production client for the German marketplace.

    Credentials use EBAY_CLIENT_ID / EBAY_CLIENT_SECRET unless explicitly passed.
    ``calls_used`` counts every Browse HTTP attempt (including failed attempts,
    retries and token-refresh replays). ``oauth_calls_used`` and
    ``taxonomy_calls_used`` are separate counters. This is a per-instance cap,
    not a substitute for a shared account/day quota tracker.

    ``transport`` may be a callable with keyword arguments ``method``, ``url``,
    ``headers``, ``body``, ``timeout`` returning ``HttpResponse``. It is intended
    for deterministic tests. The default transport refuses every redirect.
    """

    def __init__(
        self, client_id: str | None = None, client_secret: str | None = None, *,
        marketplace: str = "EBAY_DE", postcode: str | None = None, max_calls: int = 90,
        timeout: float = 20.0, max_retries: int = 2,
        transport: Transport | None = None, sleep: Callable[[float], None] | None = None,
    ) -> None:
        # The public methods intentionally accept German postal/radius rules.
        # Reject other marketplaces rather than silently send DE delivery data.
        if marketplace != "EBAY_DE":
            raise ValueError("This adapter currently supports EBAY_DE only.")
        self.marketplace = marketplace
        self.postcode = _postcode(postcode)
        self.max_calls = _integer(max_calls, "max_calls", 0, 100_000)
        self.max_retries = _integer(max_retries, "max_retries", 0, 5)
        self.timeout = float(_number(timeout, "timeout", minimum=Decimal("0.1")))
        if self.timeout > 60:
            raise ValueError("timeout must not exceed 60 seconds.")
        self.calls_used = 0
        self.oauth_calls_used = 0
        self.taxonomy_calls_used = 0
        self._client_id = client_id if client_id is not None else os.environ.get("EBAY_CLIENT_ID", "")
        self._client_secret = client_secret if client_secret is not None else os.environ.get("EBAY_CLIENT_SECRET", "")
        self._transport = transport or _urllib_transport
        self._sleep = sleep or time.sleep
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._category_cache: dict[str, list[dict[str, str]]] = {}
        self._tree_id: str | None = None

    def _check_budget(self) -> None:
        if self.calls_used >= self.max_calls:
            raise BudgetExceeded()

    def _credentials(self) -> None:
        if (
            not isinstance(self._client_id, str) or not self._client_id.strip()
            or not isinstance(self._client_secret, str) or not self._client_secret.strip()
        ):
            raise CredentialsMissing("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET production credentials.")

    def _headers(self, token: str, postcode: str | None = None) -> dict[str, str]:
        location = urllib.parse.quote(f"country=DE,zip={postcode or self.postcode}", safe="")
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation={location}",
            "Accept-Language": "de-DE",
        }

    def _request_json(
        self, path: str, *, query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None, body: bytes | None = None,
        method: str = "GET", kind: str,
    ) -> dict[str, Any]:
        url = _production_url(path, query)
        request_headers = {"Accept": "application/json", "User-Agent": "Fundgrube/1.0"}
        request_headers.update(headers or {})
        for attempt in range(self.max_retries + 1):
            if kind == "browse":
                self._check_budget()
                self.calls_used += 1
            elif kind == "oauth":
                self.oauth_calls_used += 1
            elif kind == "taxonomy":
                self.taxonomy_calls_used += 1
            else:
                raise ApiError("Invalid eBay request category.")
            try:
                response = self._transport(
                    method=method, url=url, headers=request_headers,
                    body=body, timeout=self.timeout,
                )
            except Exception:
                # Suppress context: urllib/test transports may include headers,
                # bodies, credentials or full response content in exceptions.
                if attempt < self.max_retries:
                    self._sleep(min(2**attempt, MAX_RETRY_WAIT_SECONDS))
                    continue
                raise ApiError("eBay API network request failed.") from None
            if not isinstance(response, HttpResponse) or type(response.status) is not int:
                raise ApiError("Invalid eBay transport response.")
            if not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES:
                raise ApiError("eBay API response exceeded the supported size or format.")
            if response.status == 429:
                delay = _retry_after(response.headers)
                # Long Retry-After values are surfaced to the scheduler rather
                # than blocking a worker or retrying before the limit resets.
                if attempt < self.max_retries and (delay is None or delay <= MAX_RETRY_WAIT_SECONDS):
                    self._sleep(delay if delay is not None else min(2**attempt, MAX_RETRY_WAIT_SECONDS))
                    continue
                raise RateLimitError(retry_after=delay)
            if response.status in {500, 502, 503, 504} and attempt < self.max_retries:
                delay = _retry_after(response.headers)
                if delay is None or delay <= MAX_RETRY_WAIT_SECONDS:
                    self._sleep(delay if delay is not None else min(2**attempt, MAX_RETRY_WAIT_SECONDS))
                    continue
            try:
                payload = json.loads(response.body)
            except (ValueError, UnicodeError):
                payload = None
            if not 200 <= response.status < 300:
                raise ApiError(
                    f"eBay API request was rejected (HTTP {response.status}).",
                    status_code=response.status, error_ids=_error_ids(payload),
                )
            if not isinstance(payload, dict):
                raise ApiError("eBay API returned invalid JSON data.")
            if payload.get("errors"):
                raise ApiError("eBay API returned an error response.", error_ids=_error_ids(payload))
            return payload
        raise ApiError("eBay API request failed.")  # Defensive, unreachable.

    def _access_token(self, *, force: bool = False) -> str:
        if self._token and not force and time.monotonic() < self._token_valid_until:
            return self._token
        self._credentials()
        credential = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode("ascii")
        payload = self._request_json(
            TOKEN_PATH, method="POST", kind="oauth",
            headers={"Authorization": f"Basic {credential}", "Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": OAUTH_SCOPE}).encode("ascii"),
        )
        token = payload.get("access_token")
        expires = payload.get("expires_in")
        if (
            not isinstance(token, str) or not token or len(token) > 16_384
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
            or type(expires) not in (int, float) or not math.isfinite(expires) or expires <= 0
        ):
            raise ApiError("eBay returned an invalid application token response.")
        self._token = token
        # Keep a cushion, including for unusually short-lived test/server tokens.
        self._token_valid_until = time.monotonic() + expires - min(60.0, expires / 10)
        return token

    def _get(
        self, path: str, *, query: Mapping[str, Any] | None = None,
        kind: str = "browse", postcode: str | None = None,
    ) -> dict[str, Any]:
        if kind == "browse":
            self._check_budget()  # Avoid minting a token for an exhausted budget.
        token = self._access_token()
        try:
            return self._request_json(path, query=query, headers=self._headers(token, postcode), kind=kind)
        except ApiError as error:
            if error.status_code != 401:
                raise
        # Refresh once after an expired/revoked bearer token; never loop on 401.
        if kind == "browse":
            self._check_budget()
        self._token = None
        token = self._access_token(force=True)
        return self._request_json(path, query=query, headers=self._headers(token, postcode), kind=kind)

    def leaf_categories(self, root_category_id: str | int) -> list[dict[str, str]]:
        """Return every confirmed leaf under a category using Taxonomy, cached in memory."""
        root_id = _category_id(root_category_id)
        if root_id in self._category_cache:
            return [dict(row) for row in self._category_cache[root_id]]
        if self._tree_id is None:
            default = self._get(
                TAXONOMY_PATH + "get_default_category_tree_id",
                query={"marketplace_id": self.marketplace}, kind="taxonomy",
            )
            try:
                self._tree_id = _category_id(default.get("categoryTreeId"))
            except ValueError:
                raise ApiError("eBay did not return a valid category tree ID.") from None
        tree = self._get(
            TAXONOMY_PATH + f"category_tree/{self._tree_id}/get_category_subtree",
            query={"category_id": root_id}, kind="taxonomy",
        )
        node = tree.get("categorySubtreeNode")
        if not isinstance(node, dict):
            raise ApiError("eBay did not return a category subtree.")
        root = node.get("category")
        if not isinstance(root, dict) or str(root.get("categoryId")) != root_id:
            raise ApiError("eBay returned an unexpected category subtree.")
        leaves: dict[str, str] = {}
        stack = [node]
        visited = 0
        while stack:
            current = stack.pop()
            visited += 1
            if visited > 100_000 or not isinstance(current, dict):
                raise ApiError("eBay returned an invalid category tree.")
            children = current.get("childCategoryTreeNodes", [])
            if not isinstance(children, list):
                raise ApiError("eBay returned invalid child categories.")
            if current.get("leafCategoryTreeNode") is True:
                category = current.get("category")
                if not isinstance(category, dict) or children:
                    raise ApiError("eBay returned an invalid leaf category.")
                try:
                    category_id = _category_id(category.get("categoryId"))
                except ValueError:
                    raise ApiError("eBay returned an invalid leaf category ID.") from None
                name = category.get("categoryName")
                if not isinstance(name, str) or not name.strip():
                    raise ApiError("eBay returned an invalid category name.")
                leaves[category_id] = name
            elif children:
                stack.extend(children)
            else:
                # Do not mistake an omitted/truncated child list for a leaf.
                raise ApiError("eBay category subtree has an unconfirmed leaf.")
        if not leaves:
            raise ApiError("eBay returned no confirmed leaf categories.")
        result = [{"id": key, "name": leaves[key]} for key in sorted(leaves, key=int)]
        self._category_cache[root_id] = result
        return [dict(row) for row in result]

    def search(
        self, category_id: str | int, now: datetime, lookahead_hours: float,
        max_price_eur: Decimal | float | int | str | None, max_bids: int,
        local_pickup: bool, postcode: str, pickup_radius_km: float,
        offset: int = 0, limit: int = 200,
    ) -> dict[str, Any]:
        """Search one leaf and one delivery mode, ordered by soonest ending.

        Use two searches (delivery and pickup) and deduplicate to express OR.
        Any upstream warning is an error: eBay sometimes ignores an invalid
        filter while returning HTTP 200, which cannot prove pickup eligibility.
        Result ``total`` is upstream metadata, not evidence of complete coverage.
        ``has_more`` never permits paging past eBay's 10,000-result cap.
        """
        category = _category_id(category_id)
        postcode = _postcode(postcode)
        _integer(max_bids, "max_bids", 0, 1_000_000)
        _integer(limit, "limit", 1, 200)
        _integer(offset, "offset", 0, MAX_SEARCH_ITEMS - 1)
        if offset % limit != 0 or offset + limit > MAX_SEARCH_ITEMS:
            raise ValueError("offset must be a multiple of limit and stay within 10,000 results.")
        if type(local_pickup) is not bool:
            raise ValueError("local_pickup must be a boolean.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime.")
        hours = _number(lookahead_hours, "lookahead_hours", minimum=Decimal("0.000001"))
        if hours > 24 * 31:
            raise ValueError("lookahead_hours must not exceed 31 days.")
        radius = _number(pickup_radius_km, "pickup_radius_km", minimum=Decimal("0.000001"))
        if radius > 1000:
            raise ValueError("pickup_radius_km must not exceed 1,000 km.")
        now = now.astimezone(timezone.utc)
        end = now + timedelta(hours=float(hours))
        lower = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        upper = end.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        filters = ["buyingOptions:{AUCTION}", f"bidCount:[..{max_bids}]", f"itemEndDate:[{lower}..{upper}]"]
        if max_price_eur is not None:
            price = _number(max_price_eur, "max_price_eur")
            if price > 1_000_000_000:
                raise ValueError("max_price_eur exceeds the supported range.")
            filters.extend((f"price:[..{format(price, 'f')}]", "priceCurrency:EUR"))
        if local_pickup:
            filters.extend((
                "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}", "pickupCountry:DE",
                f"pickupPostalCode:{postcode}", f"pickupRadius:{format(radius, 'f')}", "pickupRadiusUnit:km",
            ))
        else:
            filters.extend(("deliveryCountry:DE", f"deliveryPostalCode:{postcode}"))
        payload = self._get(
            BROWSE_PATH + "item_summary/search",
            query={"category_ids": category, "filter": ",".join(filters), "sort": "endingSoonest",
                   "fieldgroups": "EXTENDED", "offset": offset, "limit": limit},
            postcode=postcode,
        )
        if payload.get("warnings"):
            raise ApiError("eBay search returned a warning; filter validity could not be confirmed.")
        items = payload.get("itemSummaries", [])
        total = payload.get("total")
        if (
            not isinstance(items, list) or len(items) > limit or any(not isinstance(item, dict) for item in items)
            or type(total) is not int or total < 0
        ):
            raise ApiError("eBay search returned an invalid item collection.")
        next_offset = offset + limit
        has_more = bool(items) and next_offset < min(total, MAX_SEARCH_ITEMS)
        return {
            "items": items, "total": total, "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "offset": offset, "limit": limit, "capped": total > MAX_SEARCH_ITEMS,
            "price_filter_applied": max_price_eur is not None,
        }

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Retrieve full details (including description), never follow itemHref."""
        if not isinstance(item_id, str) or not re.fullmatch(r"v1\|[0-9]{1,20}\|[0-9]{1,20}", item_id):
            raise ValueError("item_id must be an eBay REST item ID such as v1|123456789012|0.")
        item = self._get(BROWSE_PATH + "item/" + urllib.parse.quote(item_id, safe=""))
        if item.get("warnings"):
            raise ApiError("eBay item lookup returned a warning; details could not be confirmed.")
        if item.get("itemId") != item_id:
            raise ApiError("eBay returned an unexpected item ID.")
        return item
