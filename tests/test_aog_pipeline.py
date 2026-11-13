"""Public-source integration tests: no network and no eBay credentials."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from hunter.aog import HttpResponse, collect
from hunter.feed import write_feed
from hunter.models import iso_time, normalize_item, parse_time
from hunter.pipeline import ConfigurationError, new_state, run_pipeline
from hunter.reviews import content_fingerprint

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
OBSERVED = NOW - timedelta(minutes=15)
ENV = {"HUNTER_PUBLISH_LIVE_DATA": "true"}
SETTINGS = {"postcode": "69121", "max_price_eur": 1, "pickup_radius_km": 50}
SOURCE = "https://auktionen-ohne-gebot.de/business-industrie/"


def item(**changes):
    result = {
        "id": "v1|123456789012|0", "title": "Tisch-Autoklav voll funktionsfähig",
        "url": "https://www.ebay.de/itm/123456789012", "price_eur": 1,
        "price_kind": "auction_price", "shipping_eur": None, "bid_count": 0,
        "ends_at": iso_time(NOW + timedelta(hours=4)), "condition": "Gebraucht",
        "is_auction": True, "source_provider": "auktionen-ohne-gebot",
        "source_url": SOURCE, "source_observed_at": iso_time(OBSERVED),
        "verification_level": "aggregator", "pickup_available": True,
        "pickup_search_confirmed": True, "pickup_search_postcode": "69121",
        "pickup_search_radius_km": 50,
    }
    result.update(changes)
    return result


def response(items=None, *, status="partial", pages=2, **changes):
    result = {
        "items": [item()] if items is None else items,
        "status": status, "source_state": {"next_allowed_at": iso_time(NOW + timedelta(minutes=30))},
        "observed_at": iso_time(OBSERVED), "warnings": [],
        "coverage": {"requests_this_run": pages + 1, "pages_fetched": pages,
                     "page_size": 48, "source_total_reported": 1200,
                     "source_cache_max_age_minutes": 15, "complete": False},
    }
    result.update(changes)
    return result


def full_response(items, *, at=NOW + timedelta(minutes=30), pickup_enabled=True, observed_at=None):
    cutoff = observed_at or min([at - timedelta(minutes=15)] + [observed for row in items
                               if (observed := parse_time(row.get("source_observed_at"))) is not None])
    result = response(items, status="live", pages=2 if pickup_enabled else 1,
                      finished_at=iso_time(at + timedelta(seconds=20)),
                      observed_at=iso_time(cutoff))
    result["coverage"].update(
        traversal_completed=True, reported_nonempty_but_no_records=False,
        scan_started_at=iso_time(at), scan_completed_at=result["finished_at"],
        unique_records=len({row.get("id") for row in items}),
        searches={name: {"enabled": enabled, "status": "completed" if enabled else "disabled",
                         "traversal_completed": enabled, "stop_reason": "reported_end" if enabled else "disabled",
                         "pages_fetched": int(enabled), "reported_nonempty_but_no_records": False}
                  for name, enabled in (("general", True), ("pickup", pickup_enabled))})
    return result


def run(result=None, **kwargs):
    kwargs.setdefault("source", "aog")
    kwargs.setdefault("at", NOW)
    kwargs.setdefault("environ", ENV)
    kwargs.setdefault("aog_collect", lambda settings, at, state: copy.deepcopy(result or response()))
    kwargs.setdefault("enrich", lambda rows, settings, state, at, **kw: (rows, {"ai_enabled": False, "llm_status": "disabled"}))
    kwargs.setdefault("client_factory", lambda **kw: (_ for _ in ()).throw(AssertionError("eBay API must not be called")))
    return run_pipeline(SETTINGS, **kwargs)


class PublicSourcePipelineTests(unittest.TestCase):
    def test_no_ebay_credentials_required(self):
        feed, state = run()
        self.assertEqual(feed["provider"], "auktionen-ohne-gebot")
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(len(feed["deals"]), 1)
        self.assertEqual(feed["coverage"]["browse_calls_this_run"], 0)
        self.assertEqual(state["daily_budget"]["calls_used"], 0)
        self.assertEqual(feed["coverage"]["source_total_reported"], 1200)
        self.assertIsNone(feed["coverage"]["cycle_estimate_hours"])
        self.assertFalse(feed["coverage"]["complete"])

    def test_full_source_traversal_contract_is_separate_from_global_completeness(self):
        result = response(status="live", pages=94)
        result["coverage"].update(traversal_completed=True, scan_started_at=iso_time(NOW),
                                   scan_completed_at=iso_time(NOW + timedelta(minutes=16)),
                                   last_successful_full_scan_at=iso_time(NOW + timedelta(minutes=16)))
        result["finished_at"] = iso_time(NOW + timedelta(minutes=16))
        feed, state = run(result)
        self.assertEqual(feed["source_status"], "live")
        self.assertTrue(feed["coverage"]["traversal_completed"])
        self.assertFalse(feed["coverage"]["complete"])
        self.assertEqual(feed["stats"]["searches_succeeded"], 94)
        self.assertEqual(feed["stats"]["categories_completed"], 1)
        self.assertEqual(feed["generated_at"], result["finished_at"])
        self.assertEqual(feed["last_successful_scan_at"], result["finished_at"])
        self.assertEqual(state["updated_at"], result["finished_at"])

    def test_more_than_one_thousand_collected_rows_are_accepted(self):
        collected = [item(id=f"v1|{123456780000 + number}|0", url=f"https://www.ebay.de/itm/{123456780000 + number}")
                     for number in range(1200)]
        feed, state = run(response(collected, status="live", pages=26))
        self.assertEqual(len(feed["deals"]), 1200)
        self.assertEqual(len(state["records"]), 1200)
        self.assertEqual(feed["stats"]["omitted_records"], 0)
        self.assertTrue(feed["coverage"]["all_collected_records_retained"])

    def test_completion_time_accepts_late_observations_and_prunes_ended_during_scan(self):
        _, previous = run(response([item(ends_at=iso_time(NOW + timedelta(minutes=5)))]))
        late = item(id="v1|123456789013|0", url="https://www.ebay.de/itm/123456789013",
                    source_observed_at=iso_time(NOW + timedelta(minutes=1)))
        expired = item(id="v1|123456789014|0", url="https://www.ebay.de/itm/123456789014",
                       ends_at=iso_time(NOW + timedelta(minutes=10)))
        result = response([late, expired], finished_at=iso_time(NOW + timedelta(minutes=16)))
        feed, state = run(result, state=previous)
        self.assertEqual([row["id"] for row in feed["deals"]], [late["id"]])
        self.assertEqual(feed["deals"][0]["source_observed_at"], late["source_observed_at"])
        self.assertEqual(state["records"], feed["deals"])

    def test_completion_time_cannot_be_before_start_or_unbounded_future(self):
        for value in (iso_time(NOW - timedelta(seconds=1)), iso_time(NOW + timedelta(hours=1)), "invalid"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                run(response(finished_at=value))

    def test_carried_observation_becomes_stale_during_long_scan(self):
        _, previous = run(response([item(source_observed_at=iso_time(NOW - timedelta(minutes=80)))]))
        feed, _ = run(response([], pages=0, status="error", finished_at=iso_time(NOW + timedelta(minutes=16))), state=previous)
        self.assertTrue(feed["deals"][0]["carried"])
        self.assertTrue(feed["deals"][0]["stale"])
        self.assertEqual(feed["deals"][0]["last_seen_at"], iso_time(NOW - timedelta(minutes=80)))

    def test_explicit_publication_switch_still_disables_network(self):
        called = []
        feed, _ = run(environ={}, aog_collect=lambda *args: called.append(args))
        self.assertEqual(called, [])
        self.assertEqual(feed["source_status"], "unconfigured")
        self.assertNotIn("ebay_credentials_missing", [row["code"] for row in feed["blockers"]])

    def test_observation_is_not_refreshed_to_import_time(self):
        feed, _ = run()
        row = feed["deals"][0]
        self.assertEqual(row["last_seen_at"], iso_time(OBSERVED))
        self.assertEqual(row["source_observed_at"], iso_time(OBSERVED))
        self.assertEqual(row["price_kind"], "auction_price")
        self.assertEqual(feed["last_successful_scan_at"], iso_time(NOW))

    def test_no_fabricated_location_description_reserve_or_exact_distance(self):
        injected = item(description="stale former text", city="Heidelberg", postcode="69121",
                        reserve_status="none", distance_km=1, local_pickup_confirmed=True,
                        _details_fetched=True)
        feed, _ = run(response([injected]))
        row = feed["deals"][0]
        self.assertEqual(row["description"], "")
        self.assertEqual(row["city"], "")
        self.assertEqual(row["postcode"], "")
        self.assertIsNone(row["distance_km"])
        self.assertEqual(row["reserve_status"], "unknown")
        self.assertFalse(row["local_pickup_confirmed"])
        self.assertFalse(row["details_fetched"])
        self.assertEqual(row["eligibility"], "review")

    def test_pickup_query_provenance_survives_state_without_claiming_verified_nearness(self):
        feed, state = run()
        row = state["records"][0]
        self.assertTrue(row["pickup_search_confirmed"])
        self.assertEqual(row["pickup_search_postcode"], "69121")
        self.assertEqual(row["pickup_search_radius_km"], 50)
        self.assertFalse(row["local_pickup_confirmed"])
        self.assertEqual(row, feed["deals"][0])

    def test_invalid_pickup_claim_is_not_accepted(self):
        for changes in ({"pickup_search_postcode": "not-a-postcode"},
                        {"pickup_search_radius_km": True}, {"pickup_search_radius_km": 0},
                        {"pickup_search_radius_km": 1000}, {"pickup_available": None}):
            with self.subTest(changes=changes):
                normalized = normalize_item(item(**changes), NOW)
                self.assertFalse(normalized["pickup_search_confirmed"])
                self.assertNotIn("pickup_search_postcode", normalized)

    def test_invalid_independent_pickup_time_drops_only_radius_claim(self):
        for value in (None, "invalid", iso_time(NOW + timedelta(seconds=1))):
            with self.subTest(value=value):
                source = item(pickup_search_observed_at=value)
                normalized = normalize_item(source, NOW)
                self.assertIsNotNone(normalized)
                self.assertFalse(normalized["pickup_search_confirmed"])
                self.assertNotIn("pickup_search_postcode", normalized)
                self.assertNotIn("pickup_search_observed_at", normalized)
                self.assertEqual(normalized["source_observed_at"], iso_time(OBSERVED))
                self.assertEqual(normalized["price_eur"], 1)
                self.assertEqual(normalized["bid_count"], 0)
                self.assertEqual(normalized["title"], source["title"])

    def test_legacy_pickup_clock_migrates_only_when_field_absent(self):
        legacy = normalize_item(item(), NOW)
        self.assertTrue(legacy["pickup_search_confirmed"])
        self.assertEqual(legacy["pickup_search_observed_at"], iso_time(OBSERVED))
        separate = normalize_item(item(pickup_search_observed_at=iso_time(NOW - timedelta(hours=3))), NOW)
        self.assertTrue(separate["pickup_search_confirmed"])
        self.assertEqual(separate["pickup_search_observed_at"], iso_time(NOW - timedelta(hours=3)))
        self.assertEqual(separate["source_observed_at"], iso_time(OBSERVED))

    def test_future_or_missing_source_timestamp_is_not_current(self):
        for value in (None, "not-a-time", iso_time(NOW + timedelta(minutes=1))):
            with self.subTest(value=value):
                feed, _ = run(response([item(source_observed_at=value)]))
                self.assertEqual(feed["deals"], [])

    def test_ongoing_auctions_remain_despite_old_observation(self):
        for age in (timedelta(hours=24), timedelta(hours=72), timedelta(days=7)):
            with self.subTest(age=age):
                feed, _ = run(response([item(source_observed_at=iso_time(NOW - age))]))
                row = feed["deals"][0]
                self.assertEqual(row["last_seen_at"], iso_time(NOW - age))
                self.assertEqual(row["source_observed_at"], iso_time(NOW - age))
                self.assertEqual(row["eligibility"], "review")
                self.assertTrue(row["stale"])

    def test_seven_and_twenty_three_hour_source_observations_are_retained_but_stale(self):
        for age in (timedelta(hours=7), timedelta(hours=23), timedelta(hours=24, seconds=-1)):
            with self.subTest(age=age):
                observed = iso_time(NOW - age)
                feed, state = run(response([item(source_observed_at=observed)]))
                row = feed["deals"][0]
                self.assertEqual(row["last_seen_at"], observed)
                self.assertEqual(row["source_observed_at"], observed)
                self.assertEqual(row["content_updated_at"], observed)
                self.assertTrue(row["stale"])
                self.assertEqual(row["eligibility"], "review")
                self.assertEqual(feed["stats"]["stale"], 1)
                self.assertEqual(feed["criteria"]["description_max_age_hours"], 24)
                self.assertEqual(feed["criteria"]["lookahead_hours"], 72)
                self.assertNotIn("max_freshness_hours", feed["criteria"])
                self.assertEqual(state["records"][0], row)
                self.assertTrue(any("älter als zwei Stunden" in risk for risk in row["risks"]))

    def test_longer_buffer_never_preserves_ended_auctions(self):
        for age in (timedelta(hours=7), timedelta(hours=23)):
            for end in (NOW, NOW - timedelta(seconds=1)):
                with self.subTest(age=age, end=end):
                    feed, state = run(response([item(source_observed_at=iso_time(NOW - age), ends_at=iso_time(end))]))
                    self.assertEqual(feed["deals"], [])
                    self.assertEqual(state["records"], [])

    def test_cooldown_keeps_fresh_cache_without_updating_observation(self):
        _, previous = run()
        feed, _ = run(response([], status="cooldown", pages=0), state=previous, at=NOW + timedelta(minutes=10))
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(feed["deals"][0]["last_seen_at"], iso_time(OBSERVED))
        self.assertEqual(feed["last_successful_scan_at"], iso_time(NOW))

    def test_no_request_cooldown_is_named_without_claiming_a_new_scan(self):
        _, previous = run()
        result = response([], status="cooldown", pages=0,
                          warnings=[{"code": "source_cooldown", "message": "Abrufpause"}])
        result["coverage"].update(requests_this_run=0, last_successful_full_scan_at=iso_time(NOW))
        feed, _ = run(result, state=previous, at=NOW + timedelta(minutes=10))
        self.assertEqual(feed["source_status"], "partial")
        self.assertIn("Abrufpause", feed["status_message"])
        self.assertIn("keine neue Quellenabfrage", feed["status_message"])
        self.assertEqual(feed["coverage"]["last_successful_full_scan_at"], iso_time(NOW))
        self.assertEqual(feed["last_successful_scan_at"], iso_time(NOW))
        self.assertEqual(feed["deals"][0]["source_observed_at"], iso_time(OBSERVED))
        result["coverage"]["requests_this_run"] = 1
        attempted, _ = run(result, state=previous, at=NOW + timedelta(minutes=10))
        self.assertNotIn("keine neue Quellenabfrage", attempted["status_message"])

    def test_cooldown_keeps_week_old_cache_without_refreshing_observation(self):
        observed = iso_time(NOW - timedelta(days=7))
        _, previous = run(response([item(source_observed_at=observed)]))
        feed, _ = run(response([], status="cooldown", pages=0), state=previous, at=NOW + timedelta(minutes=10))
        row = feed["deals"][0]
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(row["last_seen_at"], observed)
        self.assertEqual(row["source_observed_at"], observed)
        self.assertTrue(row["stale"])
        self.assertTrue(row["carried"])
        self.assertEqual(feed["last_successful_scan_at"], iso_time(NOW))

    def test_error_without_cache_is_not_successful_empty_scan(self):
        feed, _ = run(response([], status="error", pages=0, warnings=[{"code": "source_unavailable", "message": "Abruf fehlgeschlagen"}]))
        self.assertEqual(feed["source_status"], "error")
        self.assertIsNone(feed["last_successful_scan_at"])
        self.assertEqual(feed["blockers"][0]["code"], "source_unavailable")

    def test_evidence_from_another_source_is_not_inherited(self):
        previous = new_state(NOW)
        old = normalize_item({"id": "v1|123456789012|0", "title": "Autoklav", "description": "Old eBay detail",
                              "ends_at": iso_time(NOW + timedelta(hours=3)), "reserve_status": "none",
                              "local_pickup_confirmed": True, "_details_fetched": True}, NOW - timedelta(minutes=10))
        previous["records"] = [old]
        feed, _ = run(state=previous)
        self.assertEqual(feed["deals"][0]["description"], "")
        self.assertFalse(feed["deals"][0]["local_pickup_confirmed"])
        self.assertEqual(feed["deals"][0]["reserve_status"], "unknown")

    def test_aog_cooldown_state_roundtrips_and_is_checkpointed(self):
        checkpoints = []
        _, previous = run(checkpoint=lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertTrue(checkpoints)
        self.assertEqual(checkpoints[0]["aog"], previous["aog"])
        supplied = []
        def collect(settings, at, state):
            supplied.append(state)
            return response([], status="cooldown", pages=0)
        run(state=previous, aog_collect=collect)
        self.assertEqual(supplied[0], previous["aog"])

    def test_general_refresh_does_not_renew_old_pickup_query_evidence(self):
        _, previous = run()
        general = item(pickup_search_confirmed=False, pickup_available=None,
                       source_observed_at=iso_time(NOW + timedelta(minutes=15)))
        feed, _ = run(response([general]), state=previous, at=NOW + timedelta(minutes=30))
        self.assertTrue(feed["deals"][0]["pickup_search_confirmed"])
        self.assertEqual(feed["deals"][0]["pickup_search_observed_at"], iso_time(OBSERVED))
        self.assertEqual(feed["deals"][0]["source_observed_at"], general["source_observed_at"])

    def test_partial_pickup_traversal_preserves_separately_dated_old_radius_evidence(self):
        _, previous = run()
        general = item(pickup_search_confirmed=False, pickup_available=None,
                       source_observed_at=iso_time(NOW + timedelta(minutes=15)))
        result = response([general])
        result["coverage"]["searches"] = {"pickup": {"enabled": True, "traversal_completed": False}}
        feed, _ = run(result, state=previous, at=NOW + timedelta(minutes=30))
        row = feed["deals"][0]
        self.assertTrue(row["pickup_search_confirmed"])
        self.assertEqual(row["source_observed_at"], general["source_observed_at"])
        self.assertEqual(row["pickup_search_observed_at"], iso_time(OBSERVED))
        self.assertEqual(row["last_seen_at"], general["source_observed_at"])

    def test_only_explicit_no_pickup_not_completed_sparse_absence_refutes_old_claim(self):
        _, previous = run()
        for completed, available in ((True, None), (False, False)):
            with self.subTest(completed=completed, available=available):
                general = item(pickup_search_confirmed=False, pickup_available=available,
                               source_observed_at=iso_time(NOW + timedelta(minutes=15)))
                result = response([general])
                result["coverage"]["searches"] = {"pickup": {"enabled": True, "traversal_completed": completed,
                                                               "absence_is_evidence": False, "sparse_pages": 6}}
                feed, _ = run(result, state=previous, at=NOW + timedelta(minutes=30))
                self.assertEqual(feed["deals"][0]["pickup_search_confirmed"], available is not False)
                if available is False:
                    self.assertNotIn("pickup_search_observed_at", feed["deals"][0])
                else:
                    self.assertEqual(feed["deals"][0]["pickup_search_observed_at"], iso_time(OBSERVED))

    def test_changed_radius_profile_does_not_reuse_old_pickup_claim(self):
        _, previous = run()
        previous["records"][0]["pickup_search_radius_km"] = 100
        general = item(pickup_search_confirmed=False, pickup_available=None,
                       source_observed_at=iso_time(NOW + timedelta(minutes=15)))
        feed, _ = run(response([general]), state=previous, at=NOW + timedelta(minutes=30))
        self.assertFalse(feed["deals"][0]["pickup_search_confirmed"])
        self.assertNotIn("pickup_search_observed_at", feed["deals"][0])

    def test_empty_arrays_with_positive_totals_do_not_relabel_old_facts_as_fresh(self):
        _, previous = run()
        result = response([], status="partial", pages=3)
        result["coverage"].update(traversal_completed=True, reported_nonempty_but_no_records=True)
        feed, _ = run(result, state=previous, at=NOW + timedelta(minutes=30))
        self.assertEqual(feed["source_status"], "partial")
        self.assertIn("meldet Treffer, liefert aber keine Artikel", feed["status_message"])
        self.assertEqual(feed["last_successful_scan_at"], iso_time(NOW))
        self.assertEqual(feed["deals"][0]["source_observed_at"], iso_time(OBSERVED))
        self.assertTrue(feed["deals"][0]["carried"])

    def test_absence_from_a_partial_page_never_deletes_still_running_records(self):
        _, previous = run()
        result = response([], status="partial", pages=1)
        result["coverage"].update(traversal_completed=False, last_successful_full_scan_at=iso_time(NOW - timedelta(hours=1)))
        feed, _ = run(result, state=previous, at=NOW + timedelta(minutes=30))
        self.assertEqual(len(feed["deals"]), 1)
        self.assertTrue(feed["deals"][0]["carried"])
        self.assertEqual(feed["deals"][0]["source_observed_at"], iso_time(OBSERVED))
        self.assertEqual(feed["coverage"]["last_successful_full_scan_at"], iso_time(NOW - timedelta(hours=1)))

    def test_full_scan_removes_absent_items_from_state_feed_and_public_details(self):
        kept = item(id="v1|123456789013|0", url="https://www.ebay.de/itm/123456789013")
        added = item(id="v1|123456789014|0", url="https://www.ebay.de/itm/123456789014")
        old_feed, previous = run(response([item(), kept]))
        result = full_response([kept, added])
        checkpoints = []
        feed, state = run(result, state=previous, at=NOW + timedelta(minutes=30),
                          checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertEqual({row["id"] for row in feed["deals"]}, {kept["id"], added["id"]})
        self.assertEqual(state["records"], feed["deals"])
        self.assertEqual([row["id"] for row in checkpoints[-1]["records"]], [kept["id"]])
        self.assertEqual(feed["coverage"]["reconciliation"], {
            "policy": "successful_full_scan", "applied": True, "removed_records": 1,
            "retained_newer_records": 0, "reason": "completed"})
        self.assertFalse(feed["coverage"]["complete"])
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            write_feed(site, old_feed)
            old_details = set((site / "api" / "items").glob("*.json"))
            self.assertEqual(len(old_details), 2)
            write_feed(site, feed)
            details = set((site / "api" / "items").glob("*.json"))
            self.assertEqual(len(details), 2)
            self.assertEqual(len(old_details - details), 1)
            self.assertEqual({row["id"] for row in json.loads((site / "data" / "deals.json").read_text())["deals"]},
                             {kept["id"], added["id"]})
            manifest = json.loads((site / "api" / "manifest.json").read_text())
            self.assertEqual(manifest["counts"]["total"], 2)
            pages = [json.loads((site / page["path"]).read_text()) for page in manifest["pages"]]
            self.assertEqual({row["id"] for page in pages for row in page["items"]}, {kept["id"], added["id"]})
        # A subsequent failed run must not bring the deleted item back.
        later, _ = run(response([], status="error", pages=0), state=state, at=NOW + timedelta(hours=1))
        self.assertEqual({row["id"] for row in later["deals"]}, {kept["id"], added["id"]})

    def test_successful_empty_full_scan_removes_all_old_items_and_cache_even_on_enrichment_error(self):
        _, previous = run()
        previous["semantic_cache"] = {item()["id"]: {"assessment": {"scalp_score": 80}}}
        document = {
            "schema_version": 1, "source_snapshot_id": "sha256:" + "a" * 64,
            "reviewed_at": iso_time(NOW), "reviewer": "ChatGPT",
            "reviews": [{"id": item()["id"], "content_fingerprint": content_fingerprint(normalize_item(item(), NOW)),
                "assessment": {"semantic_interest": 60, "value_band": "around_500",
                    "assessment_confidence": "low", "product_kind": "apparatus",
                    "value_reason": "Plausible Geräteklasse, ungeprüfter Zustand.",
                    "reasons": [], "risks": [], "next_checks": []}}],
        }
        def failing_enrichment(*args, **kwargs):
            raise ValueError("optional enrichment failed")
        feed, state = run(full_response([]), state=previous, at=NOW + timedelta(minutes=30),
                          enrich=failing_enrichment, review_doc=document)
        self.assertEqual(feed["deals"], [])
        self.assertEqual(state["records"], [])
        self.assertEqual(state["semantic_cache"], {})
        self.assertEqual(feed["coverage"]["reconciliation"]["removed_records"], 1)
        self.assertEqual(feed["source_status"], "live")
        self.assertEqual(feed["interactive_review"]["applied"], 0)
        self.assertEqual(len(document["reviews"]), 1)

    def test_real_collector_preserves_union_of_general_and_pickup_results(self):
        _, previous = run(response([item(), item(id="v1|123456789014|0", url="https://www.ebay.de/itm/123456789014")]))
        def transport(*, url, **kwargs):
            if url.endswith("robots.txt"):
                return HttpResponse(200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n")
            pickup = "abholung" in parse_qs(urlsplit(url).query)
            number = "123456789014" if pickup else "123456789013"
            payload = {"gesamt": 1, "artikel": [{"titel": "Autoklav", "link": f"https://www.ebay.de/itm/{number}",
                       "ende": iso_time(NOW + timedelta(hours=4)), "preis": 1, "gebote": 0, "abholung": pickup}]}
            return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode())
        def collector(settings, at, state):
            return collect(settings, at, state, transport=transport, sleep=lambda _: None, monotonic=lambda: 0)
        feed, state = run(state=previous, at=NOW + timedelta(minutes=30), aog_collect=collector)
        self.assertEqual({row["id"] for row in feed["deals"]}, {"v1|123456789013|0", "v1|123456789014|0"})
        self.assertEqual(feed["coverage"]["reconciliation"]["removed_records"], 1)
        self.assertEqual(state["records"], feed["deals"])
        self.assertTrue(next(row for row in feed["deals"] if row["id"] == "v1|123456789014|0")["pickup_search_confirmed"])

    def test_incomplete_failed_or_unconfirmed_scans_do_not_remove_absent_items(self):
        _, previous = run()
        variants = []
        for status in ("partial", "error", "cooldown"):
            result = full_response([])
            result["status"] = status
            variants.append((status, result))
        for field, value in (("traversal_completed", False), ("reported_nonempty_but_no_records", True),
                             ("searches", {}), ("scan_started_at", iso_time(NOW)), ("scan_completed_at", None)):
            result = full_response([])
            result["coverage"][field] = value
            variants.append((field, result))
        for field, value in (("enabled", False), ("status", "partial"), ("traversal_completed", False),
                             ("stop_reason", "repeated_page"), ("pages_fetched", 0),
                             ("reported_nonempty_but_no_records", True)):
            result = full_response([])
            result["coverage"]["searches"]["pickup"][field] = value
            variants.append(("pickup." + field, result))
        for code in ("source_run_budget", "source_run_deadline", "source_daily_budget", "source_growth_limit"):
            result = full_response([])
            result["warnings"] = [{"code": code, "message": "Durchlauf begrenzt"}]
            variants.append((code, result))
        result = full_response([])
        result["coverage"]["api_requests_this_run"] = 3
        variants.append(("failed_request", result))
        for name, result in variants:
            with self.subTest(case=name):
                feed, state = run(result, state=previous, at=NOW + timedelta(minutes=30))
                self.assertEqual([row["id"] for row in state["records"]], [item()["id"]])
                self.assertFalse(feed["coverage"]["reconciliation"]["applied"])
                self.assertEqual(feed["coverage"]["reconciliation"]["reason"], "incomplete_scan")

    def test_invalid_received_records_cannot_turn_a_full_scan_into_deletion(self):
        _, previous = run()
        for changes in ({"source_observed_at": None}, {"source_observed_at": iso_time(NOW + timedelta(hours=1))},
                        {"source_provider": "other"}, {"source_url": "https://example.invalid/"}, {"id": ""}):
            with self.subTest(changes=changes):
                feed, state = run(full_response([item(**changes)]), state=previous, at=NOW + timedelta(minutes=30))
                self.assertEqual([row["id"] for row in state["records"]], [item()["id"]])
                self.assertEqual(feed["coverage"]["reconciliation"]["reason"], "invalid_records")
                self.assertFalse(feed["coverage"]["reconciliation"]["applied"])

    def test_missing_or_future_scan_observation_prevents_deletion(self):
        _, previous = run()
        for observed in (None, "invalid", iso_time(NOW + timedelta(hours=1))):
            with self.subTest(observed=observed):
                result = full_response([])
                result["observed_at"] = observed
                feed, state = run(result, state=previous, at=NOW + timedelta(minutes=30))
                self.assertEqual(len(state["records"]), 1)
                self.assertEqual(feed["coverage"]["reconciliation"]["reason"], "invalid_observation")
                self.assertFalse(feed["coverage"]["reconciliation"]["applied"])

    def test_newer_stored_observation_survives_older_full_scan_but_not_fresh_absence(self):
        _, previous = run()
        result = full_response([], observed_at=NOW - timedelta(minutes=30))
        feed, state = run(result, state=previous, at=NOW + timedelta(minutes=30))
        self.assertEqual(len(state["records"]), 1)
        self.assertEqual(state["records"][0]["last_seen_at"], iso_time(OBSERVED))
        self.assertTrue(feed["coverage"]["reconciliation"]["applied"])
        self.assertEqual(feed["coverage"]["reconciliation"]["retained_newer_records"], 1)
        self.assertEqual(feed["coverage"]["reconciliation"]["removed_records"], 0)
        fresh, _ = run(full_response([], at=NOW + timedelta(hours=1)), state=state, at=NOW + timedelta(hours=1))
        self.assertEqual(fresh["deals"], [])
        self.assertEqual(fresh["coverage"]["reconciliation"]["removed_records"], 1)

    def test_seen_item_survives_even_when_older_cache_keeps_it_marked_carried(self):
        _, previous = run(response([item(price_eur=5, bid_count=1)]))
        cached = item(source_observed_at=iso_time(NOW - timedelta(minutes=30)))
        feed, _ = run(full_response([cached], observed_at=NOW - timedelta(minutes=30)),
                      state=previous, at=NOW + timedelta(minutes=30))
        row = feed["deals"][0]
        self.assertEqual(row["price_eur"], 5)
        self.assertEqual(row["bid_count"], 1)
        self.assertTrue(row["carried"])
        self.assertTrue(feed["coverage"]["reconciliation"]["applied"])
        self.assertEqual(feed["coverage"]["reconciliation"]["removed_records"], 0)

    def test_disabled_pickup_search_does_not_prevent_general_full_scan_reconciliation(self):
        _, previous = run()
        result = full_response([], pickup_enabled=False)
        feed, state = run_pipeline({**SETTINGS, "aog_pickup_enabled": False}, source="aog", state=previous,
                                   at=NOW + timedelta(minutes=30), environ=ENV,
                                   aog_collect=lambda *args: copy.deepcopy(result))
        self.assertEqual(state["records"], [])
        self.assertTrue(feed["coverage"]["reconciliation"]["applied"])

    def test_older_cached_page_cannot_restore_obsolete_zero_bid_price(self):
        _, previous = run(response([item(price_eur=5, bid_count=1)]))
        old_summary = item(price_eur=1, bid_count=0, source_observed_at=iso_time(NOW - timedelta(minutes=30)))
        feed, _ = run(response([old_summary]), state=previous, at=NOW + timedelta(minutes=30))
        row = feed["deals"][0]
        self.assertEqual(row["price_eur"], 5)
        self.assertEqual(row["bid_count"], 1)
        self.assertEqual(row["last_seen_at"], iso_time(OBSERVED))
        self.assertEqual(row["eligibility"], "excluded")
        self.assertTrue(row["carried"])

    def test_newly_received_but_old_source_data_is_marked_stale(self):
        old_summary = item(source_observed_at=iso_time(NOW - timedelta(hours=2)))
        feed, _ = run(response([old_summary]))
        self.assertTrue(feed["deals"][0]["stale"])
        self.assertEqual(feed["stats"]["stale"], 1)

    def test_interactive_reviews_are_applied_and_reported_separately_from_api(self):
        normalized = normalize_item(item(), NOW)
        document = {
            "schema_version": 2, "source_snapshot_id": "sha256:" + "a" * 64,
            "reviewed_at": iso_time(NOW), "reviewer": "ChatGPT",
            "reviews": [{"id": normalized["id"], "content_fingerprint": content_fingerprint(normalized),
                "assessment": {"scalp_score": 86, "useability_score": 80, "value_band": "around_500",
                    "useability_reason": "Eigenständiges Gerät mit üblichen Betriebsmitteln.", "dependencies": ["Betriebsmittel"],
                    "assessment_confidence": "low", "product_kind": "apparatus",
                    "value_reason": "Eigenständiges Laborgerät; genauer Typ und Zustand unbekannt.",
                    "reasons": ["Vollständiges Spezialgerät möglich"], "risks": ["Funktion nicht geprüft"],
                    "next_checks": ["Typenschild und Vollständigkeit prüfen"]}}],
        }
        feed, state = run(review_doc=document)
        self.assertEqual(feed["interactive_review"]["status"], "applied")
        self.assertEqual(feed["interactive_review"]["applied"], 1)
        self.assertEqual(feed["interactive_review"]["unreviewed"], 0)
        self.assertFalse(feed["ai_enabled"])
        row = feed["deals"][0]
        self.assertEqual(row["assessment_basis"], "llm")
        self.assertEqual(row["semantic_review_mode"], "interactive")
        self.assertEqual(row["score_version"], 2)
        self.assertEqual(row["scalp_score"], 86)
        self.assertEqual(row["useability_score"], 80)
        self.assertEqual(row["interest_score"], 69)
        self.assertEqual(row["score"], 69)
        self.assertEqual(row["dependencies"], ["Betriebsmittel"])
        self.assertEqual(row["eligibility"], "review")
        self.assertEqual(row["price_eur"], 1)
        self.assertFalse(row["local_pickup_confirmed"])
        self.assertEqual(row, state["records"][0])

    def test_legacy_interactive_review_remains_pending_for_useability(self):
        normalized = normalize_item(item(), NOW)
        document = {
            "schema_version": 1, "source_snapshot_id": "sha256:" + "a" * 64,
            "reviewed_at": iso_time(NOW), "reviewer": "ChatGPT",
            "reviews": [{"id": normalized["id"], "content_fingerprint": content_fingerprint(normalized),
                "assessment": {"semantic_interest": 60, "value_band": "around_500",
                    "assessment_confidence": "low", "product_kind": "apparatus",
                    "value_reason": "Plausible Geräteklasse, ungeprüfter Zustand.",
                    "reasons": [], "risks": [], "next_checks": []}}],
        }
        feed, state = run(review_doc=document)
        row = feed["deals"][0]
        self.assertEqual(feed["interactive_review"]["applied"], 1)
        self.assertEqual(feed["interactive_review"]["unreviewed"], 1)
        self.assertEqual(row["assessment_basis"], "llm")
        self.assertEqual(row["score_version"], 1)
        self.assertEqual(row["scalp_score"], round(60 * .7 + row["fact_score"]))
        self.assertIsNone(row["useability_score"])
        self.assertIsNone(row["interest_score"])
        self.assertIsNone(row["score"])
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(row, state["records"][0])

    def test_invalid_interactive_review_does_not_stop_the_public_feed(self):
        feed, _ = run(review_doc={"unknown": "not trusted instructions"})
        self.assertEqual(feed["source_status"], "partial")
        self.assertEqual(feed["interactive_review"]["status"], "invalid")
        self.assertEqual(feed["deals"][0]["assessment_basis"], "heuristic")

    def test_valid_interactive_review_stays_on_retained_old_source(self):
        reviewed = iso_time(NOW - timedelta(hours=2))
        for age in (timedelta(hours=7), timedelta(hours=23), timedelta(days=7)):
            with self.subTest(age=age):
                source = item(source_observed_at=iso_time(NOW - age))
                normalized = normalize_item(source, NOW)
                document = {
                    "schema_version": 2, "source_snapshot_id": "sha256:" + "a" * 64,
                    "reviewed_at": reviewed, "reviewer": "ChatGPT",
                    "reviews": [{"id": normalized["id"], "content_fingerprint": content_fingerprint(normalized),
                        "assessment": {"scalp_score": 86, "useability_score": 80, "value_band": "around_500",
                            "useability_reason": "Eigenständiges Gerät mit üblichen Betriebsmitteln.", "dependencies": ["Betriebsmittel"],
                            "assessment_confidence": "low", "product_kind": "apparatus",
                            "value_reason": "Laborgeräteklasse; Modell und Zustand unbekannt.",
                            "reasons": [], "risks": [], "next_checks": []}}],
                }
                feed, state = run(response([source]), review_doc=document)
                row = feed["deals"][0]
                self.assertEqual(feed["interactive_review"]["applied"], 1)
                self.assertEqual(row["last_seen_at"], source["source_observed_at"])
                self.assertEqual(row["source_observed_at"], source["source_observed_at"])
                self.assertEqual(row["semantic_assessed_at"], reviewed)
                self.assertEqual(row["assessment_basis"], "llm")
                self.assertTrue(row["stale"])
                self.assertFalse(feed["ai_enabled"])
                self.assertEqual(row, state["records"][0])

    def test_published_schedule_metadata_matches_half_hour_config(self):
        feed, _ = run()
        self.assertEqual(feed["coverage"]["schedule_interval_minutes"], 30)


if __name__ == "__main__":
    unittest.main()
