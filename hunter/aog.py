"""Polite, read-only adapter for the verified public Auktionen-ohne-Gebot API.

This is an aggregator, not a direct eBay verification. No outgoing item/image
link is requested. Each scan checks robots.txt, refreshes general page zero,
rotates bounded general tail pages, and optionally reads pickup page zero.
The public search UI uses pages of 48 and the query parameters below.

Publication is planned every ten minutes. The nine-minute safety debounce
allows modest scheduler jitter; it is not the requested schedule. Existing
cooldowns, HTTP Retry-After and access-denial pauses are never shortened.

The application must preserve source_observed_at: response data may already
have been cached. Unknown shipping, bids, reserve and actual location remain
unknown. A nearby pickup search is a provisional source claim, not a verified
seller location or measured distance.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

from .models import iso_time, parse_time


ORIGIN = "https://auktionen-ohne-gebot.de"
SOURCE_URL = ORIGIN + "/business-industrie/"
ROBOTS_URL = ORIGIN + "/robots.txt"
API_PATH = "/api/auktionen"
PROVIDER = "auktionen-ohne-gebot"
PRODUCT_TOKEN = "FundgrubeBot"
USER_AGENT = PRODUCT_TOKEN + "/1.0 (+https://github.com/Pogoner/fundgrube)"
PAGE_SIZE = 48
MAX_API_REQUESTS = 6
DEFAULT_API_REQUESTS = 4
MAX_SOURCE_ITEMS = 9600
MIN_REQUEST_GAP = 10.0
PLANNED_SCAN_INTERVAL = 600
MIN_SCAN_INTERVAL = 540
DEFAULT_RATE_LIMIT_BACKOFF = 1800
SOURCE_CACHE_SECONDS = 900
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_DAILY_REQUESTS = 750


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)


class SourceError(RuntimeError):
    """Safe, fixed diagnostics; never include a response body or raw exception."""

    def __init__(self, code: str, message: str, *, cooldown: float | None = None):
        super().__init__(message)
        self.code, self.message, self.cooldown = code, message, cooldown


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "auktionen-ohne-gebot.de"
            or parts.path not in {"/robots.txt", API_PATH} or parts.fragment
            or parts.username or parts.password):
        raise SourceError("unsafe_request", "Die Quelle erlaubt nur die festgelegten HTTPS-Endpunkte.")


def _transport(*, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    _check_url(url)
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        handle = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        handle = error
    with handle:
        maximum = MAX_ROBOTS_BYTES if url == ROBOTS_URL else MAX_JSON_BYTES
        return HttpResponse(handle.code, dict(handle.headers.items()), handle.read(maximum + 1))


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _http_time(value: Any) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None


def _retry_after(headers: Mapping[str, str], at: datetime) -> float:
    value = _headers(headers).get("retry-after", "").strip()
    if re.fullmatch(r"[0-9]+", value):
        # An interval beyond datetime's range becomes an effectively permanent
        # pause, never a truncated shorter wait. Avoid int() on unbounded input.
        return float(int(value) if len(value) <= 12 else 10**12)
    date = _http_time(value)
    return max((date - at).total_seconds(), 0) if date else DEFAULT_RATE_LIMIT_BACKOFF


def _normalized_robots_path(value: str) -> str:
    # RFC 9309 matching: decode percent-encoded unreserved ASCII, retain
    # reserved encodings and encode non-ASCII UTF-8 octets consistently.
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    def replace(match):
        number = int(match.group(1), 16)
        char = chr(number)
        return char if char in unreserved else "%" + match.group(1).upper()
    value = re.sub(r"%([0-9A-Fa-f]{2})", replace, value)
    return "".join(char if ord(char) < 128 else urllib.parse.quote(char, safe="") for char in value)


class RobotsPolicy:
    """Wildcard/$ path matching, longest matching rule, Allow wins equal ties.

    Matching named user-agent groups are selected by the longest matching
    product token, then merged. '*' is used only without a named match.
    Nonstandard Crawl-delay and Request-rate are honored conservatively.
    """

    def __init__(self, body: str, product: str = PRODUCT_TOKEN):
        groups, agents, rules, delays = [], [], [], []
        has_directive = False
        recognized = False
        for raw_line in body.lstrip("\ufeff").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                recognized = True
                if has_directive:
                    groups.append((agents, rules, delays))
                    agents, rules, delays, has_directive = [], [], [], False
                if value and re.fullmatch(r"[A-Za-z0-9_*.-]+", value):
                    agents.append(value.lower())
            elif name in {"allow", "disallow", "crawl-delay", "request-rate"} and agents:
                recognized, has_directive = True, True
                if name in {"allow", "disallow"}:
                    if value:
                        rules.append((value, name == "allow"))
                elif name == "crawl-delay":
                    try:
                        delay = float(value)
                    except ValueError:
                        delay = None
                    if delay is not None and math.isfinite(delay) and delay >= 0:
                        delays.append(delay)
                    else:
                        raise SourceError("robots_invalid", "robots.txt enthält eine ungültige Abrufverzögerung.")
                else:
                    match = re.fullmatch(r"([1-9][0-9]{0,8})\s*/\s*([1-9][0-9]{0,8})", value)
                    if match:
                        delays.append(int(match.group(2)) / int(match.group(1)))
                    else:
                        raise SourceError("robots_invalid", "robots.txt enthält eine nicht unterstützte Abrufrate.")
            elif name == "sitemap":
                recognized = True
        if agents:
            groups.append((agents, rules, delays))
        if body.strip() and not recognized and any(line.split("#", 1)[0].strip() for line in body.splitlines()):
            raise SourceError("robots_invalid", "Die robots.txt-Antwort ist keine erkennbare Richtliniendatei.")
        product = product.lower()
        strengths = [max((len(agent) for agent in names if agent != "*" and agent in product), default=0) for names, _, _ in groups]
        strongest = max(strengths, default=0)
        chosen = [group for index, group in enumerate(groups)
                  if (strengths[index] == strongest if strongest else "*" in group[0])]
        self.rules = []
        self.delay = max([MIN_REQUEST_GAP] + [delay for _, _, group_delays in chosen for delay in group_delays])
        for _, group_rules, _ in chosen:
            for pattern, allow in group_rules:
                normalized = _normalized_robots_path(pattern)
                anchored = normalized.endswith("$")
                core = normalized[:-1] if anchored else normalized
                expression = "^" + ".*".join(re.escape(part) for part in core.split("*")) + ("$" if anchored else "")
                # Percent-escaped octets count as one octet for specificity.
                literal = core.replace("*", "")
                specificity = len(re.sub(r"%[0-9A-F]{2}", "x", literal))
                self.rules.append((re.compile(expression), specificity, allow))

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        target = _normalized_robots_path(parts.path + ("?" + parts.query if parts.query else ""))
        matches = [(specificity, allow) for pattern, specificity, allow in self.rules if pattern.search(target)]
        return max(matches, default=(0, True))[1]


def _numeric(value: Any, *, integer: bool = False) -> float | int | None:
    if value is None:
        return None
    if type(value) not in (int, float) or value < 0 or value > 1_000_000_000 or not math.isfinite(value):
        raise SourceError("invalid_payload", "Die Quelle hat unerwartete numerische Angebotsdaten geliefert.")
    if integer:
        if int(value) != value:
            raise SourceError("invalid_payload", "Die Quelle hat eine ungültige Gebotszahl geliefert.")
        return int(value)
    return round(float(value), 2)


def _item_id(url: Any) -> str:
    if not isinstance(url, str) or len(url) > 2500 or any(ord(char) < 32 for char in url):
        raise SourceError("invalid_payload", "Ein Angebotslink der Quelle ist ungültig.")
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in {"ebay.de", "www.ebay.de", "ebay.com", "www.ebay.com"}
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise ValueError()
        match = re.fullmatch(r"/itm/(?:[^/]+/)?([0-9]{9,15})/?", parts.path)
        if not match:
            raise ValueError()
        return "v1|" + match.group(1) + "|0"
    except ValueError:
        raise SourceError("invalid_payload", "Ein Angebotslink der Quelle ist kein eindeutiger eBay-Artikel.") from None


def _image_url(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 2500:
        raise SourceError("invalid_payload", "Die Quelle hat ein ungültiges Bildfeld geliefert.")
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme == "https" and (host == "ebayimg.com" or host.endswith(".ebayimg.com"))
                and not parts.username and not parts.password and parts.port in (None, 443)
                and not any(ord(char) < 32 for char in value)):
            return value
    except ValueError:
        pass
    # Never fetch images. An invalid optional image is safely omitted.
    return ""


def _cache_observation(headers: Mapping[str, str], at: datetime) -> tuple[datetime, int]:
    headers = _headers(headers)
    lifetime = SOURCE_CACHE_SECONDS
    for match in re.finditer(r"(?:^|,)\s*(?:s-maxage|max-age)\s*=\s*\"?([0-9]{1,10})", headers.get("cache-control", ""), re.I):
        lifetime = max(lifetime, min(int(match.group(1)), 31 * 86400))
    age_text = headers.get("age", "").strip()
    age = min(int(age_text), 31 * 86400) if re.fullmatch(r"[0-9]{1,10}", age_text) else 0
    date = _http_time(headers.get("date"))
    if date:
        age = max(age, min(max(0, math.ceil((at - date).total_seconds())), 31 * 86400))
    seconds = lifetime + age
    return at - timedelta(seconds=seconds), seconds


def _parse_page(response: HttpResponse, at: datetime, *, page_size: int, offset: int,
                pickup: bool, postcode: str, radius: float) -> tuple[list[dict], int, datetime]:
    headers = _headers(response.headers)
    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise SourceError("unexpected_content", "Die Quelle liefert kein erwartetes JSON; keine Schutzseite wird umgangen.")
    try:
        text = response.body.decode("utf-8")
        payload = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError):
        raise SourceError("invalid_json", "Die JSON-Antwort der Quelle konnte nicht bestätigt werden.") from None
    if (not isinstance(payload, dict) or type(payload.get("gesamt")) is not int or not 0 <= payload["gesamt"] <= 1_000_000_000
            or not isinstance(payload.get("artikel"), list) or len(payload["artikel"]) > page_size):
        raise SourceError("invalid_payload", "Die Quelle hat ein unerwartetes Antwortformat geliefert.")
    if (any(payload.get(key) for key in ("fehler", "error", "errors", "warning", "warnings", "warnungen"))
            or any(payload.get(key) is False for key in ("success", "erfolg", "ok"))):
        raise SourceError("source_payload_warning", "Die Quelle meldet einen Fehler oder eine Warnung; Filterergebnisse werden nicht als bestätigt übernommen.")
    total, articles = payload["gesamt"], payload["artikel"]
    if total < len(articles) or (offset == 0 and total > 0 and not articles):
        raise SourceError("invalid_payload", "Leere Artikeldaten widersprechen der gemeldeten Trefferzahl.")
    observed, cache_age = _cache_observation(response.headers, at)
    items = []
    for article in articles:
        if not isinstance(article, dict) or not isinstance(article.get("titel"), str) or not article["titel"].strip() or len(article["titel"]) > 5000:
            raise SourceError("invalid_payload", "Die Quelle hat einen unvollständigen Artikeldatensatz geliefert.")
        item_id = _item_id(article.get("link"))
        ending_raw = article.get("ende")
        ending = parse_time(ending_raw)
        if ending_raw is not None and (not isinstance(ending_raw, str) or (ending_raw and ending is None)):
            raise SourceError("invalid_payload", "Die Quelle hat ein ungültiges Auktionsende geliefert.")
        available = article.get("abholung")
        if available is not None and type(available) is not bool:
            raise SourceError("invalid_payload", "Die Quelle hat eine ungültige Abholangabe geliefert.")
        condition = article.get("zustand")
        if condition is not None and not isinstance(condition, str):
            raise SourceError("invalid_payload", "Die Quelle hat eine ungültige Zustandsangabe geliefert.")
        confirmed = pickup and available is True
        items.append({
            "id": item_id, "title": article["titel"], "url": article["link"],
            "image_url": _image_url(article.get("bild")), "description": "",
            "price_eur": _numeric(article.get("preis")), "price_kind": "auction_price",
            "shipping_eur": _numeric(article.get("versand")), "bid_count": _numeric(article.get("gebote"), integer=True),
            "ends_at": iso_time(ending) if ending else None, "condition": condition or "",
            "is_auction": True if ending else None, "pickup_available": available,
            "local_pickup_confirmed": False, "pickup_search_confirmed": confirmed,
            "pickup_search_postcode": postcode if confirmed else None,
            "pickup_search_radius_km": radius if confirmed else None,
            "source_provider": PROVIDER, "source_url": SOURCE_URL, "verification_level": "aggregator",
            "source_observed_at": iso_time(observed), "last_seen_at": iso_time(observed),
            "source_cache_age_seconds": cache_age,
        })
    return items, total, observed


def _query(*, offset: int, page_size: int, maximum: float, pickup: bool, postcode: str, radius: float) -> str:
    query = {"q": "", "kat": "12576", "max": format(maximum, "g"), "anzahl": str(page_size),
             "ab": str(offset), "ref": "suche", "typ": "ohne", "sort": "ende", "endet": "24h"}
    if pickup:
        query.update(abholung="1", plz=postcode, km=format(radius, "g"), ort="DE")
    else:
        query["gratis"] = "0"
    return ORIGIN + API_PATH + "?" + urllib.parse.urlencode(query)


def _source_state(value: Any, at: datetime, page_size: int) -> dict:
    value = value if isinstance(value, dict) else {}
    state = {"schema_version": 1, "general_next_offset": page_size, "request_day": at.date().isoformat(), "requests_today": 0}
    for key in ("last_attempt_at", "last_success_at", "next_allowed_at", "robots_checked_at"):
        parsed = parse_time(value.get(key))
        if parsed and (key == "next_allowed_at" or parsed <= at):
            state[key] = iso_time(parsed)
    offset = value.get("general_next_offset")
    if type(offset) is int and page_size <= offset < MAX_SOURCE_ITEMS and offset % page_size == 0:
        state["general_next_offset"] = offset
    if value.get("request_day") == state["request_day"] and type(value.get("requests_today")) is int:
        state["requests_today"] = max(0, min(value["requests_today"], MAX_DAILY_REQUESTS))
    return state


def _merge(items: dict[str, dict], row: dict) -> None:
    old = items.get(row["id"])
    if not old:
        items[row["id"]] = row
        return
    # Prefer fresher facts, but label the combined observation with the oldest
    # source age. Pickup evidence cannot make all merged fields look younger.
    newer, older = (row, old) if row["source_observed_at"] >= old["source_observed_at"] else (old, row)
    merged = dict(newer)
    if old["pickup_search_confirmed"] or row["pickup_search_confirmed"]:
        evidence = row if row["pickup_search_confirmed"] else old
        for key in ("pickup_search_confirmed", "pickup_search_postcode", "pickup_search_radius_km", "pickup_available"):
            merged[key] = evidence[key]
    merged["source_observed_at"] = merged["last_seen_at"] = older["source_observed_at"]
    merged["source_cache_age_seconds"] = max(old["source_cache_age_seconds"], row["source_cache_age_seconds"])
    items[row["id"]] = merged


def collect(settings: Mapping[str, Any], at: datetime, source_state: dict | None, *,
            transport: Callable | None = None, sleep: Callable[[float], None] | None = None) -> dict:
    """Fetch a bounded aggregator snapshot. All network boundaries are injectable.

    transport receives keyword url/headers/timeout and returns HttpResponse.
    settings accepts aog_page_size (12 or 48), aog_max_requests_per_run (1..6, default 4),
    aog_pickup_enabled (boolean), max_price_eur, postcode, pickup_radius_km.
    The internal API cap excludes the robots request; coverage counts both.
    Planned cadence is ten minutes, with a nine-minute duplicate-run debounce.
    The 750-request daily cap includes robots (144 default runs use 720 requests).
    """
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    at = at.astimezone(timezone.utc).replace(microsecond=0)
    page_size = settings.get("aog_page_size", PAGE_SIZE)
    limit = settings.get("aog_max_requests_per_run", DEFAULT_API_REQUESTS)
    pickup_enabled = settings.get("aog_pickup_enabled", True)
    maximum, radius = settings.get("max_price_eur", 1), settings.get("pickup_radius_km", 50)
    postcode = settings.get("postcode", "69121")
    if type(page_size) is not int or page_size not in (12, 48) or type(limit) is not int or not 1 <= limit <= MAX_API_REQUESTS:
        raise ValueError("unsupported AOG page/request limit")
    if type(pickup_enabled) is not bool or not isinstance(postcode, str) or not re.fullmatch(r"[0-9]{5}", postcode):
        raise ValueError("invalid AOG pickup settings")
    if (any(type(value) not in (int, float) for value in (maximum, radius))
            or not 0 <= maximum <= 1000000 or not 0 < radius <= 1000
            or any(not math.isfinite(value) for value in (maximum, radius))):
        raise ValueError("invalid AOG price/radius")
    state = _source_state(source_state, at, page_size)
    send, pause = transport or _transport, sleep or time.sleep
    started, waited_seconds = time.monotonic(), 0.0
    warnings, items, observations = [], {}, []
    coverage = {"requests_this_run": 0, "api_requests_this_run": 0, "pages_fetched": 0, "page_size": page_size,
                "source_total_reported": None, "source_cache_max_age_minutes": 15, "complete": False,
                "next_offset": state["general_next_offset"], "source_claims_only": True,
                "pickup_search_count": 0, "pickup_total_reported": None, "pagination_is_stable": False,
                "planned_interval_minutes": PLANNED_SCAN_INTERVAL // 60,
                "debounce_seconds": MIN_SCAN_INTERVAL, "daily_request_budget": MAX_DAILY_REQUESTS}
    def warning(code: str, message: str):
        if not any(value["code"] == code for value in warnings):
            warnings.append({"code": code, "message": message})
    def result(status: str):
        coverage["next_offset"] = state["general_next_offset"]
        return {"items": sorted(items.values(), key=lambda row: (row["ends_at"] or "", row["id"])),
                "source_state": state, "coverage": coverage, "warnings": warnings, "status": status,
                "observed_at": iso_time(min(observations)) if observations else None}
    next_allowed = parse_time(state.get("next_allowed_at"))
    previous = parse_time(state.get("last_attempt_at"))
    if next_allowed and at < next_allowed or previous and at - previous < timedelta(seconds=MIN_SCAN_INTERVAL):
        warning("source_cooldown", "Die schonende Abrufpause ist noch nicht abgelaufen.")
        return result("cooldown")
    if state["requests_today"] >= MAX_DAILY_REQUESTS:
        state["next_allowed_at"] = iso_time((at + timedelta(days=1)).replace(hour=0, minute=0, second=0))
        warning("source_daily_budget", "Das vorsichtige Tageslimit für diese Quelle ist erreicht.")
        return result("cooldown")
    state["last_attempt_at"] = iso_time(at)
    state["next_allowed_at"] = iso_time(at + timedelta(seconds=MIN_SCAN_INTERVAL))
    gap = MIN_REQUEST_GAP
    def request(url: str, *, robots: bool = False) -> HttpResponse:
        nonlocal waited_seconds
        _check_url(url)
        if coverage["requests_this_run"]:
            if gap > 60:
                raise SourceError("robots_delay", "Die robots.txt-Verzögerung überschreitet den begrenzten Einzelabruf.", cooldown=gap)
            pause(gap)
            waited_seconds += gap
        if state["requests_today"] >= MAX_DAILY_REQUESTS:
            raise SourceError("source_daily_budget", "Das vorsichtige Tageslimit für diese Quelle ist erreicht.", cooldown=86400)
        if not robots and coverage["api_requests_this_run"] >= limit:
            raise SourceError("source_run_budget", "Das Seitenlimit dieses Laufs ist erreicht.")
        coverage["requests_this_run"] += 1
        state["requests_today"] += 1
        if not robots:
            coverage["api_requests_this_run"] += 1
        try:
            response = send(url=url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain" if robots else "application/json"}, timeout=20)
        except Exception:
            raise SourceError("source_network_error", "Die öffentliche Quelle konnte nicht erreicht werden.") from None
        if not isinstance(response, HttpResponse) or type(response.status) is not int or not isinstance(response.headers, Mapping):
            raise SourceError("invalid_response", "Die Quelle hat eine unerwartete HTTP-Antwort geliefert.")
        maximum_bytes = MAX_ROBOTS_BYTES if robots else MAX_JSON_BYTES
        if not isinstance(response.body, bytes) or len(response.body) > maximum_bytes:
            raise SourceError("response_too_large", "Die Antwort der Quelle überschreitet die sichere Größenbegrenzung.")
        if response.status == 429:
            raise SourceError("source_rate_limited", "Die Quelle begrenzt weitere Abrufe; Retry-After wird beachtet.", cooldown=_retry_after(response.headers, at))
        if response.status in (401, 403):
            raise SourceError("source_access_denied", "Die Quelle verweigert den Zugriff; keine Umgehung und kein erneuter Versuch in diesem Lauf.", cooldown=86400)
        if response.status != 200:
            raise SourceError("robots_unavailable" if robots else "source_http_error", "Die Quelle hat keinen erfolgreichen, unveränderten HTTPS-Abruf erlaubt.")
        return response
    try:
        robots_response = request(ROBOTS_URL, robots=True)
        body = robots_response.body.decode("utf-8-sig", errors="strict")
        if re.search(r"<!doctype\s+html|<html\b|<script\b", body, re.I) or "text/html" in _headers(robots_response.headers).get("content-type", "").lower():
            raise SourceError("robots_invalid", "Statt robots.txt wurde eine HTML- oder Schutzseite geliefert.")
        policy = RobotsPolicy(body)
        state["robots_checked_at"] = iso_time(at)
        gap = policy.delay
        general_slots = max(1, limit - (1 if pickup_enabled and limit > 1 else 0))
        total = None
        offsets = [0]
        for page_number in range(general_slots):
            if page_number:
                if total is None or total <= page_size:
                    break
                offset = state["general_next_offset"]
                if offset >= min(total, MAX_SOURCE_ITEMS):
                    offset = page_size
                if offset in offsets:
                    break
                offsets.append(offset)
            else:
                offset = 0
            url = _query(offset=offset, page_size=page_size, maximum=maximum, pickup=False, postcode=postcode, radius=radius)
            if not policy.allowed(url):
                raise SourceError("robots_disallowed", "robots.txt erlaubt den vorgesehenen API-Pfad nicht.")
            response = request(url)
            rows, total, observed = _parse_page(response, at, page_size=page_size, offset=offset, pickup=False, postcode=postcode, radius=radius)
            observations.append(observed)
            coverage["pages_fetched"] += 1
            coverage["source_total_reported"] = total
            for row in rows:
                _merge(items, row)
            if offset:
                candidate = offset + page_size
                state["general_next_offset"] = candidate if candidate < min(total, MAX_SOURCE_ITEMS) else page_size
            if not rows:
                state["general_next_offset"] = page_size
                break
        if pickup_enabled and coverage["api_requests_this_run"] < limit:
            url = _query(offset=0, page_size=page_size, maximum=maximum, pickup=True, postcode=postcode, radius=radius)
            if not policy.allowed(url):
                raise SourceError("robots_disallowed", "robots.txt erlaubt die ergänzende Abholsuche nicht.")
            response = request(url)
            rows, pickup_total, observed = _parse_page(response, at, page_size=page_size, offset=0, pickup=True, postcode=postcode, radius=radius)
            observations.append(observed)
            coverage["pages_fetched"] += 1
            coverage["pickup_search_count"] += 1
            coverage["pickup_total_reported"] = pickup_total
            for row in rows:
                _merge(items, row)
        state["last_success_at"] = iso_time(at)
        warning("aggregator_claims_only", "Drittanbieter-Snapshot, keine direkte eBay-Liveprüfung; Versand, Mindestpreis und tatsächlicher Standort können unbekannt sein.")
        warning("bounded_coverage", "Seite eins wird erneut geprüft; weitere Seiten rotieren begrenzt. Die veränderliche Ergebnismenge ist nicht vollständig erfasst.")
        return result("partial")
    except (SourceError, UnicodeError) as error:
        if isinstance(error, SourceError):
            warning(error.code, error.message)
            if error.cooldown is not None:
                elapsed = max(time.monotonic() - started, waited_seconds)
                try:
                    next_time = at + timedelta(seconds=math.ceil(elapsed) + max(MIN_SCAN_INTERVAL, error.cooldown))
                except OverflowError:
                    next_time = datetime.max.replace(tzinfo=timezone.utc, microsecond=0)
                state["next_allowed_at"] = iso_time(next_time)
            limited = error.code in {"source_rate_limited", "source_daily_budget", "robots_delay"}
        else:
            warning("robots_invalid", "robots.txt konnte nicht sicher als UTF-8 gelesen werden.")
            limited = False
        if observations:
            state["last_success_at"] = iso_time(at)
        return result("partial" if observations else "cooldown" if limited else "error")
