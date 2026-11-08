"""Budgeted public-source discovery and static-feed orchestration; no bidding.

HUNTER_PUBLISH_LIVE_DATA=true is required before publishing eBay/local JSON data.
Deleting today's snapshot does not remove content from public Git history.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .ebay import ApiError, BudgetExceeded, CredentialsMissing, EbayClient, RateLimitError
from .models import iso_time, normalize_item, parse_time
from .ranking import assess_item

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.json"
STATE_VERSION = 2
FEED_VERSION = 3
MAX_INPUT_BYTES = 32 * 1024 * 1024
MODES = ("delivery", "pickup")
DEFAULTS = {
    "marketplace": "EBAY_DE", "root_category_id": "12576", "postcode": "69121",
    "pickup_radius_km": 50, "max_price_eur": 10, "max_shipping_eur": 15,
    "max_bids": 0, "lookahead_hours": 72,
    "browse_daily_budget": 4500, "browse_calls_per_run": 85,
    "max_pages_per_category": 2, "categories_per_run": 20,
    "detail_limit_per_run": 35, "description_max_age_hours": 24,
    "refresh_after_minutes": 90, "max_records": 800,
    "max_ai_items_per_run": 20, "api_page_size": 50,
    "schedule_interval_minutes": 10, "aog_max_requests_per_run": 4,
}
# State contains normalized records, never seller identities or raw payloads.
RECORD_FIELDS = frozenset({
    "id", "title", "description", "url", "image", "price_eur", "price_kind",
    "shipping_eur", "bid_count", "ends_at", "last_seen_at", "content_updated_at",
    "city", "postcode", "country", "distance_km", "condition", "buying_options",
    "is_auction", "reserve_status", "local_pickup_confirmed", "pickup_radius_km",
    "details_fetched", "eligibility", "eligibility_reasons", "eligibility_without_time_window", "interest_score", "score",
    "reasons", "risks", "value_band", "assessment_confidence", "assessment_basis",
    "semantic_assessed", "semantic_assessed_at", "value_reason", "fact_score", "fact_reasons", "fact_risks",
    "product_kind", "semantic_interest", "next_checks", "total_known_eur",
    "score_version", "scalp_score", "useability_score", "useability_reason", "dependencies",
    "stale", "carried", "pickup_verified_at",
    "source_provider", "source_url", "source_observed_at", "verification_level", "pickup_available",
    "pickup_search_confirmed", "pickup_search_postcode", "pickup_search_radius_km",
    "semantic_review_mode",
})


class ConfigurationError(ValueError):
    """Malformed configuration/state/input, not an expected upstream blocker."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _count(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigurationError(f"{name} must be an integer between {low} and {high}.")
    return value


def _finite(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ConfigurationError(f"{name} is outside its supported numeric range.")
    return float(value)


def validate_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, Mapping):
        raise ConfigurationError("Settings must be a JSON object.")
    result = {**DEFAULTS, **dict(settings)}
    # The old source-age limit now migrates only the separate text TTL. An
    # unchanged observation is never a reason to expire an ongoing auction.
    if "max_freshness_hours" in settings:
        _finite(settings["max_freshness_hours"], "max_freshness_hours", 0.01, 24)
        if "description_max_age_hours" not in settings:
            result["description_max_age_hours"] = settings["max_freshness_hours"]
        result.pop("max_freshness_hours", None)
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError):
        raise ConfigurationError("Settings contain non-JSON or nonfinite values.") from None
    if result["marketplace"] != "EBAY_DE":
        raise ConfigurationError("Only EBAY_DE is supported.")
    postcode = result["postcode"]
    if not isinstance(postcode, str) or len(postcode) != 5 or not postcode.isascii() or not postcode.isdigit():
        raise ConfigurationError("postcode must be a five-digit German postal code.")
    root_id = result["root_category_id"]
    if isinstance(root_id, bool) or not str(root_id).isascii() or not str(root_id).isdigit() or len(str(root_id)) > 20:
        raise ConfigurationError("root_category_id must be a numeric eBay category ID.")
    result["root_category_id"] = str(root_id)
    for key, bounds in {
        "browse_daily_budget": (1, 5000), "browse_calls_per_run": (1, 5000),
        "categories_per_run": (1, 1000), "max_pages_per_category": (1, 50),
        "detail_limit_per_run": (0, 1000), "max_records": (1, 800),
        "max_ai_items_per_run": (0, 100), "api_page_size": (1, 100), "max_bids": (0, 1000000),
        "schedule_interval_minutes": (5, 1440), "aog_max_requests_per_run": (1, 6),
    }.items():
        _count(result[key], key, *bounds)
    for key, bounds in {
        "max_price_eur": (0, 1000000000), "max_shipping_eur": (0, 1000000),
        "pickup_radius_km": (0.01, 1000), "lookahead_hours": (0.01, 744),
        "description_max_age_hours": (0.01, 24), "refresh_after_minutes": (1, 180),
    }.items():
        _finite(result[key], key, *bounds)
    return result


def new_state(at: datetime) -> dict[str, Any]:
    return {
        "schema_version": STATE_VERSION, "updated_at": iso_time(at),
        "daily_budget": {"date": at.date().isoformat(), "calls_used": 0},
        "cursor": {"category_id": None, "mode": "delivery", "offset": 0},
        "taxonomy": {}, "records": [], "last_successful_scan_at": None,
    }


def prepare_state(state: dict[str, Any] | None, at: datetime) -> dict[str, Any]:
    if state is None:
        return new_state(at)
    if not isinstance(state, dict) or state.get("schema_version") != STATE_VERSION:
        raise ConfigurationError("State schema is incompatible; migrate explicitly.")
    result = {key: copy.deepcopy(value) for key, value in state.items() if key in {
        "schema_version", "updated_at", "daily_budget", "cursor", "taxonomy", "records",
        "last_successful_scan_at", "semantic_budget", "semantic_cache",
        "aog", "active_source",
    }}
    budget = result.get("daily_budget")
    if not isinstance(budget, dict) or not isinstance(budget.get("date"), str):
        raise ConfigurationError("State is missing its daily budget ledger.")
    used = _count(budget.get("calls_used"), "state.daily_budget.calls_used", 0, 1000000)
    try:
        budget_day = datetime.strptime(budget["date"], "%Y-%m-%d").date()
    except ValueError:
        raise ConfigurationError("State has an invalid daily budget date.") from None
    if budget_day > at.date():
        raise ConfigurationError("State budget is dated in the future; refusing to reset it.")
    result["daily_budget"] = {"date": at.date().isoformat(), "calls_used": used if budget_day == at.date() else 0}
    if not isinstance(result.get("records", []), list):
        raise ConfigurationError("State records must be an array.")
    cursor = result.get("cursor", {})
    if not isinstance(cursor, dict) or cursor.get("mode", "delivery") not in MODES:
        raise ConfigurationError("State cursor has an invalid mode.")
    offset = _count(cursor.get("offset", 0), "state.cursor.offset", 0, 9800)
    if offset % 200:
        raise ConfigurationError("State cursor offset must be a multiple of 200.")
    result["cursor"] = {"category_id": cursor.get("category_id"), "mode": cursor.get("mode", "delivery"), "offset": offset}
    if not isinstance(result.get("taxonomy", {}), dict):
        raise ConfigurationError("State taxonomy must be an object.")
    result.setdefault("taxonomy", {})
    result.setdefault("records", [])
    result.setdefault("last_successful_scan_at", None)
    result["updated_at"] = iso_time(at)
    return result


def _public_record(item: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in item.items() if key in RECORD_FIELDS}


def _retained(item: Any, at: datetime) -> bool:
    """Keep known, ongoing auctions until their end, not until a source TTL.

    Observation age is a warning about facts, never a listing lifetime. The
    active lookahead window is a separate eligibility check, recomputed each run.
    """
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return False
    end, seen = parse_time(item.get("ends_at")), parse_time(item.get("last_seen_at"))
    return bool(end and seen and at < end and seen <= at)


def _sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    eligibility = {"eligible": 0, "review": 1, "excluded": 2}.get(item.get("eligibility"), 1)
    score = item.get("interest_score", item.get("score", 0))
    score = float(score) if type(score) in (int, float) and math.isfinite(score) else 0
    return (eligibility, -score, str(item.get("ends_at", "")), str(item.get("id", "")))


def _reviewed_capacity_order(rows: list[dict[str, Any]], review_doc: dict | None, at: datetime) -> list[dict[str, Any]]:
    """Restore only validated local reviews before irreversible capacity cuts.

    Recomputing facts starts every row with its heuristic. Discarding rows at
    that point can lose an already reviewed high-priority item permanently.
    This pass performs no model or source requests; expiry, content hashes and
    factual eligibility still govern which annotations may be reused. The
    normal post-API pass remains responsible for precedence and public counts.
    """
    if review_doc is not None:
        from .reviews import apply_reviews
        rows, _ = apply_reviews(rows, review_doc, at)
    return sorted(rows, key=_sort_key)


def _clear_stale_description(item: dict, at: datetime, settings: Mapping) -> dict:
    updated = parse_time(item.get("content_updated_at"))
    if not updated or updated > at or at - updated >= timedelta(hours=settings["description_max_age_hours"]):
        item["description"] = ""
    return item


def _block(blockers: list, code: str, message: str, action: str) -> None:
    if not any(old["code"] == code for old in blockers):
        blockers.append({"code": code, "message": message, "action": action})


def _upsert(records: dict, item: dict, at: datetime, settings: Mapping, assess: Callable) -> None:
    old = records.get(item["id"])
    if old and old.get("source_provider") != item.get("source_provider"):
        old = None
    if old and item.get("source_provider") == "auktionen-ohne-gebot":
        previous_seen = parse_time(old.get("source_observed_at"))
        incoming_seen = parse_time(item.get("source_observed_at"))
        if previous_seen and incoming_seen and incoming_seen < previous_seen:
            # Differently cached source pages can arrive out of order. An
            # older zero-bid summary must not undo newer price/bid evidence.
            preserved = dict(old)
            preserved.update(carried=True, stale=at - previous_seen >= timedelta(minutes=settings["refresh_after_minutes"]))
            records[item["id"]] = _public_record(assess(preserved, settings, at))
            return
    if old:
        # A summary from the other delivery mode cannot overwrite details read
        # in the same run. Older price/bid/reserve values are never filled in.
        if old.get("last_seen_at") == iso_time(at) and old.get("details_fetched") and not item.get("details_fetched"):
            current = dict(old)
            if item.get("local_pickup_confirmed"):
                current.update(local_pickup_confirmed=True, pickup_verified_at=iso_time(at), distance_km=item.get("distance_km"))
            item = current
        elif (not item.get("details_fetched") and old.get("description") and old.get("details_fetched")
              and parse_time(old.get("content_updated_at"))
              and timedelta(0) <= at - parse_time(old["content_updated_at"]) < timedelta(hours=settings["description_max_age_hours"])):
            item = dict(item)
            item["description"] = old["description"]
            item["content_updated_at"] = old.get("content_updated_at", old.get("last_seen_at"))
        if not item.get("image") and old.get("image"):
            item["image"] = old["image"]
        if old.get("local_pickup_confirmed") and old.get("pickup_verified_at") == iso_time(at):
            item.update(local_pickup_confirmed=True, pickup_verified_at=iso_time(at), distance_km=old.get("distance_km"))
    if item.get("local_pickup_confirmed"):
        item.setdefault("pickup_verified_at", iso_time(at))
    seen = parse_time(item.get("last_seen_at"))
    item.update(carried=False, stale=not seen or at - seen >= timedelta(minutes=settings["refresh_after_minutes"]))
    _clear_stale_description(item, at, settings)
    records[item["id"]] = _public_record(assess(item, settings, at))


def _leaves_valid(value: Any) -> bool:
    return bool(isinstance(value, list) and value and all(
        isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].isascii()
        and row["id"].isdigit() and isinstance(row.get("name"), str) for row in value
    ))


def _taxonomy(client: Any, state: dict, settings: dict, at: datetime, manual_ids: list[str]) -> list[dict[str, str]]:
    if manual_ids:
        # An explicit operator-selected leaf list is not a replacement for the
        # cached complete taxonomy. Removing the override restores normal scans.
        return [{"id": category, "name": f"Manuelle Blattkategorie {category}"} for category in manual_ids]
    cached = state["taxonomy"]
    fetched = parse_time(cached.get("fetched_at"))
    if (cached.get("marketplace") == settings["marketplace"]
        and cached.get("root_category_id") == settings["root_category_id"]
        and fetched and timedelta(0) <= at - fetched <= timedelta(days=7)
        and _leaves_valid(cached.get("leaves"))):
        return cached["leaves"]
    leaves = client.leaf_categories(settings["root_category_id"])
    if not _leaves_valid(leaves):
        raise ApiError("eBay returned no usable leaf categories.")
    unique = {row["id"]: {"id": row["id"], "name": row["name"]} for row in leaves}
    leaves = [unique[key] for key in sorted(unique, key=int)]
    state["taxonomy"] = {"marketplace": settings["marketplace"], "root_category_id": settings["root_category_id"], "fetched_at": iso_time(at), "leaves": leaves}
    return leaves


def _advance(cursor: dict, ids: list[str]) -> bool:
    cursor["offset"] = 0
    if cursor["mode"] == "delivery":
        cursor["mode"] = "pickup"
        return False
    cursor.update(category_id=ids[(ids.index(cursor["category_id"]) + 1) % len(ids)], mode="delivery")
    return True


def _failure(blockers: list, error: ApiError) -> bool:
    """Record safe public diagnostics; return whether further calls must stop."""
    if isinstance(error, BudgetExceeded):
        _block(blockers, "browse_budget_exhausted", "Das Abrufbudget dieses Laufs ist ausgeschöpft.", "Der nächste Lauf setzt den gespeicherten Suchcursor fort.")
        return True
    if isinstance(error, RateLimitError):
        _block(blockers, "ebay_rate_limited", "eBay begrenzt derzeit weitere API-Abrufe.", "Später erneut ausführen; Kontingent im eBay-Entwicklerkonto prüfen.")
        return True
    if isinstance(error, CredentialsMissing):
        _block(blockers, "ebay_credentials_missing", "Die eBay-Produktionsschlüssel fehlen.", "EBAY_CLIENT_ID und EBAY_CLIENT_SECRET als GitHub Actions Secrets hinterlegen.")
        return True
    if error.status_code in (401, 403):
        _block(blockers, "ebay_access_denied", "eBay hat den API-Zugriff abgelehnt.", "Produktionsschlüssel, App-Freischaltung und Browse-Zugangsberechtigung prüfen.")
        return True
    _block(blockers, "ebay_request_failed", "Mindestens ein eBay-Abruf konnte nicht bestätigt werden.", "Der Feed bleibt unvollständig; spätere Läufe erneut prüfen.")
    return False


def _scan_stats() -> dict[str, Any]:
    return {"received": 0, "searches_succeeded": 0, "searches_failed": 0, "details_fetched": 0,
            "detail_failures": 0, "refreshed": 0, "categories_scanned": 0, "categories_completed": 0,
            "categories_planned": 0, "categories_total": 0, "pages_omitted": 0, "budget_limited": False}


def _discover(client: Any, state: dict, records: dict, settings: dict, at: datetime,
              normalize: Callable, assess: Callable, blockers: list, stats: dict, manual_ids: list[str]) -> None:
    detail_limit, detail_attempts = settings["detail_limit_per_run"], 0
    due = []
    for item in records.values():
        age, remaining = at - parse_time(item["last_seen_at"]), parse_time(item["ends_at"]) - at
        if item.get("eligibility") != "excluded" and (age >= timedelta(minutes=settings["refresh_after_minutes"])
                or (remaining <= timedelta(hours=3) and age >= timedelta(minutes=30))):
            due.append(item)
    due.sort(key=lambda row: (row["ends_at"], _sort_key(row), row["last_seen_at"]))
    for old in due[:detail_limit]:
        detail_attempts += 1
        try:
            raw = {**client.get_item(old["id"]), "_details_fetched": True}
            # A detail call cannot renew earlier radius-search evidence.
            item = normalize(raw, at, local_pickup_confirmed=False, pickup_radius_km=settings["pickup_radius_km"])
            if item is None:
                records.pop(old["id"], None)
            else:
                _upsert(records, item, at, settings, assess)
            stats["details_fetched"] += 1
            stats["refreshed"] += 1
        except ApiError as error:
            stats["detail_failures"] += 1
            if error.status_code in (404, 410):
                records.pop(old["id"], None)
                continue
            if _failure(blockers, error):
                stats["budget_limited"] = isinstance(error, BudgetExceeded)
                return
    if len(due) > detail_limit:
        _block(blockers, "refresh_pending", "Nicht alle fälligen Detailprüfungen passen in diesen Lauf.",
               "Ältere Angaben bleiben bis zum Auktionsende als prüfbedürftig erhalten; ihr Beobachtungszeitpunkt wird nicht erneuert.")
    leaves = _taxonomy(client, state, settings, at, manual_ids)
    ids, cursor = [row["id"] for row in leaves], state["cursor"]
    stats["categories_total"] = len(ids)
    if cursor.get("category_id") not in ids:
        cursor.update(category_id=ids[0], mode="delivery", offset=0)
    # Search rankings change as auctions end or gain bids. A saved offset is not
    # a stable continuation token; replay each visited mode from its first page.
    cursor["offset"] = 0
    planned = min(settings["categories_per_run"], len(ids))
    stats["categories_planned"] = planned
    completed, pages, scanned = 0, {}, set()
    raw_by_id, pickup_ids = {}, set()
    reserve_details = max(0, detail_limit - detail_attempts)
    scan_ceiling = max(client.calls_used, client.max_calls - reserve_details)
    if scan_ceiling == client.calls_used and client.calls_used < client.max_calls:
        scan_ceiling += 1
    stopped = False
    while completed < planned:
        if client.calls_used >= scan_ceiling:
            stats["budget_limited"] = True
            _block(blockers, "scan_budget_limited", "Der geplante Suchbereich passt nicht vollständig in das Abrufbudget.", "Die gespeicherte Kategorie und Versandart werden im nächsten Lauf erneut ab Seite eins geprüft.")
            break
        category, mode, offset = cursor["category_id"], cursor["mode"], cursor["offset"]
        key = (category, mode)
        if pages.get(key, 0) >= settings["max_pages_per_category"]:
            stats["pages_omitted"] += 1
            _block(blockers, "pagination_pending", "Eine Kategorie hat weitere Ergebnisse außerhalb des Seitenbudgets.", "Die Suche wechselt zur nächsten Kategorie oder Versandart; dichte Kategorien bleiben ausdrücklich unvollständig.")
            completed += int(_advance(cursor, ids))
            continue
        try:
            page = client.search(category_id=category, now=at, lookahead_hours=settings["lookahead_hours"],
                max_price_eur=None, max_bids=settings["max_bids"], local_pickup=mode == "pickup",
                postcode=settings["postcode"], pickup_radius_km=settings["pickup_radius_km"], offset=offset, limit=200)
        except ApiError as error:
            stats["searches_failed"] += 1
            stopped = _failure(blockers, error)
            if stopped:
                stats["budget_limited"] = isinstance(error, BudgetExceeded)
                break
            completed += int(_advance(cursor, ids))
            continue
        pages[key] = pages.get(key, 0) + 1
        scanned.add(category)
        stats["searches_succeeded"] += 1
        stats["received"] += len(page["items"])
        for raw in page["items"]:
            item = normalize(raw, at, local_pickup_confirmed=mode == "pickup", pickup_radius_km=settings["pickup_radius_km"])
            if item is None:
                continue
            item_id = item["id"]
            raw_by_id[item_id] = {**raw_by_id.get(item_id, {}), **raw}
            if mode == "pickup":
                pickup_ids.add(item_id)
            _upsert(records, item, at, settings, assess)
        if page.get("capped"):
            stats["pages_omitted"] += 1
            _block(blockers, "ebay_result_cap", "eBay begrenzt diese Suchabfrage auf 10.000 Ergebnisse.", "Zeitraum oder Kategorie weiter aufteilen; die Abdeckung ist nicht vollständig.")
        if page["has_more"]:
            next_offset = page.get("next_offset")
            if type(next_offset) is not int or next_offset <= offset or next_offset > 9800 or next_offset % 200:
                raise ConfigurationError("eBay client returned invalid pagination metadata.")
            cursor["offset"] = next_offset
        else:
            completed += int(_advance(cursor, ids))
    stats.update(categories_scanned=len(scanned), categories_completed=completed)
    cursor["offset"] = 0
    candidates = []
    for item_id in raw_by_id:
        item, price = records[item_id], records[item_id].get("price_eur")
        if (item.get("bid_count") in (None, 0) and item.get("is_auction") in (None, True) and item.get("eligibility") != "excluded"
                and (price is None or (type(price) in (int, float) and price <= settings["max_price_eur"]))
                and not (item.get("details_fetched") and parse_time(item.get("content_updated_at")) == at)):
            candidates.append(item)
    candidates.sort(key=_sort_key)
    if not stopped:
        for old in candidates[:max(0, detail_limit - detail_attempts)]:
            if client.calls_used >= client.max_calls:
                stats["budget_limited"] = True
                break
            detail_attempts += 1
            try:
                raw = {**raw_by_id[old["id"]], **client.get_item(old["id"]), "_details_fetched": True}
                item = normalize(raw, at, local_pickup_confirmed=old["id"] in pickup_ids, pickup_radius_km=settings["pickup_radius_km"])
                if item is None:
                    records.pop(old["id"], None)
                else:
                    _upsert(records, item, at, settings, assess)
                stats["details_fetched"] += 1
            except ApiError as error:
                stats["detail_failures"] += 1
                if error.status_code in (404, 410):
                    records.pop(old["id"], None)
                    continue
                if _failure(blockers, error):
                    stats["budget_limited"] = isinstance(error, BudgetExceeded)
                    break


def run_pipeline(settings: Mapping[str, Any], *, source: str = "auto", state: dict | None = None,
                 raw_items: list[dict] | None = None, at: datetime | None = None,
                 environ: Mapping[str, str] | None = None, client_factory: Callable = EbayClient,
                 normalize: Callable = normalize_item, assess: Callable = assess_item,
                 enrich: Callable | None = None, checkpoint: Callable | None = None,
                 aog_collect: Callable | None = None, review_doc: dict | None = None) -> tuple[dict, dict]:
    """Return feed/state; inject dependencies for hermetic tests.

    Expected upstream blockers produce a feed. Invalid configuration/programming
    errors propagate. checkpoint persists the quota ledger in finally even if a
    programming error aborts a run after consuming paid/limited requests.
    """
    settings, at = validate_settings(settings), at or now_utc()
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ConfigurationError("at must be timezone-aware.")
    at = at.astimezone(timezone.utc).replace(microsecond=0)
    state = prepare_state(state, at)
    env = os.environ if environ is None else environ
    if source not in {"auto", "disabled", "json", "ebay", "aog"}:
        raise ConfigurationError("Unsupported source.")
    manual_text = env.get("EBAY_LEAF_CATEGORY_IDS", "").strip()
    manual_ids = [part.strip() for part in manual_text.split(",")] if manual_text else []
    if len(manual_ids) > 1000 or any(not value.isascii() or not value.isdigit() or len(value) > 20 for value in manual_ids):
        raise ConfigurationError("EBAY_LEAF_CATEGORY_IDS must be a comma-separated list of numeric leaf IDs.")
    manual_ids = sorted(set(manual_ids), key=int)
    records = {}
    for cached in state["records"]:
        # Changing data source must not relabel old records or inherit richer
        # eBay details under an aggregator-only claim (or vice versa).
        if isinstance(cached, dict) and ((source == "aog") != (cached.get("source_provider") == "auktionen-ohne-gebot")):
            continue
        if _retained(cached, at):
            item = _public_record(cached)
            _clear_stale_description(item, at, settings)
            item.update(carried=True, stale=at - parse_time(item["last_seen_at"]) >= timedelta(minutes=settings["refresh_after_minutes"]))
            assessed = _public_record(assess(item, settings, at))
            old = records.get(assessed["id"])
            if old is None or assessed["last_seen_at"] > old["last_seen_at"]:
                records[assessed["id"]] = assessed
    blockers, scan, status = [], _scan_stats(), "unconfigured"
    provider, calls_this_run = ("ebay" if source in {"auto", "ebay"} else source), 0
    source_coverage = {}
    if source == "aog":
        provider = "auktionen-ohne-gebot"
    if state.get("active_source") and state["active_source"] != provider:
        state["last_successful_scan_at"] = None
        state.pop("semantic_cache", None)
    state["active_source"] = provider
    if source == "disabled":
        status = "disabled"
        records.clear()
        _block(blockers, "source_disabled", "Die Datenquelle ist ausgeschaltet.", "HUNTER_SOURCE=aog aktiviert den öffentlichen Sammler; ebay wählt die optionale eBay-API.")
    elif env.get("HUNTER_PUBLISH_LIVE_DATA", "").lower() != "true":
        records.clear()
        _block(blockers, "publication_not_enabled", "Die Veröffentlichung echter Angebotsdaten ist noch nicht freigegeben.", "Datenrechte und öffentliche Git-Historie prüfen; anschließend HUNTER_PUBLISH_LIVE_DATA=true setzen.")
        if source in {"auto", "ebay"} and (not env.get("EBAY_CLIENT_ID", "").strip() or not env.get("EBAY_CLIENT_SECRET", "").strip()):
            _failure(blockers, CredentialsMissing())
    elif source in {"auto", "ebay"} and (not env.get("EBAY_CLIENT_ID", "").strip() or not env.get("EBAY_CLIENT_SECRET", "").strip()):
        records.clear()
        _failure(blockers, CredentialsMissing())
    elif source == "aog":
        if aog_collect is None:
            from .aog import collect as aog_collect
        result = aog_collect(settings, at, state.get("aog"))
        if (not isinstance(result, dict) or result.get("status") not in {"live", "partial", "error", "cooldown"}
                or not isinstance(result.get("items"), list) or len(result["items"]) > 1000
                or not isinstance(result.get("source_state"), dict) or not isinstance(result.get("coverage"), dict)):
            raise ConfigurationError("Public source returned an invalid result contract.")
        state["aog"] = result["source_state"]
        if checkpoint is not None:
            # Persist the crawl cooldown even if a later processing step fails.
            checkpoint(state)
        source_coverage = result["coverage"]
        scan["received"] = len(result["items"])
        scan["searches_succeeded"] = _count(source_coverage.get("pages_fetched", 0), "aog.pages_fetched", 0, 6)
        scan["categories_scanned"] = int(scan["searches_succeeded"] > 0)
        scan["categories_total"] = scan["categories_planned"] = 1
        for raw in result["items"]:
            if not isinstance(raw, dict):
                raise ConfigurationError("Public source item is not an object.")
            observed = parse_time(raw.get("source_observed_at"))
            if observed is None or observed > at or raw.get("source_provider") != provider:
                continue
            item = normalize(raw, at, local_pickup_confirmed=False, pickup_radius_km=settings["pickup_radius_km"])
            if item is not None:
                _upsert(records, item, at, settings, assess)
        for warning in result.get("warnings", []):
            if isinstance(warning, dict) and isinstance(warning.get("code"), str) and isinstance(warning.get("message"), str):
                _block(blockers, warning["code"], warning["message"],
                       "Quellenstand und Abdeckung beachten; vor einem Gebot das Originalangebot prüfen. Der nächste geplante Lauf berücksichtigt Wartezeiten.")
        source_status = result["status"]
        status = ("partial" if records else "error") if source_status in {"cooldown", "error"} else source_status
        if scan["searches_succeeded"]:
            state["last_successful_scan_at"] = iso_time(at)
        if source_status == "error":
            scan["searches_failed"] = 1
        if checkpoint is not None:
            checkpoint(state)
    elif source == "json":
        if not isinstance(raw_items, list) or len(raw_items) > 10000 or any(not isinstance(row, dict) for row in raw_items):
            raise ConfigurationError("JSON source must contain at most 10,000 item objects.")
        scan["received"] = len(raw_items)
        for raw in raw_items:
            item = normalize(raw, at, local_pickup_confirmed=False, pickup_radius_km=settings["pickup_radius_km"])
            if item is not None:
                _upsert(records, item, at, settings, assess)
        status, state["last_successful_scan_at"] = "live", iso_time(at)
    else:
        remaining = settings["browse_daily_budget"] - state["daily_budget"]["calls_used"]
        if remaining <= 0:
            status, scan["budget_limited"] = ("partial" if records else "error"), True
            _block(blockers, "daily_budget_exhausted", "Das konfigurierte Tagesbudget für eBay-Abrufe ist ausgeschöpft.", "Der nächste UTC-Tag erhält ein neues lokales Budget; eBay-Kontingente gelten zusätzlich.")
        else:
            client = client_factory(client_id=env["EBAY_CLIENT_ID"], client_secret=env["EBAY_CLIENT_SECRET"],
                marketplace=settings["marketplace"], postcode=settings["postcode"], max_calls=min(settings["browse_calls_per_run"], remaining))
            try:
                _discover(client, state, records, settings, at, normalize, assess, blockers, scan, manual_ids)
            except ApiError as error:
                _failure(blockers, error)
                status = "partial" if records or scan["searches_succeeded"] else "error"
            else:
                success = scan["searches_succeeded"] or scan["refreshed"]
                incomplete = (scan["searches_failed"] or scan["detail_failures"] or scan["pages_omitted"] or scan["budget_limited"]
                              or scan["categories_completed"] < scan["categories_planned"] or bool(blockers))
                status = "partial" if incomplete and (success or records) else "error" if incomplete else "live"
            finally:
                calls_this_run = _count(client.calls_used, "client.calls_used", 0, 1000000)
                state["daily_budget"]["calls_used"] += calls_this_run
                state["cursor"]["offset"] = 0
                checkpoint_rows = [_public_record(row) for row in records.values() if _retained(row, at)]
                state["records"] = _reviewed_capacity_order(checkpoint_rows, review_doc, at)[:settings["max_records"]]
                if checkpoint is not None:
                    checkpoint(state)
            if scan["searches_succeeded"]:
                state["last_successful_scan_at"] = iso_time(at)
    rows = [_public_record(row) for row in records.values() if _retained(row, at)]
    capacity_reviews = review_doc if status not in {"unconfigured", "disabled"} else None
    rows = _reviewed_capacity_order(rows, capacity_reviews, at)
    omitted_records = max(0, len(rows) - settings["max_records"])
    rows = rows[:settings["max_records"]]
    semantic = {"ai_enabled": False, "llm_status": "disabled", "calls_used": 0,
                "cache_hits": 0, "assessed": 0, "errors": [], "budget_exhausted": False}
    if status not in {"unconfigured", "disabled"}:
        try:
            if enrich is None:
                from .semantic import enrich_semantics
                enrich = enrich_semantics
            rows, semantic = enrich(rows, settings, state, at, environ=env)
        except Exception:
            semantic = {**semantic, "llm_status": "error", "errors": ["Optionale semantische Sichtung fehlgeschlagen."]}
            _block(blockers, "semantic_failed", "Die optionale semantische Sichtung ist fehlgeschlagen.", "Fakten und manuelle Sichtung bleiben verfügbar; Modellkonfiguration prüfen.")
    else:
        state.pop("semantic_cache", None)
    interactive_review = {"status": "none", "reviewer": "ChatGPT", "reviewed_at": None,
                          "applied": 0, "available": 0, "unreviewed": len(rows)}
    if review_doc is not None and status not in {"unconfigured", "disabled"}:
        from .reviews import apply_reviews
        rows, review_meta = apply_reviews(rows, review_doc, at)
        interactive_review.update(review_meta)
        interactive_review["validation_status"] = review_meta.get("status", "invalid")
        interactive_review["status"] = {"ok": "applied", "partial": "applied", "absent": "none",
                                        "future": "invalid"}.get(review_meta.get("status"), review_meta.get("status", "invalid"))
        interactive_review["available"] = review_meta.get("total", 0)
    rows = sorted((_public_record(row) for row in rows if _retained(row, at)), key=_sort_key)
    state["records"], state["updated_at"] = rows[:settings["max_records"]], iso_time(at)
    rows = state["records"]
    # V1 can contain an authentic LLM review without an independent Useability
    # assessment. Neither that nor the two-dimensional heuristic is a complete
    # V2 LLM review, including when no interactive document was supplied.
    interactive_review["unreviewed"] = sum(
        not (row.get("assessment_basis") == "llm" and type(row.get("score_version")) is int
             and row["score_version"] == 2
             and all(type(row.get(key)) is int and 0 <= row[key] <= 100
                     for key in ("scalp_score", "useability_score")))
        for row in rows
    )
    leaves = state.get("taxonomy", {}).get("leaves", [])
    categories_total = scan["categories_total"] or (len(manual_ids) if manual_ids else len(leaves) if isinstance(leaves, list) else 0)
    counts = {name: sum(row.get("eligibility") == name for row in rows) for name in ("eligible", "review", "excluded")}
    active = sum(at < parse_time(row["ends_at"]) < at + timedelta(hours=settings["lookahead_hours"]) for row in rows)
    messages = {"unconfigured": "Noch keine freigegebene Live-Datenquelle eingerichtet.", "disabled": "Datenquelle ausgeschaltet.",
        "live": "Der geplante Abruf ist abgeschlossen; die Gesamtkategorie wird schrittweise durchsucht.",
        "partial": "Teilabruf: Abdeckung und Aktualität einzelner Angebote sind begrenzt.",
        "error": "Die Datenquelle konnte in diesem Lauf nicht erfolgreich abgerufen werden."}
    if source == "json" and status == "live":
        messages["live"] = "Lokaler JSON-Import abgeschlossen; keine erneute Liveprüfung bei eBay."
    if source == "aog":
        messages["live"] = "Öffentliche Angebotsübersicht abgerufen; Angaben stammen von auktionen-ohne-gebot.de, nicht aus einer eigenen eBay-Prüfung."
        messages["partial"] = "Öffentliche Quelle aktiv: begrenzter Ausschnitt mit zwischengespeicherten Angaben von auktionen-ohne-gebot.de."
    feed = {
        "schema_version": FEED_VERSION, "generated_at": iso_time(at), "last_successful_scan_at": state.get("last_successful_scan_at"),
        "source_status": status, "status_message": messages[status], "blockers": blockers, "source": provider, "provider": provider,
        "site_url": settings.get("site_url", "https://pogoner.github.io/fundgrube/"),
        "location": {"postcode": settings["postcode"], "city": settings.get("city", "Heidelberg"), "country": "DE", "pickup_radius_km": settings["pickup_radius_km"]},
        "criteria": {key: settings[key] for key in ("max_price_eur", "max_shipping_eur", "max_bids", "lookahead_hours", "description_max_age_hours", "refresh_after_minutes")},
        "stats": {**scan, "shortlisted": counts["eligible"] + counts["review"], "active": active,
                  "retained": len(rows), "outside_active_window": len(rows) - active, **counts,
                  "stale": sum(bool(row.get("stale")) for row in rows), "carried": sum(bool(row.get("carried")) for row in rows), "omitted_records": omitted_records},
        "coverage": {"categories_total": categories_total, "categories_scanned": scan["categories_scanned"], "categories_completed": scan["categories_completed"],
                     "categories_planned": scan["categories_planned"], "cycle_estimate_hours": (math.ceil(categories_total / settings["categories_per_run"]) * settings["schedule_interval_minutes"] / 60) if categories_total else None,
                     "cycle_estimate_is_lower_bound": True, "schedule_interval_minutes": settings["schedule_interval_minutes"], "browse_calls_this_run": calls_this_run,
                     "browse_calls_today": state["daily_budget"]["calls_used"], "daily_budget": settings["browse_daily_budget"], "complete": False,
                     "price_filter_applied": False, "cursor": dict(state["cursor"]), "manual_leaf_override": bool(manual_ids),
                     "pagination_caveat": "Ergebnisse verändern sich während des Abrufs; Seiten werden nur innerhalb eines Laufs fortgesetzt. Dichte Kategorien können unvollständig bleiben."},
        "ai_enabled": bool(semantic.get("ai_enabled")), "semantic": semantic, "ai_metadata": semantic,
        "interactive_review": interactive_review,
        "api_page_size": settings["api_page_size"], "deals": rows,
    }
    if source == "aog":
        feed["coverage"].update({
            **source_coverage, "complete": False, "source_claims_only": True,
            "source_url": "https://auktionen-ohne-gebot.de/business-industrie/",
            "cycle_estimate_hours": None, "manual_leaf_override": False,
            "browse_calls_this_run": 0, "browse_calls_today": 0, "daily_budget": None,
            "price_filter_applied": True,
            "pagination_caveat": "Begrenzter Ausschnitt aus einer veränderlichen, zwischengespeicherten Ergebnisliste. Die Trefferzahl der Quelle ist keine garantierte vollständige Abdeckung.",
        })
    json.dumps(feed, allow_nan=False)
    json.dumps(state, allow_nan=False)
    return feed, state


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise ConfigurationError("A required local JSON file does not exist.")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ConfigurationError("A local JSON file exceeds the supported size limit.")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, UnicodeError):
        raise ConfigurationError("A local JSON file contains malformed JSON.") from None


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".state-", suffix=".json", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Fundgrube's static GUI and assistant-readable feed")
    parser.add_argument("--source", choices=("auto", "disabled", "json", "ebay", "aog"), default=os.environ.get("HUNTER_SOURCE", "auto"))
    parser.add_argument("--file", type=Path, help="Local licensed JSON input; never downloaded from a URL")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "runtime")
    parser.add_argument("--site-dir", type=Path, default=ROOT / "site")
    parser.add_argument("--config", type=Path, default=SETTINGS_PATH)
    parser.add_argument("--reviews", type=Path, default=ROOT / "reviews" / "current.json",
                        help="Optional content-bound interactive ChatGPT reviews; never instructions or source facts")
    args = parser.parse_args(argv)
    if args.source == "json" and args.file is None:
        parser.error("--source json requires --file with a local JSON path")
    state_path = args.state_dir / "state.json"
    try:
        settings = _read_json(args.config)
        state = _read_json(state_path) if state_path.exists() else None
        raw_items = None
        if args.source == "json" and os.environ.get("HUNTER_PUBLISH_LIVE_DATA", "").lower() == "true":
            obj = _read_json(args.file)
            raw_items = obj if isinstance(obj, list) else obj.get("itemSummaries", obj.get("items")) if isinstance(obj, dict) else None
        review_doc = None
        if args.reviews.exists():
            from .reviews import load_reviews
            try:
                review_doc = load_reviews(args.reviews)
            except (OSError, ValueError, TypeError):
                review_doc = {"invalid_review_file": True}
        feed, next_state = run_pipeline(settings, source=args.source, state=state, raw_items=raw_items, review_doc=review_doc,
                                       checkpoint=lambda value: _atomic_json(state_path, value))
        _atomic_json(state_path, next_state)
        from .feed import write_feed
        write_feed(args.site_dir, feed)
    except (ConfigurationError, OSError, ValueError, TypeError, KeyError):
        print("Fundgrube build failed: invalid configuration, state, input, or output. No success was claimed.", file=sys.stderr)
        return 1
    print(f"Fundgrube: {feed['source_status']}; source={feed['provider']}; {len(feed['deals'])} active records; "
          f"{feed['coverage']['browse_calls_this_run']} Browse calls; {feed['coverage'].get('requests_this_run', 0)} public-source requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
