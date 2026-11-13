"""Private runtime-origin migration and publication boundaries; no live I/O."""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hunter.aog import collect
from hunter.feed import write_feed
from hunter.models import iso_time, normalize_item
from hunter.pipeline import ConfigurationError, main, new_state, prepare_state, run_pipeline, validate_settings
from hunter.private_search import (
    PrivateSearchError, bind_pickup_context, load_private_search, matching_origin,
)
from hunter.reviews import content_fingerprint


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
ORIGIN = "99999"
KEY = "11" * 32  # Synthetic fixture, never a production secret.


def env(postcode=ORIGIN, key=KEY):
    return {"HUNTER_PUBLISH_LIVE_DATA": "true",
            "HUNTER_PRIVATE_SEARCH": json.dumps({"postcode": postcode, "profile_key": key})}


def legacy_item(**changes):
    result = {
        "id": "v1|123456789012|0", "title": "Fiktives Prüfgerät", "description": "", "condition": "Gebraucht",
        "url": "https://www.ebay.de/itm/123456789012", "price_eur": 1, "price_kind": "auction_price",
        "shipping_eur": None, "bid_count": 0, "ends_at": iso_time(NOW + timedelta(hours=4)),
        "source_provider": "auktionen-ohne-gebot", "verification_level": "aggregator",
        "source_url": "https://auktionen-ohne-gebot.de/business-industrie/",
        "source_observed_at": iso_time(NOW - timedelta(minutes=15)), "is_auction": True,
        "pickup_available": True, "pickup_search_confirmed": True, "pickup_search_postcode": ORIGIN,
        "pickup_search_radius_km": 50, "pickup_search_observed_at": iso_time(NOW - timedelta(hours=1)),
    }
    result.update(changes)
    return result


def response(items=(), *, status="cooldown"):
    return {"status": status, "items": list(items), "source_state": {
        "request_day": NOW.date().isoformat(), "requests_today": 120,
        "next_allowed_at": iso_time(NOW + timedelta(hours=2)),
    }, "coverage": {"pages_fetched": int(bool(items)), "api_requests_this_run": int(bool(items))}}


def run(*, state=None, environment=None, items=(), collect_override=None, **kwargs):
    return run_pipeline({}, source="aog", state=state, at=NOW, environ=env() if environment is None else environment,
                        aog_collect=collect_override or (lambda *args: response(items, status="partial" if items else "cooldown")),
                        enrich=lambda rows, *args, **kwargs: (rows, {"ai_enabled": False}), **kwargs)


def old_state():
    state = new_state(NOW)
    state["active_source"] = "auktionen-ohne-gebot"
    state["records"] = [normalize_item(legacy_item(), NOW)]
    state["daily_budget"]["calls_used"] = 42
    state["aog"] = {"request_day": NOW.date().isoformat(), "requests_today": 120,
                    "next_allowed_at": iso_time(NOW + timedelta(hours=2)), "postcode": ORIGIN,
                    "pickup_search_postcode": ORIGIN, "city": "Private Test Origin"}
    return state


class PrivateConfigurationTests(unittest.TestCase):
    def test_secret_is_strict_and_errors_do_not_echo_values(self):
        invalid = ["not-json", "[]", "null", "{}", json.dumps({"postcode": ORIGIN}),
                   json.dumps({"postcode": ORIGIN, "profile_key": "short"}),
                   json.dumps({"postcode": "9999", "profile_key": KEY}),
                   json.dumps({"postcode": ORIGIN, "profile_key": KEY, "city": "Private Test Origin"}),
                   '{"postcode":"99999","postcode":"88888","profile_key":"' + KEY + '"}']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PrivateSearchError) as caught:
                load_private_search({"HUNTER_PRIVATE_SEARCH": value}, required=True)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(ORIGIN, str(caught.exception))
        self.assertIsNone(load_private_search({}))
        with self.assertRaises(PrivateSearchError):
            load_private_search({}, required=True)

    def test_context_is_stable_keyed_and_changes_with_origin_or_key(self):
        private = load_private_search(env())
        self.assertEqual(private.profile, load_private_search(env()).profile)
        self.assertRegex(private.profile, r"^hmac-sha256:v1:[0-9a-f]{64}$")
        self.assertNotIn(ORIGIN, private.profile)
        self.assertNotIn(KEY, repr(private))
        self.assertNotIn(ORIGIN, repr(private))
        self.assertNotEqual(private.profile, load_private_search(env("88888")).profile)
        self.assertNotEqual(private.profile, load_private_search(env(key="22" * 32)).profile)

    def test_live_search_has_no_static_or_missing_secret_fallback(self):
        called = []
        for environment in ({"HUNTER_PUBLISH_LIVE_DATA": "true"},
                            {"HUNTER_PUBLISH_LIVE_DATA": "true", "HUNTER_PRIVATE_SEARCH": "invalid"}):
            with self.subTest(environment=environment), self.assertRaises(PrivateSearchError):
                run(environment=environment, collect_override=lambda *args: called.append(args))
        self.assertEqual(called, [])
        for key in ("postcode", "city", "profile_key", "pickup_search_profile"):
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                validate_settings({key: ORIGIN})

    def test_disabled_and_publication_gated_runs_need_no_secret(self):
        for source, environment in (("disabled", {}), ("aog", {}), ("json", {"HUNTER_PUBLISH_LIVE_DATA": "true"})):
            with self.subTest(source=source):
                feed, state = run_pipeline({}, source=source, at=NOW, environ=environment, raw_items=[],
                                           aog_collect=lambda *args: self.fail("unexpected source call"))
                self.assertEqual(feed["deals"], [])
                self.assertNotIn("postcode", feed["location"])

    def test_direct_adapter_requires_an_explicit_private_profile(self):
        for settings in ({}, {"postcode": ORIGIN}, {"postcode": ORIGIN, "pickup_search_profile": "invalid"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                collect(settings, NOW, None, transport=lambda *args, **kw: self.fail("unexpected source call"))

    def test_cli_setup_failure_writes_no_snapshot_and_exposes_no_secret_value(self):
        environment = {"HUNTER_PUBLISH_LIVE_DATA": "true", "HUNTER_PRIVATE_SEARCH":
                       json.dumps({"postcode": ORIGIN, "profile_key": KEY, "unexpected": "field"})}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment, clear=True), \
                patch("hunter.aog.collect", side_effect=AssertionError("unexpected source call")):
            root = Path(directory)
            config = root / "config.json"
            config.write_text("{}")
            errors = io.StringIO()
            with redirect_stderr(errors):
                code = main(["--source", "aog", "--config", str(config), "--state-dir", str(root / "runtime"),
                             "--site-dir", str(root / "site")])
            self.assertEqual(code, 1)
            self.assertIn("HUNTER_PRIVATE_SEARCH", errors.getvalue())
            self.assertNotIn(ORIGIN, errors.getvalue())
            self.assertNotIn(KEY, errors.getvalue())
            self.assertFalse((root / "runtime/state.json").exists())
            self.assertFalse((root / "site/api/manifest.json").exists())


class PrivateMigrationTests(unittest.TestCase):
    def test_legacy_state_migrates_before_checkpoint_without_budget_reset(self):
        source = old_state()
        before = copy.deepcopy(source)
        private = load_private_search(env())
        migrated = prepare_state(source, NOW, private=private)
        row = migrated["records"][0]
        self.assertEqual(row["pickup_search_profile"], private.profile)
        self.assertNotIn("pickup_search_postcode", row)
        self.assertEqual(row["pickup_search_observed_at"], before["records"][0]["pickup_search_observed_at"])
        self.assertEqual(row["source_observed_at"], before["records"][0]["source_observed_at"])
        self.assertEqual(migrated["daily_budget"], before["daily_budget"])
        self.assertEqual(migrated["aog"]["requests_today"], 120)
        self.assertEqual(migrated["aog"]["next_allowed_at"], before["aog"]["next_allowed_at"])
        self.assertNotIn(ORIGIN, json.dumps(migrated))
        self.assertEqual(source, before)
        self.assertEqual(content_fingerprint(row), content_fingerprint(before["records"][0]))
        checkpoints = []
        def fail_normalizer(*args, **kwargs):
            raise RuntimeError("synthetic processing failure")
        with self.assertRaises(RuntimeError):
            run(state=source, items=[legacy_item()], normalize=fail_normalizer,
                checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertTrue(checkpoints)
        for checkpoint in checkpoints:
            self.assertNotIn(ORIGIN, json.dumps(checkpoint))
            self.assertEqual(checkpoint["aog"]["next_allowed_at"], before["aog"]["next_allowed_at"])

    def test_missing_context_drops_only_legacy_pickup_claim(self):
        source = old_state()
        migrated = prepare_state(source, NOW)
        row = migrated["records"][0]
        self.assertFalse(row["pickup_search_confirmed"])
        self.assertNotIn("pickup_search_postcode", row)
        self.assertEqual(content_fingerprint(row), content_fingerprint(source["records"][0]))
        self.assertEqual(row["price_eur"], source["records"][0]["price_eur"])
        self.assertEqual(migrated["aog"]["requests_today"], 120)

    def test_actual_seller_location_is_not_a_private_query_field(self):
        private = load_private_search(env())
        item = {"city": "Seller Test City", "postcode": ORIGIN, "pickup_search_postcode": ORIGIN,
                "title": "Fiktives Prüfgerät", "description": "Verkäufertext", "condition": "Gebraucht"}
        migrated = bind_pickup_context(item, private)
        self.assertEqual(migrated["city"], item["city"])
        self.assertEqual(migrated["postcode"], ORIGIN)
        self.assertNotIn("pickup_search_postcode", migrated)
        self.assertEqual(content_fingerprint(migrated), content_fingerprint(item))

    def test_profile_or_key_change_cannot_revalidate_old_evidence(self):
        _, state = run(items=[legacy_item()])
        old_profile = state["records"][0]["pickup_search_profile"]
        for environment in (env("88888"), env(key="22" * 32)):
            with self.subTest(environment=environment):
                feed, changed = run(state=state, environment=environment)
                self.assertEqual(changed["records"][0]["pickup_search_profile"], old_profile)
                self.assertNotIn("Nähe laut Umkreissuche der Quelle", feed["deals"][0]["fact_reasons"])
                with tempfile.TemporaryDirectory() as directory:
                    write_feed(directory, feed)
                    page = json.loads((Path(directory) / "api/candidates/page-001.json").read_text())
                    self.assertFalse(page["items"][0]["pickup_search_profile_match"])

    def test_changed_profile_drops_old_traversal_claim_but_honors_source_cooldown(self):
        private = load_private_search(env())
        changed = load_private_search(env("88888"))
        previous = {"pickup_search_profile": private.profile, "request_day": NOW.date().isoformat(),
                    "requests_today": 120, "next_allowed_at": iso_time(NOW + timedelta(hours=2)),
                    "last_successful_full_scan_at": iso_time(NOW - timedelta(hours=1)),
                    "last_successful_traversals": {"general": iso_time(NOW - timedelta(hours=1)),
                                                   "pickup": iso_time(NOW - timedelta(hours=1))}}
        result = collect({"postcode": changed.postcode, "pickup_search_profile": changed.profile}, NOW, previous,
                         transport=lambda *args, **kw: self.fail("cooldown must prevent every request"))
        self.assertEqual(result["status"], "cooldown")
        self.assertEqual(result["source_state"]["next_allowed_at"], previous["next_allowed_at"])
        self.assertEqual(result["source_state"]["requests_today"], 120)
        self.assertNotIn("pickup", result["source_state"]["last_successful_traversals"])
        self.assertIn("general", result["source_state"]["last_successful_traversals"])
        self.assertIsNone(result["coverage"]["last_successful_full_scan_at"])

    def test_legacy_different_origin_is_preserved_as_a_mismatch(self):
        private = load_private_search(env())
        migrated = bind_pickup_context(legacy_item(pickup_search_postcode="88888"), private)
        self.assertNotEqual(migrated["pickup_search_profile"], private.profile)
        self.assertFalse(matching_origin(migrated, {"pickup_search_profile": private.profile}))

    def test_explicit_invalid_hmac_never_falls_back_to_matching_raw_postcode(self):
        private = load_private_search(env())
        item = legacy_item(pickup_search_profile="invalid")
        migrated = bind_pickup_context(item, private)
        self.assertFalse(migrated["pickup_search_confirmed"])
        self.assertFalse(matching_origin(item, {"postcode": ORIGIN, "pickup_search_profile": private.profile}))

    def test_cached_direct_source_pickup_requires_same_profile(self):
        private = load_private_search(env())
        item = {"local_pickup_confirmed": True, "pickup_verified_at": iso_time(NOW),
                "pickup_search_profile": private.profile, "postcode": "88888", "city": "Seller Test City"}
        self.assertTrue(bind_pickup_context(item, private, cached=True)["local_pickup_confirmed"])
        changed = bind_pickup_context(item, load_private_search(env("88888")), cached=True)
        self.assertFalse(changed["local_pickup_confirmed"])
        self.assertNotIn("pickup_verified_at", changed)
        self.assertEqual(changed["postcode"], "88888")

    def test_public_files_contain_only_derived_match_and_preserve_scores(self):
        feed, state = run(items=[legacy_item()])
        row = state["records"][0]
        private = load_private_search(env())
        self.assertTrue(matching_origin(row, {"pickup_search_profile": private.profile}))
        self.assertNotIn(ORIGIN, json.dumps({"feed": feed, "state": state}))
        self.assertNotIn(KEY, json.dumps({"feed": feed, "state": state}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feed(root, feed)
            for path in root.rglob("*.json"):
                text = path.read_text()
                self.assertNotIn(ORIGIN, text, str(path))
                self.assertNotIn(KEY, text, str(path))
                self.assertNotIn(private.profile, text, str(path))
            page = json.loads((root / "api/candidates/page-001.json").read_text())
            public = page["items"][0]
            self.assertTrue(public["pickup_search_profile_match"])
            for key in ("scalp_score", "useability_score", "interest_score", "score"):
                self.assertEqual(public[key], row[key])
            self.assertEqual(public["pickup_search_observed_at"], row["pickup_search_observed_at"])


if __name__ == "__main__":
    unittest.main()
