"""Hermetic tests for the static interface; no eBay/OpenAI/GitHub requests."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hunter import feed as publisher


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def deal(number: int = 1, **changes):
    item = {
        "id": f"v1|{100000000000 + number}|0", "title": f"Fiktives Prüfgerät {number}",
        "description": "Fiktiver Testtext, kein echtes Angebot.", "url": "", "image": "",
        "price_eur": 1, "shipping_eur": None, "bid_count": 0,
        "ends_at": iso(NOW + timedelta(hours=2)), "last_seen_at": iso(NOW), "content_updated_at": iso(NOW),
        "city": "Heidelberg", "postcode": "69121", "distance_km": None,
        "condition": "Laut Anzeige gebraucht", "eligibility": "review", "reserve_status": "unknown",
        "local_pickup_confirmed": True, "pickup_radius_km": 50,
        "interest_score": 75, "score": 75, "value_band": "unknown",
        "assessment_confidence": "low", "assessment_basis": "heuristic", "semantic_assessed": False,
        "reasons": ["Im Abhol-Suchradius"], "risks": ["Mindestpreis unbekannt"],
    }
    item.update(changes)
    return item


def sample(items=None, **changes):
    result = {
        "schema_version": 2, "generated_at": iso(NOW), "last_successful_scan_at": iso(NOW),
        "source_status": "live", "status_message": "Nur eine Test-Fixture.", "blockers": [],
        "location": {"postcode": "69121"}, "criteria": {"max_freshness_hours": 24},
        "stats": {"received": len(items or [])}, "coverage": {"categories_scanned": 1},
        "ai_enabled": False, "deals": items or [],
    }
    result.update(changes)
    return result


def aggregator_deal(number: int = 1, **changes):
    item = deal(
        number, source_provider="auktionen-ohne-gebot",
        source_url="https://auktionen-ohne-gebot.de/business-industrie/",
        source_observed_at=iso(NOW), verification_level="aggregator",
        last_seen_at=iso(NOW - timedelta(minutes=15)), content_updated_at=None,
        description="", city="", postcode="", distance_km=None,
        local_pickup_confirmed=False, pickup_available=None, reserve_status="unknown",
    )
    item.update(changes)
    return item


def schema_errors(value, schema, root, path="$input"):
    """Check the small vocabulary used by our schema without a CI dependency.

    This is a test helper, not a general JSON Schema implementation. Consumers
    can use any Draft 2020-12 validator with the published schema.
    """
    errors = []
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        errors.extend(schema_errors(value, target, root, path))
    if "oneOf" in schema:
        matches = sum(not schema_errors(value, child, root, path) for child in schema["oneOf"])
        if matches != 1:
            errors.append(f"{path}: oneOf matched {matches} branches")
    for child in schema.get("allOf", []):
        errors.extend(schema_errors(value, child, root, path))
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append(f"{path}: const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: enum")
    kinds = schema.get("type", [])
    if isinstance(kinds, str):
        kinds = [kinds]
    checks = {
        "object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        "boolean": isinstance(value, bool), "null": value is None,
    }
    if kinds and not any(checks[kind] for kind in kinds):
        return [*errors, f"{path}: type"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                errors.extend(schema_errors(child, properties[key], root, f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown {key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(schema_errors(child, schema["additionalProperties"], root, f"{path}.{key}"))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            errors.append(f"{path}: array length")
        for index, child in enumerate(value):
            errors.extend(schema_errors(child, schema.get("items", {}), root, f"{path}[{index}]"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            errors.append(f"{path}: text length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: pattern")
        if schema.get("format") == "date-time" and publisher._timestamp(value) is None:
            errors.append(f"{path}: date-time")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            errors.append(f"{path}: numeric range")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: exclusiveMinimum")
    return errors


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.site.mkdir()
        self.schema = json.loads(publisher.SCHEMA_PATH.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def read(self, path):
        return json.loads((self.site / path).read_text(encoding="utf-8"))

    def assertSchema(self, document):
        self.assertEqual(schema_errors(document, self.schema, self.schema), [])

    def test_127_items_are_all_paginated_and_share_one_snapshot(self):
        publisher.write_feed(self.site, sample([deal(index) for index in range(127)]))
        manifest = self.read("api/manifest.json")
        self.assertEqual(manifest["counts"], {"total": 127, "pages": 3, "page_size": 50})
        identifiers = []
        for expected_index, info in enumerate(manifest["pages"], 1):
            document = self.read(info["path"])
            self.assertSchema(document)
            self.assertEqual(document["snapshot_id"], manifest["snapshot_id"])
            self.assertEqual(document["page_index"], expected_index)
            self.assertEqual(document["page_count"], 3)
            self.assertEqual(document["total"], 127)
            self.assertEqual(len(document["items"]), info["count"])
            self.assertEqual(hashlib.sha256((self.site / info["path"]).read_bytes()).hexdigest(), info["sha256"])
            identifiers.extend(item["id"] for item in document["items"])
            self.assertEqual(document["next_path"], manifest["pages"][expected_index]["path"] if expected_index < 3 else None)
            for item in document["items"]:
                detail = self.read(item["detail_path"])
                self.assertSchema(detail)
                self.assertEqual(detail["snapshot_id"], manifest["snapshot_id"])
                self.assertEqual(detail["item"]["id"], item["id"])
        self.assertEqual(len(set(identifiers)), 127)
        self.assertSchema(manifest)
        self.assertSchema(self.read("data/deals.json"))

    def test_empty_unconfigured_feed_does_not_fabricate_offers(self):
        publisher.write_feed(self.site, sample(source_status="unconfigured", last_successful_scan_at=None))
        manifest = self.read("api/manifest.json")
        self.assertEqual(manifest["counts"], {"total": 0, "pages": 1, "page_size": 50})
        self.assertIsNone(manifest["last_successful_scan_at"])
        page = self.read(manifest["pages"][0]["path"])
        self.assertEqual(page["items"], [])
        self.assertEqual(self.read("data/deals.json")["deals"], [])
        self.assertSchema(page)
        self.assertSchema(manifest)

    def test_disabled_and_unconfigured_never_publish_stale_input_records(self):
        for status in ("unconfigured", "disabled"):
            with self.subTest(status=status):
                publisher.write_feed(self.site, sample([deal()], source_status=status))
                data = self.read("data/deals.json")
                self.assertEqual(data["deals"], [])
                self.assertEqual(data["stats"]["omitted_by_source_status"], 1)

    def test_error_can_keep_fresh_carry_with_honest_source_status(self):
        publisher.write_feed(self.site, sample([deal(carried=True)], source_status="error"))
        data = self.read("data/deals.json")
        self.assertEqual(data["source_status"], "error")
        self.assertTrue(data["deals"][0]["carried"])

    def test_null_facts_are_preserved_and_not_defaulted_to_zero(self):
        publisher.write_feed(self.site, sample([deal(price_eur=None, bid_count=None, shipping_eur=None, distance_km=None, content_updated_at=None)]))
        item = self.read("data/deals.json")["deals"][0]
        for key in ("price_eur", "shipping_eur", "bid_count", "distance_km", "content_updated_at"):
            self.assertIsNone(item[key])
        self.assertEqual(item["reserve_status"], "unknown")
        self.assertEqual(item["eligibility"], "review")

    def test_met_reserve_is_not_claimed_to_be_no_reserve(self):
        publisher.write_feed(self.site, sample([deal(reserve_status="met", reserve_price_met=True)]))
        self.assertEqual(self.read("data/deals.json")["deals"][0]["reserve_status"], "met")

    def test_full_text_is_only_in_detail_and_lengths_are_disclosed(self):
        description = "Titel und Text. " * 2000
        publisher.write_feed(self.site, sample([deal(description=description)]))
        summary = self.read("data/deals.json")["deals"][0]
        detail = self.read(summary["detail_path"])["item"]
        self.assertEqual(len(summary["description"]), 600)
        self.assertTrue(summary["description_truncated"])
        self.assertEqual(len(detail["description"]), 20000)
        self.assertTrue(detail["description_truncated"])
        self.assertEqual(detail["description"], description[:20000])

    def test_untruncated_detail_is_not_marked_truncated(self):
        publisher.write_feed(self.site, sample([deal(description="x" * 800)]))
        summary = self.read("data/deals.json")["deals"][0]
        self.assertTrue(summary["description_truncated"])
        self.assertFalse(self.read(summary["detail_path"])["item"]["description_truncated"])

    def test_content_update_time_is_only_exported_when_present(self):
        changed = iso(NOW - timedelta(days=1))
        publisher.write_feed(self.site, sample([deal(content_updated_at=changed)]))
        self.assertEqual(self.read("data/deals.json")["deals"][0]["content_updated_at"], changed)

    def test_expired_out_of_buffer_undated_future_items_never_become_active(self):
        items = [
            deal(1, ends_at=iso(NOW)),
            deal(2, last_seen_at=iso(NOW - timedelta(hours=24, seconds=1))),
            deal(3, last_seen_at=None),
            deal(4, ends_at=None),
            deal(5, last_seen_at=iso(NOW + timedelta(minutes=2))),
            deal(6),
        ]
        publisher.write_feed(self.site, sample(items))
        data = self.read("data/deals.json")
        self.assertEqual([item["id"] for item in data["deals"]], [deal(6)["id"]])
        self.assertEqual(data["stats"]["active"], 1)
        self.assertEqual(data["stats"]["omitted_by_export_freshness"], 5)

    def test_exact_24_hour_boundary_is_not_promoted_as_active(self):
        publisher.write_feed(self.site, sample([deal(last_seen_at=iso(NOW - timedelta(hours=24)))]))
        data = self.read("data/deals.json")
        self.assertEqual(data["deals"], [])

    def test_price_refresh_does_not_rejuvenate_old_or_undated_description(self):
        for content_at, expected in ((iso(NOW - timedelta(hours=25)), "stale"), (None, "unknown")):
            with self.subTest(content_at=content_at):
                publisher.write_feed(self.site, sample([deal(content_updated_at=content_at, description="Old full description")]))
                item = self.read("data/deals.json")["deals"][0]
                self.assertEqual(item["last_seen_at"], iso(NOW))
                self.assertEqual(item["description"], "")
                self.assertEqual(item["description_status"], expected)
                self.assertEqual(self.read(item["detail_path"])["item"]["description"], "")

    def test_actual_backend_audit_fields_survive_public_export(self):
        additions = {"next_checks": ["Modell prüfen"], "product_kind": "apparatus", "semantic_interest": 90,
                     "semantic_assessed_at": iso(NOW), "price_kind": "minimum_bid", "details_fetched": True,
                     "total_known_eur": 11, "country": "DE", "buying_options": ["AUCTION"],
                     "eligibility_reasons": ["Mindestpreis unbekannt"]}
        publisher.write_feed(self.site, sample([deal(**additions)]))
        data = self.read("data/deals.json")
        self.assertSchema(data)
        for key, value in additions.items():
            self.assertEqual(data["deals"][0][key], value)

    def test_source_buffer_cannot_be_extended_above_one_day_by_config(self):
        publisher.write_feed(self.site, sample([deal(last_seen_at=iso(NOW - timedelta(hours=25)))], criteria={"max_freshness_hours": 48}))
        data = self.read("data/deals.json")
        self.assertEqual(data["freshness"]["maximum_source_age_hours"], 24)
        self.assertEqual(data["criteria"]["max_freshness_hours"], 24)
        self.assertEqual(data["deals"], [])

    def test_shorter_expiry_is_honored(self):
        publisher.write_feed(self.site, sample([deal(last_seen_at=iso(NOW - timedelta(hours=2)))], criteria={"max_freshness_hours": 1}))
        self.assertEqual(self.read("data/deals.json")["deals"], [])

    def test_valid_until_is_end_or_buffer_boundary_whichever_comes_first(self):
        publisher.write_feed(self.site, sample([deal(1), deal(2, ends_at=iso(NOW + timedelta(hours=30)))]))
        items = {item["id"]: item for item in self.read("data/deals.json")["deals"]}
        self.assertEqual(items[deal(1)["id"]]["valid_until"], iso(NOW + timedelta(hours=2)))
        self.assertEqual(items[deal(2)["id"]]["valid_until"], iso(NOW + timedelta(hours=24)))

    def test_default_buffer_is_24_hours_without_setting(self):
        publisher.write_feed(self.site, sample([deal(last_seen_at=iso(NOW - timedelta(hours=23)))], criteria={}))
        data = self.read("data/deals.json")
        self.assertEqual(len(data["deals"]), 1)
        self.assertEqual(data["criteria"]["max_freshness_hours"], 24)
        self.assertEqual(data["freshness"]["maximum_source_age_hours"], 24)
        self.assertTrue(data["freshness"]["retention_not_revalidation"])
        self.assertFalse(data["freshness"]["source_currentness_guaranteed"])
        self.assertEqual(data["schema_version"], 2)
        self.assertSchema(data)

    def test_7h_and_23h_observations_remain_without_becoming_new_facts(self):
        items = [aggregator_deal(
            hours, last_seen_at=iso(NOW - timedelta(hours=hours)),
            source_observed_at=iso(NOW - timedelta(hours=hours) + timedelta(minutes=15)),
            ends_at=iso(NOW + timedelta(hours=30)), stale=True, carried=True,
        ) for hours in (7, 23)]
        original = copy.deepcopy(items)
        publisher.write_feed(self.site, sample(items, source="auktionen-ohne-gebot", provider="auktionen-ohne-gebot"))
        data = self.read("data/deals.json")
        self.assertEqual(data["stats"]["active"], 2)
        self.assertEqual(data["stats"]["omitted_by_export_freshness"], 0)
        by_id = {item["id"]: item for item in data["deals"]}
        for raw in original:
            item = by_id[raw["id"]]
            self.assertEqual(item["last_seen_at"], raw["last_seen_at"])
            self.assertEqual(item["source_observed_at"], raw["source_observed_at"])
            self.assertTrue(item["stale"])
            self.assertTrue(item["carried"])
            self.assertEqual(item["price_eur"], raw["price_eur"])
            self.assertEqual(item["bid_count"], raw["bid_count"])
            expected = publisher._timestamp(raw["last_seen_at"]) + timedelta(hours=24)
            self.assertEqual(item["valid_until"], iso(expected))
            detail = self.read(item["detail_path"])["item"]
            self.assertEqual(detail["source_observed_at"], raw["source_observed_at"])
            self.assertTrue(detail["stale"])
        self.assertEqual(items, original)
        self.assertSchema(data)

    def test_lower_six_hour_buffer_still_drops_seven_hour_observation(self):
        publisher.write_feed(self.site, sample([deal(last_seen_at=iso(NOW - timedelta(hours=7)))], criteria={"max_freshness_hours": 6}))
        data = self.read("data/deals.json")
        self.assertEqual(data["deals"], [])
        self.assertEqual(data["freshness"]["maximum_source_age_hours"], 6)
        self.assertEqual(data["criteria"]["max_freshness_hours"], 6)

    def test_lower_fractional_buffer_honors_its_exact_boundary(self):
        publisher.write_feed(self.site, sample([
            deal(1, last_seen_at=iso(NOW - timedelta(minutes=29))),
            deal(2, last_seen_at=iso(NOW - timedelta(minutes=30))),
        ], criteria={"max_freshness_hours": 0.5}))
        data = self.read("data/deals.json")
        self.assertEqual([item["id"] for item in data["deals"]], [deal(1)["id"]])
        self.assertEqual(data["deals"][0]["valid_until"], iso(NOW + timedelta(minutes=1)))
        self.assertEqual(data["freshness"]["maximum_source_age_hours"], 0.5)

    def test_24h_retention_never_renews_a_description_timestamp(self):
        changed = iso(NOW - timedelta(hours=23))
        publisher.write_feed(self.site, sample([deal(content_updated_at=changed, description="Older description", stale=True)]))
        item = self.read("data/deals.json")["deals"][0]
        self.assertEqual(item["last_seen_at"], iso(NOW))
        self.assertEqual(item["content_updated_at"], changed)
        self.assertEqual(item["description"], "Older description")
        self.assertTrue(item["stale"])
        publisher.write_feed(self.site, sample([deal(content_updated_at=changed, description="Older description")], criteria={"max_freshness_hours": 6}))
        shorter = self.read("data/deals.json")["deals"][0]
        self.assertEqual(shorter["content_updated_at"], changed)
        self.assertEqual(shorter["description_status"], "stale")
        self.assertEqual(shorter["description"], "")

    def test_future_source_times_are_not_promoted_even_within_a_minute(self):
        future = iso(NOW + timedelta(seconds=1))
        publisher.write_feed(self.site, sample([
            deal(1, last_seen_at=future),
            aggregator_deal(2, source_observed_at=future),
            aggregator_deal(3, last_seen_at=future, source_observed_at=iso(NOW)),
        ]))
        self.assertEqual(self.read("data/deals.json")["deals"], [])

    def test_future_description_is_not_made_current_by_retention(self):
        publisher.write_feed(self.site, sample([deal(content_updated_at=iso(NOW + timedelta(seconds=1)))]))
        item = self.read("data/deals.json")["deals"][0]
        self.assertEqual(item["description"], "")
        self.assertEqual(item["description_status"], "unknown")

    def test_schema_accepts_one_day_buffer_but_not_longer(self):
        publisher.write_feed(self.site, sample())
        data = self.read("data/deals.json")
        self.assertSchema(data)
        data["freshness"]["maximum_source_age_hours"] = 24.01
        self.assertTrue(schema_errors(data, self.schema, self.schema))

    def test_snapshot_is_deterministic_and_covers_full_description(self):
        input_feed = sample([deal(2), deal(1)])
        original = copy.deepcopy(input_feed)
        publisher.write_feed(self.site, input_feed)
        first = self.read("api/manifest.json")["snapshot_id"]
        input_feed["deals"].reverse()
        publisher.write_feed(self.site, input_feed)
        self.assertEqual(self.read("api/manifest.json")["snapshot_id"], first)
        self.assertEqual(sorted(input_feed["deals"], key=lambda item: item["id"]), sorted(original["deals"], key=lambda item: item["id"]))
        input_feed["deals"][0]["description"] += "Ein weiteres Detail."
        publisher.write_feed(self.site, input_feed)
        self.assertNotEqual(self.read("api/manifest.json")["snapshot_id"], first)

    def test_unsafe_id_is_hashed_never_used_as_path(self):
        identifier = "../../elsewhere/evil.json?secret=not-a-real-secret"
        publisher.write_feed(self.site, sample([deal(id=identifier)]))
        item = self.read("data/deals.json")["deals"][0]
        expected = f"api/items/{hashlib.sha256(identifier.encode()).hexdigest()}.json"
        self.assertEqual(item["detail_path"], expected)
        self.assertTrue((self.site / expected).is_file())
        self.assertEqual(self.read(expected)["item"]["id"], identifier)

    def test_seller_credentials_and_exact_value_fields_are_not_exported(self):
        input_feed = sample([deal(seller="private-user", raw={"access_token": "private-token"}, market_value_low_eur=500, market_value_high_eur=800)], client_secret="root-secret")
        publisher.write_feed(self.site, input_feed)
        text = (self.site / "data/deals.json").read_text(encoding="utf-8")
        self.assertNotIn("private-user", text)
        self.assertNotIn("private-token", text)
        self.assertNotIn("root-secret", text)
        self.assertNotIn("market_value_", text)

    def test_nested_credentials_in_public_metadata_fail_without_echoing_value(self):
        with self.assertRaises(ValueError) as caught:
            publisher.write_feed(self.site, sample(coverage={"nested": {"client_secret": "do-not-echo"}}))
        self.assertNotIn("do-not-echo", str(caught.exception))
        self.assertFalse((self.site / "api/manifest.json").exists())

    def test_nonfinite_values_anywhere_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publisher.write_feed(self.site, sample([deal(price_eur=value)]))
        with self.assertRaises(ValueError):
            publisher.write_feed(self.site, sample(unused=float("nan")))

    def test_duplicate_ids_are_rejected_instead_of_overwriting_details(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            publisher.write_feed(self.site, sample([deal(), deal(title="Other detail")]))

    def test_invalid_timestamp_or_schema_fails_before_mutation(self):
        for changes in ({"generated_at": "2026-11-07T12:00:00"}, {"schema_version": 1}, {"source_status": "pretend-live"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                publisher.write_feed(self.site, sample(**changes))
        self.assertFalse((self.site / "api/manifest.json").exists())

    def test_numeric_booleans_are_not_valid_fact_values(self):
        for field in ("price_eur", "bid_count", "interest_score", "shipping_eur"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                publisher.write_feed(self.site, sample([deal(**{field: True})]))

    def test_interest_sort_never_promotes_excluded_above_eligible(self):
        publisher.write_feed(self.site, sample([deal(1, eligibility="excluded", interest_score=99), deal(2, eligibility="eligible", interest_score=20), deal(3, eligibility="review", interest_score=100)]))
        self.assertEqual([item["eligibility"] for item in self.read("data/deals.json")["deals"]], ["eligible", "review", "excluded"])

    def test_openapi_is_get_only_and_pages_match_manifest(self):
        publisher.write_feed(self.site, sample([deal(1), deal(2)], api_page_size=1))
        api = self.read("api/openapi.json")
        self.assertEqual(api["openapi"], "3.1.0")
        self.assertEqual(api["servers"], [{"url": "https://pogoner.github.io/fundgrube"}])
        for path, operations in api["paths"].items():
            self.assertEqual(set(operations), {"get"}, path)
        for page in self.read("api/manifest.json")["pages"]:
            self.assertIn("/" + page["path"], api["paths"])
        self.assertFalse(self.read("api/manifest.json")["interface"]["mcp_server"])

    def test_custom_site_url_is_supported_but_not_credentials_or_bad_schemes(self):
        publisher.write_feed(self.site, sample(site_url="https://example.org/demo/"))
        self.assertEqual(self.read("api/manifest.json")["interface"]["site_url"], "https://example.org/demo/")
        for url in ("javascript:alert(1)", "https://user:password@example.org/", "https://example.org/?key=secret", "http://example.org/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                publisher.write_feed(self.site, sample(site_url=url))

    def test_only_previously_generated_obsolete_files_are_removed(self):
        publisher.write_feed(self.site, sample([deal(index) for index in range(51)]))
        old = self.read("data/deals.json")["deals"][-1]["detail_path"]
        unrelated = self.site / "api/notes.txt"
        unrelated.write_text("User-owned notes stay.", encoding="utf-8")
        publisher.write_feed(self.site, sample())
        self.assertFalse((self.site / old).exists())
        self.assertFalse((self.site / "api/candidates/page-002.json").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "User-owned notes stay.")

    def test_malicious_inventory_path_cannot_delete_user_files(self):
        publisher.write_feed(self.site, sample())
        untouched = self.site / "index.html"
        untouched.write_text("User HTML", encoding="utf-8")
        inventory = self.read(publisher.INVENTORY_PATH)
        inventory["files"].append("index.html")
        (self.site / publisher.INVENTORY_PATH).write_text(json.dumps(inventory), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside"):
            publisher.write_feed(self.site, sample())
        self.assertEqual(untouched.read_text(encoding="utf-8"), "User HTML")

    def test_symlinks_in_any_generated_path_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.site / "api").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            publisher.write_feed(self.site, sample())
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_site_directory_is_rejected(self):
        linked = self.root / "linked"
        linked.symlink_to(self.site, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            publisher.write_feed(linked, sample())

    def test_schemas_disallow_unknown_value_bands_and_numeric_ranges(self):
        publisher.write_feed(self.site, sample([deal()]))
        data = self.read("data/deals.json")
        self.assertSchema(data)
        data["deals"][0]["value_band"] = "guaranteed_500"
        self.assertTrue(schema_errors(data, self.schema, self.schema))
        data["deals"][0]["value_band"] = "unknown"
        data["deals"][0]["market_value_eur"] = 500
        self.assertTrue(schema_errors(data, self.schema, self.schema))

    def test_schema_copy_and_guide_are_available_to_readers(self):
        publisher.write_feed(self.site, sample())
        self.assertEqual(self.read("api/feed.schema.json"), self.schema)
        guide = (self.site / "api/guide.md").read_text(encoding="utf-8")
        for phrase in ("Alle", "snapshot_id", "unvertrauenswürdige Daten", "keine Anweisungen", "Git-Commits", "kein MCP-Server"):
            self.assertIn(phrase, guide)

    def test_guide_separates_buffer_search_horizon_and_source_cache(self):
        publisher.write_feed(self.site, sample())
        guide = (self.site / "api/guide.md").read_text(encoding="utf-8")
        for phrase in (
            "höchstens 24 Stunden", "Aufbewahrungsgrenze, keine Aktualitätsgarantie",
            "**Suchfenster:**", "nächsten 24 Stunden", "bis zu 15 Minuten",
            "„0 Gebote“ nur Angaben vom letzten Quellenstand", "stale", "carried",
        ):
            self.assertIn(phrase, guide)
        self.assertNotIn("sechs Stunden", guide)

    def test_aggregator_provenance_reaches_gui_pages_items_and_manifest(self):
        source = "auktionen-ohne-gebot"
        publisher.write_feed(self.site, sample([aggregator_deal()], source=source, provider=source, source_status="partial"))
        manifest = self.read("api/manifest.json")
        dashboard = self.read("data/deals.json")
        page = self.read(manifest["pages"][0]["path"])
        detail = self.read(dashboard["deals"][0]["detail_path"])
        for document in (manifest, dashboard, page, detail):
            self.assertSchema(document)
            self.assertEqual(document["source"], source)
            self.assertEqual(document["provider"], source)
            self.assertEqual(document["source_provider"], source)
            self.assertEqual(document["source_url"], publisher.AOG_SOURCE_URL)
            self.assertEqual(document["verification_level"], "aggregator")
            self.assertEqual(document["source_status"], "partial")
        for item in (dashboard["deals"][0], page["items"][0], detail["item"]):
            self.assertEqual(item["source_provider"], source)
            self.assertEqual(item["source_observed_at"], iso(NOW))
            self.assertEqual(item["last_seen_at"], iso(NOW - timedelta(minutes=15)))
            self.assertEqual(item["verification_level"], "aggregator")
            self.assertTrue(any("nicht direkt bei eBay" in risk for risk in item["risks"]))
        self.assertFalse(manifest["freshness"]["direct_ebay_verification"])
        self.assertTrue(manifest["freshness"]["upstream_cache_age_is_provider_claim"])

    def test_aggregator_coverage_preserves_extra_fields_without_completeness_claim(self):
        coverage = {
            "requests_this_run": 3, "pages_fetched": 2, "page_size": 60,
            "source_total_reported": 345, "source_cache_max_age_minutes": 15,
            "source_claims_only": False, "next_offset": 120, "capped": True,
            "complete": True, "extra_limits": {"max_pages_per_run": 2, "all_pages_read": False},
        }
        publisher.write_feed(self.site, sample([aggregator_deal()], source="auktionen-ohne-gebot", provider="auktionen-ohne-gebot", coverage=coverage))
        manifest = self.read("api/manifest.json")
        self.assertSchema(manifest)
        exported = manifest["coverage"]
        self.assertEqual(exported["requests_this_run"], 3)
        self.assertEqual(exported["source_total_reported"], 345)
        self.assertEqual(exported["next_offset"], 120)
        self.assertEqual(exported["extra_limits"], coverage["extra_limits"])
        self.assertTrue(exported["source_claims_only"])
        self.assertFalse(exported["complete"])

    def test_aggregator_missing_fulltext_location_and_reserve_stay_unknown(self):
        publisher.write_feed(self.site, sample([aggregator_deal(pickup_available=True)]))
        item = self.read("data/deals.json")["deals"][0]
        self.assertTrue(item["pickup_available"])
        self.assertFalse(item["local_pickup_confirmed"])
        self.assertIsNone(item["distance_km"])
        self.assertEqual(item["city"], "")
        self.assertEqual(item["postcode"], "")
        self.assertEqual(item["description"], "")
        self.assertEqual(item["description_status"], "missing")
        self.assertEqual(item["reserve_status"], "unknown")

    def test_unknown_pickup_availability_is_not_false_or_true(self):
        publisher.write_feed(self.site, sample([aggregator_deal(pickup_available=None)]))
        self.assertIsNone(self.read("data/deals.json")["deals"][0]["pickup_available"])
        with self.assertRaisesRegex(ValueError, "pickup_available"):
            publisher.write_feed(self.site, sample([aggregator_deal(pickup_available="yes")]))

    def test_aggregator_source_url_is_fixed_not_an_arbitrary_link(self):
        urls = [
            "http://auktionen-ohne-gebot.de/business-industrie/",
            "https://auktionen-ohne-gebot.de/other/",
            "https://auktionen-ohne-gebot.de.evil.example/business-industrie/",
            "https://auktionen-ohne-gebot.de/business-industrie/?token=do-not-echo",
            "https://auktionen-ohne-gebot.de@evil.example/business-industrie/",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                publisher.write_feed(self.site, sample([aggregator_deal(source_url=url)]))
            self.assertNotIn("do-not-echo", str(caught.exception))
        self.assertFalse((self.site / "api/manifest.json").exists())

    def test_aggregator_cannot_claim_direct_api_verification(self):
        with self.assertRaisesRegex(ValueError, "direct verification"):
            publisher.write_feed(self.site, sample([aggregator_deal(verification_level="direct_api")]))
        with self.assertRaisesRegex(ValueError, "direct verification"):
            publisher.write_feed(self.site, sample([deal()], provider="auktionen-ohne-gebot"))

    def test_aggregator_source_timestamps_must_be_aware_and_fresh(self):
        for timestamp in (None, "not-a-time", "2026-11-07T12:00:00", iso(NOW - timedelta(hours=24)), iso(NOW - timedelta(hours=25)), iso(NOW + timedelta(minutes=2))):
            with self.subTest(timestamp=timestamp):
                publisher.write_feed(self.site, sample([aggregator_deal(source_observed_at=timestamp)]))
                dashboard = self.read("data/deals.json")
                self.assertEqual(dashboard["deals"], [])
                self.assertEqual(dashboard["stats"]["omitted_by_export_freshness"], 1)

    def test_source_fetch_does_not_make_older_observation_newer(self):
        publisher.write_feed(self.site, sample([aggregator_deal(source_observed_at=iso(NOW - timedelta(minutes=30)), last_seen_at=iso(NOW))]))
        item = self.read("data/deals.json")["deals"][0]
        self.assertEqual(item["source_observed_at"], iso(NOW - timedelta(minutes=30)))
        self.assertEqual(item["last_seen_at"], iso(NOW - timedelta(minutes=30)))

    def test_source_timestamp_is_normalized_to_utc(self):
        publisher.write_feed(self.site, sample([aggregator_deal(source_observed_at="2026-11-07T13:00:00+01:00")]))
        self.assertEqual(self.read("data/deals.json")["deals"][0]["source_observed_at"], iso(NOW))

    def test_optional_null_provenance_preserves_old_schema2_items(self):
        publisher.write_feed(self.site, sample([deal(source_provider=None, source_url=None, source_observed_at=None, verification_level=None)]))
        dashboard = self.read("data/deals.json")
        self.assertSchema(dashboard)
        self.assertEqual(len(dashboard["deals"]), 1)

    def test_aggregator_guide_discloses_cache_and_unknown_pickup(self):
        publisher.write_feed(self.site, sample())
        guide = (self.site / "api/guide.md").read_text(encoding="utf-8")
        for phrase in ("verification_level: aggregator", "15 Minuten", "Anbieterangabe", "pickup_available", "keine vollständigen Beschreibungen", "keinen Mindestpreisstatus"):
            self.assertIn(phrase, guide)

    def test_aggregator_radius_match_is_only_a_provisional_search_claim(self):
        publisher.write_feed(self.site, sample([aggregator_deal(
            pickup_search_confirmed=True, pickup_search_postcode="69121", pickup_search_radius_km=50,
            local_pickup_confirmed=True,
        )]))
        dashboard = self.read("data/deals.json")
        self.assertSchema(dashboard)
        item = dashboard["deals"][0]
        self.assertTrue(item["pickup_search_confirmed"])
        self.assertEqual(item["pickup_search_postcode"], "69121")
        self.assertEqual(item["pickup_search_radius_km"], 50)
        self.assertFalse(item["local_pickup_confirmed"])
        self.assertIsNone(item["distance_km"])
        self.assertEqual(item["city"], "")
        detail = self.read(item["detail_path"])["item"]
        self.assertTrue(detail["pickup_search_confirmed"])
        self.assertEqual(detail["source_observed_at"], iso(NOW))

    def test_invalid_radius_or_postcode_drops_search_claim(self):
        changes = [
            {"pickup_search_postcode": "6912"}, {"pickup_search_postcode": "69121x"},
            {"pickup_search_postcode": 69121}, {"pickup_search_postcode": ""},
            {"pickup_search_radius_km": -1}, {"pickup_search_radius_km": 0},
            {"pickup_search_radius_km": "50"}, {"pickup_search_radius_km": True},
            {"pickup_search_radius_km": None},
        ]
        for invalid in changes:
            with self.subTest(invalid=invalid):
                fields = {"pickup_search_confirmed": True, "pickup_search_postcode": "69121", "pickup_search_radius_km": 50, **invalid}
                publisher.write_feed(self.site, sample([aggregator_deal(**fields)]))
                item = self.read("data/deals.json")["deals"][0]
                self.assertFalse(item["pickup_search_confirmed"])
                self.assertFalse(item["local_pickup_confirmed"])
                self.assertNotIn("pickup_search_postcode", item)
                self.assertNotIn("pickup_search_radius_km", item)

    def test_radius_match_needs_real_boolean_and_aggregator_provenance(self):
        fields = {"pickup_search_confirmed": "true", "pickup_search_postcode": "69121", "pickup_search_radius_km": 50}
        publisher.write_feed(self.site, sample([aggregator_deal(**fields)]))
        self.assertFalse(self.read("data/deals.json")["deals"][0]["pickup_search_confirmed"])
        fields["pickup_search_confirmed"] = True
        publisher.write_feed(self.site, sample([deal(**fields)]))
        item = self.read("data/deals.json")["deals"][0]
        self.assertFalse(item["pickup_search_confirmed"])
        self.assertNotIn("pickup_search_postcode", item)


if __name__ == "__main__":
    unittest.main()
