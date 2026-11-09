"""Polite, read-only adapter for the verified public Auktionen-ohne-Gebot API.

This is an aggregator, not a direct eBay verification. No outgoing item/image
link is requested. Each scan checks robots.txt and traverses the general and
optional pickup searches from offset zero, interleaving them fairly. It stops
at the reported ends, on anomalies, or at explicit request/time safety caps.
The public search UI uses pages of 48 and the query parameters below. Its
"Auch später endende Auktionen anzeigen" option sends ``endet=alle``; there is
no three-day server filter. The strict active window is evaluated locally.

Publication is planned every thirty minutes. The twenty-nine-minute debounce
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
MAX_API_REQUESTS = 160
DEFAULT_API_REQUESTS = 160
MAX_SOURCE_ITEMS = 9600
MIN_REQUEST_GAP = 10.0
PLANNED_SCAN_INTERVAL = 1800
MIN_SCAN_INTERVAL = 1740
MAX_RUN_SECONDS = 1500
DEFAULT_RATE_LIMIT_BACKOFF = 1800
SOURCE_CACHE_SECONDS = 900
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_DAILY_REQUESTS = 8000


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
    if total < len(articles):
        raise SourceError("invalid_payload", "Die Artikeldaten übersteigen die gemeldete Trefferzahl.")
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
            "pickup_search_observed_at": iso_time(observed) if confirmed else None,
            "source_provider": PROVIDER, "source_url": SOURCE_URL, "verification_level": "aggregator",
            "source_observed_at": iso_time(observed), "last_seen_at": iso_time(observed),
            "source_cache_age_seconds": cache_age,
        })
    return items, total, observed


def _query(*, offset: int, page_size: int, maximum: float, pickup: bool, postcode: str, radius: float,
           end_filter: str) -> str:
    query = {"q": "", "kat": "12576", "max": format(maximum, "g"), "anzahl": str(page_size),
             "ab": str(offset), "ref": "suche", "typ": "ohne", "sort": "ende", "endet": end_filter}
    if pickup:
        query.update(abholung="1", plz=postcode, km=format(radius, "g"), ort="DE")
    else:
        query["gratis"] = "0"
    return ORIGIN + API_PATH + "?" + urllib.parse.urlencode(query)


def _source_state(value: Any, at: datetime, page_size: int) -> dict:
    value = value if isinstance(value, dict) else {}
    state = {"schema_version": 2, "general_next_offset": 0, "request_day": at.date().isoformat(), "requests_today": 0,
             "last_successful_traversals": {}}
    for key in ("last_attempt_at", "last_success_at", "next_allowed_at", "robots_checked_at",
                "last_successful_full_scan_at", "last_full_scan_started_at"):
        parsed = parse_time(value.get(key))
        if parsed and (key == "next_allowed_at" or parsed <= at):
            state[key] = iso_time(parsed)
    # Legacy rotating offsets are deliberately not resumed: a full traversal
    # always starts at zero. Its source is mutable, not a resumable snapshot.
    previous_searches = value.get("last_successful_traversals", {})
    if isinstance(previous_searches, dict):
        for key in ("general", "pickup"):
            parsed = parse_time(previous_searches.get(key))
            if parsed and parsed <= at:
                state["last_successful_traversals"][key] = iso_time(parsed)
    if value.get("request_day") == state["request_day"] and type(value.get("requests_today")) is int:
        state["requests_today"] = max(0, min(value["requests_today"], MAX_DAILY_REQUESTS))
    return state


def _merge(items: dict[str, dict], row: dict) -> None:
    old = items.get(row["id"])
    if not old:
        items[row["id"]] = row
        return
    # General facts and radius-search evidence have independent observation
    # clocks. Neither can make the other look newly verified.
    newer = row if row["source_observed_at"] >= old["source_observed_at"] else old
    merged = dict(newer)
    candidates = [value for value in (old, row) if value["pickup_search_confirmed"]]
    if candidates and newer.get("pickup_available") is not False:
        evidence = max(candidates, key=lambda value: value["pickup_search_observed_at"])
        for key in ("pickup_search_confirmed", "pickup_search_postcode", "pickup_search_radius_km",
                    "pickup_search_observed_at", "pickup_available"):
            merged[key] = evidence[key]
    items[row["id"]] = merged


def collect(settings: Mapping[str, Any], at: datetime, source_state: dict | None, *,
            transport: Callable | None = None, sleep: Callable[[float], None] | None = None,
            monotonic: Callable[[], float] | None = None) -> dict:
    """Traverse both mutable source lists, not an atomic or all-eBay snapshot.

    Network and clock boundaries are injectable. A run permits at most 160
    data requests plus robots and 25 minutes; 8,000 HTTP requests/day bound
    the 48 half-hour runs. Those are safety ceilings, never target counts.
    At 4,200 general + 260 pickup rows, normal page-size-48 traversal needs
    94 data requests. Every enabled search starts at zero; no legacy cursor
    is mistaken for a complete current scan. Partial results are reusable.
    """
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    at = at.astimezone(timezone.utc).replace(microsecond=0)
    page_size = settings.get("aog_page_size", PAGE_SIZE)
    limit = settings.get("aog_max_requests_per_run", DEFAULT_API_REQUESTS)
    duration = settings.get("aog_max_run_seconds", MAX_RUN_SECONDS)
    pickup_enabled = settings.get("aog_pickup_enabled", True)
    maximum, radius = settings.get("max_price_eur", 1), settings.get("pickup_radius_km", 50)
    lookahead, postcode = settings.get("lookahead_hours", 72), settings.get("postcode", "69121")
    if type(page_size) is not int or page_size != PAGE_SIZE or type(limit) is not int or not 1 <= limit <= MAX_API_REQUESTS:
        raise ValueError("unsupported AOG page/request limit")
    if type(duration) is not int or not 30 <= duration <= MAX_RUN_SECONDS:
        raise ValueError("unsupported AOG time limit")
    if type(pickup_enabled) is not bool or not isinstance(postcode, str) or not re.fullmatch(r"[0-9]{5}", postcode):
        raise ValueError("invalid AOG pickup settings")
    if (any(type(value) not in (int, float) for value in (maximum, radius))
            or not 0 <= maximum <= 1000000 or not 0 < radius <= 1000
            or any(not math.isfinite(value) for value in (maximum, radius))):
        raise ValueError("invalid AOG price/radius")
    if type(lookahead) not in (int, float) or not 0 < lookahead <= 744 or not math.isfinite(lookahead):
        raise ValueError("invalid AOG lookahead")
    end_filter = "alle" if lookahead > 24 else "24h"
    state = _source_state(source_state, at, page_size)
    send, pause, clock = transport or _transport, sleep or time.sleep, monotonic or time.monotonic
    started, virtual_wait_seconds = clock(), 0.0
    warnings, items, observations = [], {}, []
    searches = {}
    seen_ids, page_signatures = {}, {}
    for name, enabled in (("general", True), ("pickup", pickup_enabled)):
        searches[name] = {
            "enabled": enabled, "status": "not_started" if enabled else "disabled",
            "traversal_completed": False, "started_at": None, "completed_at": None,
            "last_successful_traversal_at": state["last_successful_traversals"].get(name),
            "pages_fetched": 0, "records_read": 0, "unique_records": 0, "duplicate_records": 0,
            "total_reported_initial": None, "total_reported_latest": None,
            "total_reported_min": None, "total_reported_max": None, "total_changed": False,
            "next_offset": 0, "remaining_reported": None, "unobserved_estimate": None,
            "short_pages": 0, "empty_pages": 0, "sparse_pages": 0, "sparse_slots_estimate": 0,
            "absence_is_evidence": False, "reported_nonempty_but_no_records": False,
            "repeated_pages": 0, "stop_reason": "not_started" if enabled else "disabled",
        }
        seen_ids[name], page_signatures[name] = set(), {}
    coverage = {
        "requests_this_run": 0, "api_requests_this_run": 0, "pages_fetched": 0, "page_size": page_size,
        "source_total_reported": None, "source_cache_max_age_minutes": 15, "complete": False,
        "next_offset": 0, "source_claims_only": True, "pickup_search_count": 0, "pickup_total_reported": None,
        "pagination_is_stable": False, "upstream_end_filter": end_filter, "local_active_window_hours": lookahead,
        "planned_interval_minutes": PLANNED_SCAN_INTERVAL // 60, "debounce_seconds": MIN_SCAN_INTERVAL,
        "daily_request_budget": MAX_DAILY_REQUESTS, "max_api_requests_per_run": limit,
        "max_run_seconds": duration, "max_source_items_per_search": MAX_SOURCE_ITEMS,
        "traversal_completed": False, "scan_started_at": None, "scan_finished_at": None, "scan_completed_at": None,
        "last_successful_full_scan_at": state.get("last_successful_full_scan_at"),
        "records_read": 0, "unique_records": 0, "duplicate_records": 0,
        "reported_nonempty_but_no_records": False, "searches": searches,
    }

    def elapsed() -> float:
        # No-sleep test transports still model requested gaps, without losing
        # independently simulated network latency or counting real sleep twice.
        return max(0.0, clock() - started) + virtual_wait_seconds

    def current_time() -> datetime:
        return at + timedelta(seconds=int(elapsed()))

    def warning(code: str, message: str):
        if not any(value["code"] == code for value in warnings):
            warnings.append({"code": code, "message": message})

    def stop_search(name: str, reason: str, *, completed: bool = False):
        meta = searches[name]
        meta["status"] = "completed" if completed else "partial" if meta["pages_fetched"] else "error"
        meta["stop_reason"] = reason
        meta["traversal_completed"] = completed
        if completed:
            meta["completed_at"] = iso_time(current_time())
            if not meta["reported_nonempty_but_no_records"]:
                meta["last_successful_traversal_at"] = meta["completed_at"]
                state["last_successful_traversals"][name] = meta["completed_at"]

    def result(status: str):
        finished = iso_time(current_time())
        if coverage["scan_started_at"] is not None:
            coverage["scan_finished_at"] = finished
        coverage["elapsed_seconds"] = int(elapsed())
        coverage["requests_today"] = state["requests_today"]
        coverage["next_offset"] = state["general_next_offset"] = searches["general"]["next_offset"]
        coverage["records_read"] = sum(meta["records_read"] for meta in searches.values())
        coverage["unique_records"] = len(items)
        coverage["duplicate_records"] = coverage["records_read"] - len(items)
        coverage["traversal_completed"] = all(meta["traversal_completed"] for meta in searches.values() if meta["enabled"])
        coverage["reported_nonempty_but_no_records"] = any(meta["reported_nonempty_but_no_records"]
                                                          for meta in searches.values() if meta["enabled"])
        if observations and (items or not coverage["reported_nonempty_but_no_records"]):
            state["last_success_at"] = finished
        if coverage["traversal_completed"]:
            coverage["scan_completed_at"] = finished
            if not coverage["reported_nonempty_but_no_records"]:
                state["last_successful_full_scan_at"] = finished
                state["last_full_scan_started_at"] = coverage["scan_started_at"]
        coverage["last_successful_full_scan_at"] = state.get("last_successful_full_scan_at")
        if coverage["reported_nonempty_but_no_records"]:
            warning("source_empty_results", "Mindestens eine Suche meldet Treffer, hat aber keinen Artikel geliefert. Besuchte Offsets sind keine bestätigte aktuelle Artikelliste; ältere Beobachtungen bleiben erhalten.")
            if status == "live":
                status = "partial"
        if coverage["scan_started_at"] is not None:
            warning("aggregator_claims_only", "Drittanbieter-Daten, keine direkte eBay-Liveprüfung und kein atomarer Snapshot; die Trefferliste kann sich während des Durchlaufs verändern.")
            if not coverage["traversal_completed"]:
                warning("bounded_coverage", "Der Durchlauf hat nicht beide Quellenlisten bis zum gemeldeten Ende erreicht; erhaltene Angebote und frühere Beobachtungen bleiben erhalten.")
        return {"items": sorted(items.values(), key=lambda row: (row["ends_at"] or "", row["id"])),
                "source_state": state, "coverage": coverage, "warnings": warnings, "status": status,
                "finished_at": finished, "observed_at": iso_time(min(observations)) if observations else None}

    next_allowed, previous = parse_time(state.get("next_allowed_at")), parse_time(state.get("last_attempt_at"))
    if next_allowed and at < next_allowed or previous and at - previous < timedelta(seconds=MIN_SCAN_INTERVAL):
        warning("source_cooldown", "Die schonende Abrufpause ist noch nicht abgelaufen.")
        return result("cooldown")
    if state["requests_today"] >= MAX_DAILY_REQUESTS:
        state["next_allowed_at"] = iso_time((at + timedelta(days=1)).replace(hour=0, minute=0, second=0))
        warning("source_daily_budget", "Das Tageslimit für diese Quelle ist erreicht.")
        return result("cooldown")
    state["last_attempt_at"] = coverage["scan_started_at"] = iso_time(at)
    state["next_allowed_at"] = iso_time(at + timedelta(seconds=MIN_SCAN_INTERVAL))
    gap = MIN_REQUEST_GAP

    def request(url: str, *, robots: bool = False) -> HttpResponse:
        nonlocal virtual_wait_seconds
        _check_url(url)
        # Budget/deadline checks precede any sleep, so a finished run never
        # waits or sends an unnecessary extra page merely to discover its cap.
        request_day = current_time().date().isoformat()
        if state["request_day"] < request_day:
            state["request_day"], state["requests_today"] = request_day, 0
        if state["requests_today"] >= MAX_DAILY_REQUESTS:
            until_midnight = ((current_time() + timedelta(days=1)).replace(hour=0, minute=0, second=0) - current_time()).total_seconds()
            raise SourceError("source_daily_budget", "Das Tageslimit für diese Quelle ist erreicht.", cooldown=until_midnight)
        if not robots and coverage["api_requests_this_run"] >= limit:
            raise SourceError("source_run_budget", "Die Quellenliste überschreitet das Seitenbudget dieses Durchlaufs.")
        delay = gap if coverage["requests_this_run"] else 0
        if delay > 60:
            raise SourceError("robots_delay", "Die robots.txt-Verzögerung überschreitet den begrenzten Einzelabruf.", cooldown=delay)
        if elapsed() + delay + 20 > duration:
            raise SourceError("source_run_deadline", "Der Durchlauf endet am Zeitlimit; verbleibende Seiten werden nicht als gelesen ausgegeben.")
        if delay:
            before_pause = clock()
            pause(delay)
            virtual_wait_seconds += max(0.0, delay - max(0.0, clock() - before_pause))
        # Waiting itself can cross midnight. Account the following request to
        # its actual UTC day rather than quietly dropping it on the next run.
        request_day = current_time().date().isoformat()
        if state["request_day"] < request_day:
            state["request_day"], state["requests_today"] = request_day, 0
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
            raise SourceError("source_rate_limited", "Die Quelle begrenzt weitere Abrufe; Retry-After wird beachtet.", cooldown=_retry_after(response.headers, current_time()))
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
        state["robots_checked_at"] = iso_time(current_time())
        gap = policy.delay
        pending = [name for name, meta in searches.items() if meta["enabled"]]
        while pending:
            # Round-robin only while both lists have remaining pages. Small
            # pickup results finish quickly; general then receives every slot.
            name = pending.pop(0)
            meta, pickup = searches[name], name == "pickup"
            offset = meta["next_offset"]
            if offset >= MAX_SOURCE_ITEMS:
                stop_search(name, "source_growth_limit")
                warning("source_growth_limit", "Die Quellenliste ist über die begrenzte Traversierungsgröße gewachsen; Abdeckung bleibt ausdrücklich unvollständig.")
                continue
            url = _query(offset=offset, page_size=page_size, maximum=maximum, pickup=pickup, postcode=postcode, radius=radius, end_filter=end_filter)
            if not policy.allowed(url):
                raise SourceError("robots_disallowed", "robots.txt erlaubt den vorgesehenen API-Pfad nicht.")
            if meta["started_at"] is None:
                meta["started_at"] = iso_time(current_time())
            response = request(url)
            rows, total, observed = _parse_page(response, current_time(), page_size=page_size, offset=offset,
                                               pickup=pickup, postcode=postcode, radius=radius)
            observations.append(observed)
            coverage["source_cache_max_age_minutes"] = max(coverage["source_cache_max_age_minutes"],
                                                           math.ceil((current_time() - observed).total_seconds() / 60))
            coverage["pages_fetched"] += 1
            coverage["pickup_search_count"] += int(pickup)
            coverage["pickup_total_reported" if pickup else "source_total_reported"] = total
            meta["pages_fetched"] += 1
            meta["records_read"] += len(rows)
            if meta["total_reported_initial"] is None:
                meta["total_reported_initial"] = meta["total_reported_min"] = meta["total_reported_max"] = total
            meta["total_reported_latest"] = total
            meta["total_reported_min"] = min(meta["total_reported_min"], total)
            meta["total_reported_max"] = max(meta["total_reported_max"], total)
            meta["total_changed"] = meta["total_reported_min"] != meta["total_reported_max"]
            ids = tuple(row["id"] for row in rows)
            signature = frozenset(ids)
            # Remember the first requested offset for a matching ID set. The
            # stop criterion stays order-independent; the diagnostic can now
            # identify the source pagination failure without exposing listings.
            previous_offset = page_signatures[name].get(signature)
            repeated = bool(rows and previous_offset is not None)
            page_signatures[name].setdefault(signature, offset)
            seen_ids[name].update(ids)
            meta["unique_records"] = len(seen_ids[name])
            meta["duplicate_records"] = meta["records_read"] - meta["unique_records"]
            # The production pickup search can return only a few articles for
            # a full requested page. Empty/short arrays are therefore not EOF.
            # The source's filtering semantics are unknown: traverse the next
            # requested offset, not offset + returned_count, until its reported
            # end. This visits offsets; it never proves unique completeness.
            meta["next_offset"] = offset + page_size
            meta["remaining_reported"] = max(0, total - meta["next_offset"])
            meta["unobserved_estimate"] = max(0, total - meta["unique_records"])
            meta["short_pages"] += int(len(rows) < page_size)
            meta["empty_pages"] += int(not rows)
            sparse_slots = max(0, min(page_size, total - offset) - len(rows))
            meta["sparse_pages"] += int(sparse_slots > 0)
            meta["sparse_slots_estimate"] += sparse_slots
            meta["reported_nonempty_but_no_records"] = meta["total_reported_max"] > 0 and not meta["records_read"]
            if sparse_slots:
                warning("sparse_pages", "Die Quelle liefert weniger Artikel als die angefragten Seitenpositionen erwarten lassen. Weitere gemeldete Offsets werden gelesen; Zähldifferenzen können Filterung oder Änderungen betreffen und beweisen keine Zahl fehlender Angebote.")
            for row in rows:
                _merge(items, row)
            if total > MAX_SOURCE_ITEMS:
                warning("source_growth_limit", "Die gemeldete Quellenliste überschreitet die Traversierungsgrenze; ein vollständiger Durchlauf ist mit diesen Schutzgrenzen nicht möglich.")
            if repeated:
                meta["repeated_pages"] += 1
                stop_search(name, "repeated_page")
                search_label = "Abholsuche" if pickup else "Allgemeine Suche"
                warning("repeated_page", f"{search_label}: Die Quelle hat bei Offset {offset} dieselben Angebots-IDs wie bei Offset {previous_offset} geliefert; diese Suche wird sicherheitshalber beendet.")
            elif meta["next_offset"] >= total:
                stop_search(name, "reported_end", completed=True)
            else:
                meta["status"], meta["stop_reason"] = "partial", "not_finished"
                pending.append(name)
        completed = all(meta["traversal_completed"] for meta in searches.values() if meta["enabled"])
        return result("live" if completed else "partial")
    except (SourceError, UnicodeError) as error:
        code = error.code if isinstance(error, SourceError) else "robots_invalid"
        if isinstance(error, SourceError):
            warning(code, error.message)
            if error.cooldown is not None:
                try:
                    next_time = at + timedelta(seconds=math.ceil(elapsed()) + max(MIN_SCAN_INTERVAL, error.cooldown))
                except OverflowError:
                    next_time = datetime.max.replace(tzinfo=timezone.utc, microsecond=0)
                state["next_allowed_at"] = iso_time(next_time)
        else:
            warning(code, "robots.txt konnte nicht sicher als UTF-8 gelesen werden.")
        for name, meta in searches.items():
            if meta["enabled"] and not meta["traversal_completed"] and meta["stop_reason"] in {"not_started", "not_finished"}:
                stop_search(name, code)
        limited = code in {"source_rate_limited", "source_daily_budget", "robots_delay"}
        return result("partial" if observations else "cooldown" if limited else "error")
