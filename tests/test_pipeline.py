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
from hunter.ranking import assess_item

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
ENV = {"HUNTER_PUBLISH_LIVE_DATA": "true", "EBAY_CLIENT_ID": "fake-id", "EBAY_CLIENT_SECRET": "fake-secret"}
SETTINGS = {"categories_per_run": 1, "browse_calls_per_run": 20, "detail_limit_per_run": 2}
ID = "v1|123456789012|0"
LEAVES = [{"id": "100", "name": "Labor"}, {"id": "200", "name": "Werkzeug"}]


def raw_item(item_id=ID, **overrides):
    result = {
        "itemId": item_id, "title": "Labor Oszilloskop", "description": "Gebrauchtes Gerät",
        "currentBidPrice": {"value": "1.00", "currency": "EUR"}, "bidCount": 0,
        "buyingOptions": ["AUCTION"], "itemEndDate": iso_time(NOW + timedelta(hours=12)),
        "itemLocation": {"city": "Heidelberg", "postalCode": "69121", "country": "DE"},
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
        for settings in ({"browse_daily_budget": 5001}, {"max_records": 0}, {"max_bids": True},
                         {"max_freshness_hours": 24}, {"max_price_eur": float("nan")},
                         {"postcode": "69121,x:y"}, {"marketplace": "EBAY_US"}):
            with self.subTest(settings=settings), self.assertRaises(ConfigurationError):
                run(settings)

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

    def test_old_expired_or_future_records_removed_without_refresh(self):
        state = cached_state(timedelta(hours=7))
        expired = normalize_item(raw_item("v1|2|0", itemEndDate=iso_time(NOW - timedelta(seconds=1))), NOW)
        future = normalize_item(raw_item("v1|3|0"), NOW + timedelta(minutes=1))
        state["records"].extend([expired, future])
        create, clients = factory()
        feed, result = run(state=state, client_factory=create)
        self.assertEqual(feed["deals"], [])
        self.assertEqual(result["records"], [])
        self.assertEqual(clients[0].detail_ids, [])

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
                                   content_updated_at=iso_time(NOW - timedelta(hours=7)))
        create, _ = factory(results=[page([raw_item(description="", shortDescription="Fresh short text")]), page()])
        feed, _ = run({"detail_limit_per_run": 0}, state=state, client_factory=create)
        self.assertEqual(feed["deals"][0]["description"], "Fresh short text")
        self.assertEqual(feed["deals"][0]["content_updated_at"], iso_time(NOW))

    def test_stale_carried_description_removed_before_semantic_processing(self):
        state = cached_state(timedelta(minutes=5))
        state["records"][0].update(description="Old full description", content_updated_at=iso_time(NOW - timedelta(hours=7)))
        observed = []
        def enrich(items, *args, **kwargs):
            observed.extend(copy.deepcopy(items))
            return no_semantics(items, *args, **kwargs)
        run(state=state, enrich=enrich)
        self.assertEqual(observed[0]["description"], "")


class TestSemanticAndJson(unittest.TestCase):
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
            config.write_text('{"max_freshness_hours":24}')
            self.assertEqual(main(["--config", str(config), "--state-dir", str(root / "runtime"), "--site-dir", str(root / "site")]), 1)
            self.assertFalse((root / "site/api/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
