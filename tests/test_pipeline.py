"""Offline orchestration tests with fake API clients and temporary output paths."""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hunter.ebay import ApiError, BudgetExceeded, RateLimitError
from hunter.models import iso_time, normalize_item
from hunter.pipeline import ConfigurationError, main, new_state, prepare_state, run_pipeline, validate_settings
from hunter.ranking import apply_semantic, assess_item
from hunter.reviews import seal_reviews

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
ENV = {"HUNTER_PUBLISH_LIVE_DATA": "true", "EBAY_CLIENT_ID": "fake-id", "EBAY_CLIENT_SECRET": "fake-secret",
       "HUNTER_PRIVATE_SEARCH": json.dumps({"postcode": "99999", "profile_key": "11" * 32})}
SETTINGS = {"categories_per_run": 1, "browse_calls_per_run": 20, "detail_limit_per_run": 2}
ID = "v1|123456789012|0"
LEAVES = [{"id": "100", "name": "Labor"}, {"id": "200", "name": "Werkzeug"}]


def raw_item(item_id=ID, **overrides):
    result = {
        "itemId": item_id, "title": "Labor Oszilloskop", "description": "Gebrauchtes Gerät",
        "currentBidPrice": {"value": "1.00", "currency": "EUR"}, "bidCount": 0,
        "buyingOptions": ["AUCTION"], "itemEndDate": iso_time(NOW + timedelta(hours=12)),
        "itemLocation": {"city": "Teststadt", "postalCode": "99999", "country": "DE"},
        "shippingOptions": [{"shippingCost": {"value": "7.99", "currency": "EUR"}}],
        "itemWebUrl": "https://www.ebay.de/itm/123456789012",
    }
    result.update(overrides)
    return result


def page(items=None, *, more=False, offset=0, capped=False):
    return {"items": items or [], "total": 1000 if more else len(items or []), "has_more": more,
            "next_offset": offset + 200 if more else None, "offset": offset, "limit": 200,
            "capped": capped, "price_filter_applied": False}


def no_semantics(items, settings, state, at, **kwargs):
    return items, {"ai_enabled": False, "llm_status": "disabled", "calls_used": 0, "errors": []}


class FakeClient:
    def __init__(self, *, results=None, details=None, leaves=None, taxonomy_error=None, **kwargs):
        self.max_calls = kwargs["max_calls"]
        self.calls_used = 0
        self.arguments = kwargs
        self.queries = []
        self.detail_ids = []
        self.results = list(results or [])
        self.details = details or {}
        self.leaves = copy.deepcopy(LEAVES if leaves is None else leaves)
        self.taxonomy_error = taxonomy_error
        self.taxonomy_calls = 0

    def _call(self, result):
        cost, result = result if isinstance(result, tuple) else (1, result)
        if self.calls_used + cost > self.max_calls:
            self.calls_used = self.max_calls
            raise BudgetExceeded()
        self.calls_used += cost
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def leaf_categories(self, root):
        self.taxonomy_calls += 1
        if self.taxonomy_error:
            raise self.taxonomy_error
        return self.leaves

    def search(self, **kwargs):
        self.queries.append(kwargs)
        return self._call(self.results.pop(0) if self.results else page())

    def get_item(self, item_id):
        self.detail_ids.append(item_id)
        return self._call(self.details.get(item_id, raw_item(item_id)))


def factory(**options):
    clients = []

    def create(**kwargs):
        client = FakeClient(**options, **kwargs)
        clients.append(client)
        return client

    return create, clients


def run(settings=None, **kwargs):
    kwargs.setdefault("at", NOW)
    kwargs.setdefault("environ", ENV)
    kwargs.setdefault("enrich", no_semantics)
    if "client_factory" not in kwargs:
        kwargs["client_factory"] = factory()[0]
    return run_pipeline({**SETTINGS, **(settings or {})}, **kwargs)


def cached_state(age=timedelta(hours=2), *, raw=None):
    state = new_state(NOW)
    seen = NOW - age
    item = normalize_item(raw or raw_item(), seen)
    state["records"] = [assess_item(item, validate_settings(SETTINGS), seen)]
    return state


class TestConfiguration(unittest.TestCase):
    def test_no_publication_flag_is_unconfigured_without_any_calls(self):
        create, clients = factory()
        feed, state = run(environ={"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret"}, client_factory=create)
        self.assertEqual(feed["source_status"], "unconfigured")
        self.assertEqual(feed["deals"], [])
        self.assertEqual(state["records"], [])
        self.assertEqual(feed["blockers"][0]["code"], "publication_not_enabled")
        self.assertEqual(clients, [])

    def test_auto_without_credentials_is_unconfigured(self):
        feed, _ = run(environ={"HUNTER_PUBLISH_LIVE_DATA": "true"})
        self.assertEqual(feed["source_status"], "unconfigured")
        self.assertEqual(feed["blockers"][0]["code"], "ebay_credentials_missing")

    def test_disabled_purges_records_and_semantic_cache_not_budget(self):
        state = cached_state()
        state["daily_budget"]["calls_used"] = 123
        state["semantic_cache"] = {ID: {"assessment": {}}}
        feed, result = run(source="disabled", state=state)
        self.assertEqual(feed["source_status"], "disabled")
        self.assertEqual(result["records"], [])
        self.assertNotIn("semantic_cache", result)
        self.assertEqual(result["daily_budget"]["calls_used"], 123)

    def test_invalid_configuration_is_not_a_successful_empty_feed(self):
        for settings in ({"browse_daily_budget": 5001}, {"max_records": 0}, {"max_records": 5001}, {"max_bids": True},
                         {"max_freshness_hours": 24.01}, {"max_price_eur": float("nan")},
                         {"postcode": "99999,x:y"}, {"marketplace": "EBAY_US"}):
            with self.subTest(settings=settings), self.assertRaises(ConfigurationError):
                run(settings)

    def test_three_day_window_is_separate_from_description_ttl_and_other_limits(self):
        settings = validate_settings({})
        self.assertEqual(settings["description_max_age_hours"], 24)
        self.assertEqual(settings["lookahead_hours"], 72)
        self.assertNotIn("max_freshness_hours", settings)
        self.assertEqual(settings["max_records"], 5000)
        self.assertEqual(settings["refresh_after_minutes"], 90)
        self.assertEqual(settings["schedule_interval_minutes"], 30)
        self.assertEqual(settings["aog_max_requests_per_run"], 160)
        self.assertEqual(settings["aog_max_run_seconds"], 1500)
        self.assertEqual(validate_settings({"max_freshness_hours": 24})["description_max_age_hours"], 24)
        self.assertEqual(validate_settings({"max_freshness_hours": 0.5})["description_max_age_hours"], 0.5)
        self.assertEqual(validate_settings({"max_freshness_hours": 1, "description_max_age_hours": 12})["description_max_age_hours"], 12)

    def test_retention_capacity_is_reported_separately_from_full_scan_budgets(self):
        for capacity in (5000, 2):
            with self.subTest(capacity=capacity):
                feed, _ = run({"max_records": capacity}, source="json", raw_items=[])
                self.assertEqual(feed["coverage"]["retention_capacity"], capacity)
                self.assertFalse(feed["coverage"]["complete"])
        settings = validate_settings({"max_records": 5000})
        self.assertEqual(settings["aog_max_requests_per_run"], 160)
        self.assertEqual(settings["schedule_interval_minutes"], 30)
        self.assertEqual(settings["browse_daily_budget"], 4500)

    def test_pipeline_retains_5000_records_and_reports_the_next_omission(self):
        items = [raw_item(item_id=f"v1|{100000000000 + index}|0") for index in range(5001)]
        feed, state = run(source="json", raw_items=items)
        self.assertEqual(len(feed["deals"]), 5000)
        self.assertEqual(len(state["records"]), 5000)
        self.assertEqual(feed["coverage"]["retention_capacity"], 5000)
        self.assertEqual(feed["stats"]["omitted_records"], 1)
        self.assertFalse(feed["coverage"]["complete"])

    def test_description_age_limit_is_validated_separately(self):
        for value in (0, 24.01, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_settings({"description_max_age_hours": value})

    def test_invalid_state_does_not_reset_quota(self):
        for state in ({}, {"schema_version": 1}, {"schema_version": 2, "daily_budget": {}},
                      {**new_state(NOW), "daily_budget": {"date": "future", "calls_used": 10}}):
            with self.subTest(state=state), self.assertRaises(ConfigurationError):
                run(state=state)

    def test_future_budget_day_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            prepare_state(new_state(NOW + timedelta(days=1)), NOW)

    def test_next_utc_day_resets_daily_budget_only(self):
        state = new_state(NOW - timedelta(days=1))
        state["daily_budget"]["calls_used"] = 4500
        state["cursor"] = {"category_id": "100", "mode": "pickup", "offset": 200}
        result = prepare_state(state, NOW)
        self.assertEqual(result["daily_budget"], {"date": "2026-11-07", "calls_used": 0})
        self.assertEqual(result["cursor"], state["cursor"])

    def test_naive_run_time_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            run(at=NOW.replace(tzinfo=None))


class TestDiscovery(unittest.TestCase):
    def test_dual_modes_high_recall_and_unknown_reserve(self):
        create, clients = factory(results=[page([raw_item()]), page([raw_item()])])
        feed, state = run(client_factory=create)
        client = clients[0]
        self.assertEqual([q["local_pickup"] for q in client.queries], [False, True])
        self.assertTrue(all(q["max_price_eur"] is None for q in client.queries))
        self.assertEqual(len(feed["deals"]), 1)
        self.assertEqual(feed["deals"][0]["reserve_status"], "unknown")
        self.assertEqual(feed["deals"][0]["eligibility"], "review")
        self.assertTrue(feed["deals"][0]["local_pickup_confirmed"])
        self.assertTrue(feed["deals"][0]["details_fetched"])
        self.assertEqual(feed["source_status"], "live")
        self.assertFalse(feed["coverage"]["complete"])
        self.assertEqual(state["daily_budget"]["calls_used"], 3)
        self.assertEqual(state["cursor"], {"category_id": "200", "mode": "delivery", "offset": 0})

    def test_category_cursor_continues_across_runs(self):
        _, state = run()
        create, clients = factory()
        feed, result = run(state=state, client_factory=create, at=NOW + timedelta(minutes=30))
        self.assertEqual(clients[0].queries[0]["category_id"], "200")
        self.assertEqual(result["cursor"]["category_id"], "100")
        self.assertEqual(clients[0].taxonomy_calls, 0)
        self.assertEqual(feed["coverage"]["categories_total"], 2)

    def test_pagination_limit_advances_category_without_persisting_unstable_offset(self):
        create, _ = factory(results=[page(more=True), page(more=True, offset=200)])
        feed, state = run(client_factory=create)
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(state["cursor"], {"category_id": "200", "mode": "delivery", "offset": 0})
        self.assertEqual(feed["stats"]["pages_omitted"], 1)
        create, clients = factory()
        _, next_state = run(state=state, client_factory=create, at=NOW + timedelta(minutes=30))
        self.assertEqual(clients[0].queries[0]["offset"], 0)
        self.assertEqual(clients[0].queries[0]["category_id"], "200")
        self.assertEqual(next_state["cursor"]["category_id"], "100")

    def test_manual_leaf_ids_override_without_replacing_taxonomy_cache(self):
        state = new_state(NOW)
        state["taxonomy"] = {"marketplace": "EBAY_DE", "root_category_id": "12576", "fetched_at": iso_time(NOW), "leaves": LEAVES}
        create, clients = factory()
        feed, result = run(state=state, client_factory=create, environ={**ENV, "EBAY_LEAF_CATEGORY_IDS": "777,888,777"})
        self.assertEqual(clients[0].taxonomy_calls, 0)
        self.assertEqual(clients[0].queries[0]["category_id"], "777")
        self.assertTrue(feed["coverage"]["manual_leaf_override"])
        self.assertEqual(result["taxonomy"], state["taxonomy"])

    def test_malformed_manual_leaf_ids_fail_without_api_calls(self):
        for value in ("100,", "100,sellers:{x}", "100/../200", "١٠٠"):
            create, clients = factory()
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                run(client_factory=create, environ={**ENV, "EBAY_LEAF_CATEGORY_IDS": value})
            self.assertEqual(clients, [])

    def test_saved_old_page_offset_restarts_from_zero(self):
        state = new_state(NOW)
        state["cursor"] = {"category_id": "100", "mode": "pickup", "offset": 400}
        create, clients = factory()
        run(state=state, client_factory=create)
        self.assertEqual(clients[0].queries[0]["offset"], 0)
        self.assertTrue(clients[0].queries[0]["local_pickup"])

    def test_10000_result_cap_marks_partial(self):
        create, _ = factory(results=[page(capped=True), page()])
        feed, _ = run(client_factory=create)
        self.assertEqual(feed["source_status"], "partial")
        self.assertIn("ebay_result_cap", [row["code"] for row in feed["blockers"]])

    def test_failure_after_success_is_partial_not_live(self):
        create, _ = factory(results=[page([raw_item()]), ApiError("private upstream body", status_code=500)])
        feed, _ = run(client_factory=create)
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(feed["stats"]["searches_succeeded"], 1)
        self.assertEqual(feed["stats"]["searches_failed"], 1)
        self.assertNotIn("private upstream body", json.dumps(feed))

    def test_all_search_failures_error(self):
        create, _ = factory(results=[ApiError(), ApiError()])
        feed, _ = run(client_factory=create)
        self.assertEqual(feed["source_status"], "error")
        self.assertEqual(feed["deals"], [])
        self.assertIsNone(feed["last_successful_scan_at"])

    def test_access_denial_stops_run_and_preserves_cursor(self):
        create, clients = factory(results=[ApiError(status_code=403)])
        feed, state = run(client_factory=create)
        self.assertEqual(feed["source_status"], "error")
        self.assertEqual(len(clients[0].queries), 1)
        self.assertEqual(state["cursor"]["mode"], "delivery")
        self.assertEqual(feed["blockers"][0]["code"], "ebay_access_denied")

    def test_taxonomy_failure_is_expected_error_feed(self):
        create, _ = factory(taxonomy_error=ApiError(status_code=403))
        feed, state = run(client_factory=create)
        self.assertEqual(feed["source_status"], "error")
        self.assertEqual(state["daily_budget"]["calls_used"], 0)

    def test_budget_includes_failed_retry_attempts(self):
        create, _ = factory(results=[(3, ApiError(status_code=500)), page()])
        feed, state = run(client_factory=create)
        self.assertEqual(state["daily_budget"]["calls_used"], 4)
        self.assertEqual(feed["coverage"]["browse_calls_this_run"], 4)

    def test_daily_remaining_caps_new_client(self):
        state = new_state(NOW)
        state["daily_budget"]["calls_used"] = 4499
        create, clients = factory()
        feed, result = run(state=state, client_factory=create)
        self.assertEqual(clients[0].max_calls, 1)
        self.assertEqual(result["daily_budget"]["calls_used"], 4500)
        self.assertEqual(feed["source_status"], "partial")

    def test_exhausted_daily_budget_never_constructs_client(self):
        state = new_state(NOW)
        state["daily_budget"]["calls_used"] = 4500
        create, clients = factory()
        feed, _ = run(state=state, client_factory=create)
        self.assertEqual(clients, [])
        self.assertEqual(feed["source_status"], "error")

    def test_rate_limit_stops_and_counts_failed_call(self):
        create, clients = factory(results=[RateLimitError(retry_after=3600)])
        feed, state = run(client_factory=create)
        self.assertEqual(feed["source_status"], "error")
        self.assertEqual(state["daily_budget"]["calls_used"], 1)
        self.assertEqual(len(clients[0].queries), 1)

    def test_ledger_checkpoint_runs_even_after_programming_exception(self):
        create, _ = factory(results=[RuntimeError("programming error")])
        checkpoints = []
        with self.assertRaises(RuntimeError):
            run(client_factory=create, checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["daily_budget"]["calls_used"], 1)

    def test_known_uninteresting_and_expensive_items_do_not_get_details(self):
        media = raw_item("v1|2|0", title="BMW Zeitschriften", bidCount=3)
        expensive = raw_item("v1|3|0", currentBidPrice={"value": "100", "currency": "EUR"})
        create, clients = factory(results=[page([media, expensive]), page()])
        feed, _ = run(client_factory=create)
        self.assertEqual(clients[0].detail_ids, [])
        self.assertEqual(feed["stats"]["excluded"], 2)

    def test_mixed_auction_bin_with_unknown_price_gets_details(self):
        raw = raw_item(currentBidPrice=None, price={"value": "500", "currency": "EUR"}, buyingOptions=["AUCTION", "FIXED_PRICE"])
        detail = raw_item(currentBidPrice=None, minimumPriceToBid={"value": "1", "currency": "EUR"})
        create, clients = factory(results=[page([raw]), page()], details={ID: detail})
        feed, _ = run(client_factory=create)
        self.assertEqual(clients[0].detail_ids, [ID])
        self.assertEqual(feed["deals"][0]["price_eur"], 1)
        self.assertEqual(feed["deals"][0]["price_kind"], "minimum_bid")

    def test_missing_bid_and_format_fields_get_detail_confirmation(self):
        summary = raw_item(bidCount=None, buyingOptions=[])
        create, clients = factory(results=[page([summary]), page()], details={ID: raw_item()})
        feed, _ = run(client_factory=create)
        self.assertEqual(clients[0].detail_ids, [ID])
        self.assertEqual(feed["deals"][0]["bid_count"], 0)
        self.assertTrue(feed["deals"][0]["is_auction"])


class TestRefreshAndState(unittest.TestCase):
    def test_due_record_is_refreshed_before_discovery(self):
        create, clients = factory(details={ID: raw_item(bidCount=2)})
        feed, state = run(state=cached_state(), client_factory=create)
        self.assertEqual(clients[0].detail_ids, [ID])
        self.assertEqual(feed["stats"]["refreshed"], 1)
        self.assertEqual(state["records"][0]["bid_count"], 2)
        self.assertEqual(state["records"][0]["eligibility"], "excluded")
        self.assertEqual(state["records"][0]["last_seen_at"], iso_time(NOW))

    def test_ending_soon_refreshes_before_normal_interval(self):
        state = cached_state(timedelta(minutes=30), raw=raw_item(itemEndDate=iso_time(NOW + timedelta(hours=2))))
        create, clients = factory()
        run(state=state, client_factory=create)
        self.assertEqual(clients[0].detail_ids, [ID])

    def test_undue_record_retains_observation_timestamp(self):
        state = cached_state(timedelta(minutes=10))
        old_seen = state["records"][0]["last_seen_at"]
        create, clients = factory()
        feed, result = run(state=state, client_factory=create)
        self.assertEqual(clients[0].detail_ids, [])
        self.assertEqual(result["records"][0]["last_seen_at"], old_seen)
        self.assertEqual(feed["stats"]["carried"], 1)

    def test_expired_future_or_unknown_records_removed_without_refresh(self):
        state = new_state(NOW)
        expired = normalize_item(raw_item("v1|2|0", itemEndDate=iso_time(NOW - timedelta(seconds=1))), NOW)
        future = normalize_item(raw_item("v1|3|0"), NOW + timedelta(minutes=1))
        unknown = normalize_item(raw_item("v1|4|0"), NOW)
        unknown["last_seen_at"] = None
        state["records"].extend([expired, future, unknown])
        create, clients = factory()
        feed, result = run(state=state, client_factory=create)
        self.assertEqual(feed["deals"], [])
        self.assertEqual(result["records"], [])
        self.assertEqual(clients[0].detail_ids, [])

    def test_old_cache_survives_failed_refresh_until_auction_end_without_new_timestamp(self):
        for age in (timedelta(hours=7), timedelta(hours=23), timedelta(hours=24), timedelta(days=4), timedelta(days=9)):
            with self.subTest(age=age):
                state = cached_state(age)
                previous_seen = state["records"][0]["last_seen_at"]
                previous_content = state["records"][0]["content_updated_at"]
                create, clients = factory(details={ID: ApiError(status_code=500)})
                feed, result = run(state=state, client_factory=create)
                self.assertEqual(clients[0].detail_ids, [ID])
                self.assertEqual(feed["source_status"], "partial")
                row = result["records"][0]
                self.assertEqual(row["last_seen_at"], previous_seen)
                self.assertEqual(row["content_updated_at"], previous_content)
                self.assertTrue(row["stale"])
                self.assertTrue(row["carried"])
                self.assertEqual(row["eligibility"], "review")
                self.assertTrue(any("älter als zwei Stunden" in risk for risk in row["risks"]))
                if age >= timedelta(hours=24):
                    self.assertEqual(row["description"], "")

    def test_auction_end_is_exclusive_regardless_observation_age(self):
        for age, end in ((timedelta(hours=7), NOW), (timedelta(hours=23), NOW - timedelta(seconds=1)),
                         (timedelta(days=5), NOW), (timedelta(days=5), NOW - timedelta(seconds=1))):
            with self.subTest(age=age, end=end):
                state = cached_state(age, raw=raw_item(itemEndDate=iso_time(end)))
                create, clients = factory()
                feed, result = run(state=state, client_factory=create)
                self.assertEqual(feed["deals"], [])
                self.assertEqual(result["records"], [])
                self.assertEqual(clients[0].detail_ids, [])

    def test_legacy_source_age_limit_only_affects_description_not_record_lifetime(self):
        state = cached_state(timedelta(days=5))
        old_seen = state["records"][0]["last_seen_at"]
        create, _ = factory(details={ID: ApiError(status_code=500)})
        feed, _ = run({"max_freshness_hours": 0.5}, state=state, client_factory=create)
        self.assertEqual(feed["criteria"]["description_max_age_hours"], 0.5)
        self.assertNotIn("max_freshness_hours", feed["criteria"])
        self.assertEqual(feed["deals"][0]["last_seen_at"], old_seen)
        self.assertEqual(feed["deals"][0]["description"], "")

    def test_retained_record_enters_strict_active_window_without_source_refresh(self):
        end = iso_time(NOW + timedelta(hours=72))
        state = cached_state(timedelta(days=5), raw=raw_item(itemEndDate=end))
        state["records"][0].update(eligibility="excluded", eligibility_reasons=["Quellenstand hat das zulässige Aufbewahrungsfenster erreicht"])
        create, clients = factory()
        feed, state = run({"detail_limit_per_run": 0}, state=state, client_factory=create)
        self.assertEqual(len(feed["deals"]), 1)
        self.assertEqual(feed["deals"][0]["eligibility"], "excluded")
        self.assertEqual(feed["deals"][0]["eligibility_without_time_window"], "review")
        self.assertEqual(feed["stats"]["retained"], 1)
        self.assertEqual(feed["stats"]["active"], 0)
        self.assertEqual(feed["stats"]["outside_active_window"], 1)
        observed = feed["deals"][0]["last_seen_at"]
        later, state = run({"detail_limit_per_run": 0}, state=state, client_factory=create, at=NOW + timedelta(seconds=1))
        self.assertEqual(later["deals"][0]["eligibility"], "review")
        self.assertEqual(later["deals"][0]["eligibility_without_time_window"], "review")
        self.assertEqual(later["deals"][0]["last_seen_at"], observed)
        self.assertEqual(later["stats"]["active"], 1)
        self.assertEqual(later["stats"]["outside_active_window"], 0)
        self.assertEqual([item for client in clients for item in client.detail_ids], [])
        self.assertTrue(all(query["lookahead_hours"] == 72 for client in clients for query in client.queries))

    def test_failed_refresh_carries_fresh_old_data_with_stale_marker(self):
        state = cached_state()
        old_seen = state["records"][0]["last_seen_at"]
        create, _ = factory(details={ID: ApiError(status_code=500)})
        feed, result = run(state=state, client_factory=create)
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(result["records"][0]["last_seen_at"], old_seen)
        self.assertTrue(result["records"][0]["stale"])

    def test_404_details_removes_previously_cached_item(self):
        create, _ = factory(details={ID: ApiError(status_code=404)})
        feed, state = run(state=cached_state(), client_factory=create)
        self.assertEqual(feed["deals"], [])
        self.assertEqual(state["records"], [])

    def test_direct_detail_refresh_does_not_renew_old_pickup_proof(self):
        state = cached_state()
        state["records"][0].update(local_pickup_confirmed=True, pickup_verified_at=state["records"][0]["last_seen_at"])
        feed, _ = run(state=state)
        self.assertFalse(feed["deals"][0]["local_pickup_confirmed"])

    def test_raw_and_seller_identity_never_persisted(self):
        raw = raw_item(seller={"username": "private-seller", "contact": "secret"}, mystery="do not persist")
        create, _ = factory(results=[page([raw]), page()], details={ID: raw})
        feed, state = run(client_factory=create)
        serialized = json.dumps({"feed": feed, "state": state})
        self.assertNotIn("private-seller", serialized)
        self.assertNotIn("do not persist", serialized)
        self.assertNotIn('"seller"', serialized)

    def test_deterministic_dedup_and_max_records(self):
        raws = [raw_item(f"v1|{number}|0") for number in (9, 3, 1)]
        feed, state = run({"max_records": 2}, source="json", raw_items=raws + [raws[0]])
        self.assertEqual([row["id"] for row in feed["deals"]], ["v1|1|0", "v1|3|0"])
        self.assertEqual(len(state["records"]), 2)
        self.assertEqual(feed["stats"]["omitted_records"], 1)

    def test_unknown_price_bids_are_never_filled_from_old_record(self):
        state = cached_state(timedelta(minutes=5))
        raw = raw_item(currentBidPrice=None, bidCount=None)
        create, _ = factory(results=[page([raw]), page()])
        feed, _ = run({"detail_limit_per_run": 0}, state=state, client_factory=create)
        self.assertIsNone(feed["deals"][0]["price_eur"])
        self.assertIsNone(feed["deals"][0]["bid_count"])

    def test_old_full_description_is_not_rejuvenated_by_fresh_summary(self):
        state = cached_state(timedelta(minutes=5))
        state["records"][0].update(description="Old full description", details_fetched=True,
                                   content_updated_at=iso_time(NOW - timedelta(hours=24)))
        create, _ = factory(results=[page([raw_item(description="", shortDescription="Fresh short text")]), page()])
        feed, _ = run({"detail_limit_per_run": 0}, state=state, client_factory=create)
        self.assertEqual(feed["deals"][0]["description"], "Fresh short text")
        self.assertEqual(feed["deals"][0]["content_updated_at"], iso_time(NOW))

    def test_stale_carried_description_removed_before_semantic_processing(self):
        state = cached_state(timedelta(minutes=5))
        state["records"][0].update(description="Old full description", content_updated_at=iso_time(NOW - timedelta(hours=24)))
        observed = []
        def enrich(items, *args, **kwargs):
            observed.extend(copy.deepcopy(items))
            return no_semantics(items, *args, **kwargs)
        run(state=state, enrich=enrich)
        self.assertEqual(observed[0]["description"], "")

    def test_retained_full_description_keeps_its_own_age_when_summary_is_refreshed(self):
        for age in (timedelta(hours=7), timedelta(hours=23)):
            with self.subTest(age=age):
                state = cached_state(timedelta(minutes=5))
                content_time = iso_time(NOW - age)
                state["records"][0].update(description="Retained full description", details_fetched=True,
                                           content_updated_at=content_time)
                create, _ = factory(results=[page([raw_item(description="", shortDescription="Fresh short text")]), page()])
                feed, _ = run({"detail_limit_per_run": 0}, state=state, client_factory=create)
                row = feed["deals"][0]
                self.assertEqual(row["description"], "Retained full description")
                self.assertEqual(row["content_updated_at"], content_time)
                self.assertEqual(row["last_seen_at"], iso_time(NOW))


class TestReviewedCapacity(unittest.TestCase):
    """A low keyword match must not evict a still-valid genuine review."""

    valuable_id = "v1|700000000001|0"
    competitor_id = "v1|700000000002|0"
    incoming_id = "v1|700000000003|0"

    def fixture(self):
        seen = NOW - timedelta(minutes=15)
        settings = validate_settings(SETTINGS)
        valuable = assess_item(normalize_item(raw_item(self.valuable_id, title="Analysegerät Modell Z9"), seen), settings, NOW)
        competitor = assess_item(normalize_item(raw_item(self.competitor_id, title="Autoklav"), seen), settings, NOW)
        answer = {
            "scalp_score": 99, "useability_score": 100, "useability_reason": "Eigenständige Apparatur.",
            "dependencies": [], "value_band": "around_500", "assessment_confidence": "medium",
            "product_kind": "apparatus", "value_reason": "Begründete hohe Priorität aus gelesenen Angaben.",
            "reasons": [], "risks": [], "next_checks": [],
        }
        reviewed_at = iso_time(NOW - timedelta(minutes=5))
        snapshot = "sha256:" + "a" * 64
        review = seal_reviews(
            {"snapshot_id": snapshot, "generated_at": iso_time(seen), "deals": [valuable, competitor]},
            {"schema_version": 2, "source_snapshot_id": snapshot, "reviewed_at": reviewed_at,
             "reviewer": "ChatGPT", "groups": [{"ids": [self.valuable_id], "assessment": answer}]},
        )
        valuable = apply_semantic(valuable, answer)
        valuable.update(semantic_review_mode="interactive", semantic_assessed_at=reviewed_at)
        state = new_state(NOW)
        state["records"] = [valuable, competitor]
        return state, review, raw_item(self.incoming_id, title="Labor Oszilloskop")

    def test_valid_review_is_restored_before_cap_and_optional_enrichment(self):
        state, review, incoming = self.fixture()
        original = copy.deepcopy(state)
        selected = []

        def enrich(items, *args, **kwargs):
            selected.extend(copy.deepcopy(items))
            return no_semantics(items, *args, **kwargs)

        feed, result = run({"max_records": 2}, source="json", raw_items=[incoming],
                           state=state, review_doc=review, enrich=enrich)
        self.assertEqual(state, original)
        self.assertEqual([row["id"] for row in selected], [self.valuable_id, self.incoming_id])
        self.assertEqual(selected[0]["interest_score"], 99)
        self.assertEqual(selected[0]["assessment_basis"], "llm")
        self.assertEqual([row["id"] for row in result["records"]], [self.valuable_id, self.incoming_id])
        self.assertEqual(result["records"][0]["last_seen_at"], original["records"][0]["last_seen_at"])
        self.assertEqual(feed["stats"]["omitted_records"], 1)
        self.assertEqual(feed["interactive_review"]["applied"], 1)
        self.assertEqual(feed["interactive_review"]["unreviewed"], 1)
        self.assertEqual(feed["semantic"]["calls_used"], 0)
        self.assertEqual(feed["coverage"]["browse_calls_this_run"], 0)

    def test_old_unchanged_review_keeps_score_and_original_date_before_capacity(self):
        state, review, incoming = self.fixture()
        reviewed_at = iso_time(NOW - timedelta(days=4))
        review["reviewed_at"] = reviewed_at
        feed, result = run({"max_records": 2}, source="json", raw_items=[incoming],
                           state=state, review_doc=review)
        row = next(row for row in result["records"] if row["id"] == self.valuable_id)
        self.assertEqual(row["interest_score"], 99)
        self.assertEqual(row["semantic_assessed_at"], reviewed_at)
        self.assertEqual(row["last_seen_at"], state["records"][0]["last_seen_at"])
        self.assertEqual(feed["interactive_review"]["applied"], 1)
        self.assertIsNone(feed["interactive_review"]["expires_at"])

    def test_future_changed_invalid_or_text_expired_reviews_do_not_win_capacity(self):
        for mutation in ("future", "changed", "invalid", "text_expired"):
            with self.subTest(mutation=mutation):
                state, review, incoming = self.fixture()
                if mutation == "future":
                    review["reviewed_at"] = iso_time(NOW + timedelta(seconds=1))
                elif mutation == "changed":
                    state["records"][0]["description"] = "Inhalt hat sich geändert."
                elif mutation == "invalid":
                    review["reviews"][0]["assessment"]["useability_score"] = True
                else:
                    state["records"][0]["content_updated_at"] = iso_time(NOW - timedelta(hours=24))
                feed, result = run({"max_records": 2}, source="json", raw_items=[incoming],
                                   state=state, review_doc=review)
                self.assertNotIn(self.valuable_id, [row["id"] for row in result["records"]])
                self.assertEqual(len(result["records"]), 2)
                self.assertTrue(all(row["assessment_basis"] == "heuristic" for row in result["records"]))
                self.assertEqual(feed["interactive_review"]["applied"], 0)

    def test_review_priority_cannot_override_price_bids_or_transport_exclusions(self):
        for field, value in (("price_eur", 100), ("bid_count", 2), ("shipping_eur", 100)):
            for capacity in (2, 3):
                with self.subTest(field=field, capacity=capacity):
                    state, review, incoming = self.fixture()
                    state["records"][0][field] = value
                    feed, _ = run({"max_records": capacity}, source="json", raw_items=[incoming],
                                  state=state, review_doc=review)
                    kept = {row["id"]: row for row in feed["deals"]}
                    if capacity == 2:
                        self.assertNotIn(self.valuable_id, kept)
                    else:
                        row = kept[self.valuable_id]
                        self.assertEqual(row["interest_score"], 99)
                        self.assertEqual(row["eligibility"], "excluded")
                        self.assertEqual(row["eligibility_without_time_window"], "excluded")
                        self.assertEqual(row[field], value)

    def test_newer_api_assessment_still_wins_after_preselection(self):
        state, review, incoming = self.fixture()
        answer = {**review["reviews"][0]["assessment"], "scalp_score": 10}

        def enrich(items, *args, **kwargs):
            output = copy.deepcopy(items)
            target = next(index for index, row in enumerate(output) if row["id"] == self.valuable_id)
            output[target] = apply_semantic(output[target], answer)
            output[target].pop("semantic_review_mode", None)
            output[target]["semantic_assessed_at"] = iso_time(NOW)
            return output, {"ai_enabled": True, "llm_status": "ok", "calls_used": 0}

        feed, _ = run({"max_records": 2}, source="json", raw_items=[incoming],
                      state=state, review_doc=review, enrich=enrich)
        row = next(row for row in feed["deals"] if row["id"] == self.valuable_id)
        self.assertEqual(row["interest_score"], 10)
        self.assertNotIn("semantic_review_mode", row)
        self.assertEqual(feed["interactive_review"]["applied"], 0)
        self.assertEqual(feed["interactive_review"]["skipped_newer"], 1)

    def test_budget_checkpoint_uses_same_review_aware_capacity_order(self):
        state, review, incoming = self.fixture()
        checkpoints = []
        create, clients = factory(results=[page([incoming]), page()])
        feed, _ = run({"max_records": 2, "detail_limit_per_run": 0}, state=state,
                      review_doc=review, client_factory=create,
                      checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertTrue(checkpoints)
        self.assertEqual([row["id"] for row in checkpoints[-1]["records"]], [self.valuable_id, self.incoming_id])
        self.assertEqual(checkpoints[-1]["records"][0]["interest_score"], 99)
        self.assertEqual(len(clients[0].queries), 2)
        self.assertEqual(clients[0].detail_ids, [])
        self.assertEqual(feed["semantic"]["calls_used"], 0)


class TestSemanticAndJson(unittest.TestCase):
    def test_dual_scores_are_persisted_and_api_review_counts_without_interactive_document(self):
        answer = {"scalp_score": 85, "useability_score": 90, "useability_reason": "Eigenständiges Messgerät.",
                  "dependencies": ["Übliche Netzversorgung"], "value_band": "around_500", "assessment_confidence": "low",
                  "product_kind": "apparatus", "value_reason": "Plausibles Spezialgerät.", "reasons": [], "risks": [], "next_checks": []}

        def enrich(items, settings, state, at, **kwargs):
            return [apply_semantic(row, answer) for row in items], {"ai_enabled": True, "llm_status": "ok"}

        feed, state = run(source="json", raw_items=[raw_item()], enrich=enrich)
        self.assertEqual(feed["schema_version"], 3)
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(feed["interactive_review"]["unreviewed"], 0)
        self.assertEqual(feed["interactive_review"]["status"], "none")
        self.assertEqual(feed["deals"], state["records"])
        row = feed["deals"][0]
        for key in ("scalp_score", "useability_score", "useability_reason", "dependencies"):
            self.assertEqual(row[key], answer[key])
        self.assertEqual(row["score_version"], 2)
        self.assertEqual(row["score"], 77)
        self.assertEqual(row["interest_score"], 77)
        self.assertEqual(row["price_eur"], 1)
        self.assertEqual(row["last_seen_at"], iso_time(NOW))

    def test_unreviewed_counts_dual_pending_and_heuristics_not_just_llm_basis(self):
        def enrich(items, settings, state, at, **kwargs):
            output = [dict(row) for row in items]
            output[0].update(assessment_basis="llm", score_version=2, scalp_score=85, useability_score=90)
            output[1].update(assessment_basis="llm", score_version=1, scalp_score=85, useability_score=None, interest_score=None, score=None)
            output[2].update(assessment_basis="llm", score_version=2, scalp_score=85, useability_score=None, interest_score=None, score=None)
            output[3].update(assessment_basis="heuristic", score_version=2, scalp_score=85, useability_score=90)
            return output, {"ai_enabled": True, "llm_status": "partial"}

        source = [raw_item(f"v1|12345678901{index}|0") for index in range(4)]
        feed, _ = run(source="json", raw_items=source, enrich=enrich)
        self.assertEqual(feed["interactive_review"]["unreviewed"], 3)
        self.assertEqual(sum(row["assessment_basis"] == "llm" for row in feed["deals"]), 3)

    def test_optional_semantics_failure_preserves_factual_feed(self):
        def fail(*args, **kwargs):
            raise RuntimeError("secret api-key")
        feed, state = run(source="json", raw_items=[raw_item()], enrich=fail)
        self.assertEqual(len(feed["deals"]), 1)
        self.assertEqual(feed["semantic"]["llm_status"], "error")
        self.assertNotIn("secret api-key", json.dumps(feed))
        self.assertEqual(state["records"][0]["price_eur"], 1)

    def test_semantic_budget_and_cache_updates_are_preserved(self):
        def enrich(items, settings, state, at, **kwargs):
            state["semantic_budget"] = {"date": "2026-11-07", "requests": 1, "tokens": 200, "reserved_tokens": 0}
            state["semantic_cache"] = {ID: {"assessment": {"value_band": "unknown"}}}
            return items, {"ai_enabled": True, "llm_status": "ok"}
        feed, state = run(source="json", raw_items=[raw_item()], enrich=enrich)
        self.assertTrue(feed["ai_enabled"])
        self.assertEqual(state["semantic_budget"]["requests"], 1)
        self.assertIn(ID, state["semantic_cache"])

    def test_json_is_publication_gated(self):
        feed, _ = run(source="json", raw_items=[raw_item()], environ={})
        self.assertEqual(feed["source_status"], "unconfigured")
        self.assertEqual(feed["deals"], [])

    def test_malformed_json_collection_rejected(self):
        for raw in ({"item": "not list"}, ["not object"], None):
            with self.subTest(raw=raw), self.assertRaises(ConfigurationError):
                run(source="json", raw_items=raw)

    def test_real_disabled_semantic_integration_uses_no_external_services(self):
        feed, _ = run_pipeline(SETTINGS, source="json", raw_items=[raw_item()], environ=ENV, at=NOW)
        self.assertFalse(feed["ai_enabled"])
        self.assertEqual(feed["semantic"]["llm_status"], "disabled")
        self.assertEqual(feed["interactive_review"]["unreviewed"], 1)


class TestCommandLine(unittest.TestCase):
    def test_disabled_cli_writes_state_and_required_feed_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            code = main(["--source", "disabled", "--state-dir", str(root / "runtime"), "--site-dir", str(root / "site")])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads((root / "runtime/state.json").read_text())["schema_version"], 2)
            self.assertEqual(json.loads((root / "site/data/deals.json").read_text())["source_status"], "disabled")
            self.assertTrue((root / "site/api/manifest.json").is_file())

    def test_invalid_settings_cli_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text('{"max_freshness_hours":25}')
            self.assertEqual(main(["--config", str(config), "--state-dir", str(root / "runtime"), "--site-dir", str(root / "site")]), 1)
            self.assertFalse((root / "site/api/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
