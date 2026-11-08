import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hunter.reviews import (
    MAX_REVIEW_BYTES, MAX_REVIEWS, ReviewsError, apply_reviews, content_fingerprint,
    load_reviews, merge_reviews, read_json_file, seal_reviews, upgrade_reviews, validate_reviews,
)
from scripts.pack_reviews import main as pack_reviews

NOW = datetime(2026, 11, 7, 22, 20, tzinfo=timezone.utc)
SNAPSHOT = "sha256:" + "a" * 64


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def item(number=1, **updates):
    result = {
        "id": f"v1|{100000000000 + number}|0", "title": "Tisch-Autoklav",
        "description": "", "condition": "Gebraucht", "description_truncated": False,
        "last_seen_at": stamp(NOW - timedelta(minutes=10)), "ends_at": stamp(NOW + timedelta(hours=2)),
        "eligibility": "review", "eligibility_reasons": ["Reserve unbekannt"],
        "price_eur": 1.0, "shipping_eur": None, "bid_count": 0, "reserve_status": "unknown",
        "local_pickup_confirmed": False, "pickup_search_confirmed": True, "pickup_search_radius_km": 50,
        "is_auction": True, "price_kind": "auction_price", "fact_score": 25,
        "interest_score": 50, "score": 50, "semantic_assessed": True,
        "assessment_basis": "heuristic", "value_band": "unknown", "assessment_confidence": "low",
        "product_kind": "unknown", "fact_reasons": ["Günstiger Einstieg"],
        "fact_risks": ["Reserve unbekannt"], "reasons": ["Heuristik"], "risks": ["Reserve unbekannt"],
    }
    result.update(updates)
    return result


def assessment(**updates):
    result = {
        "scalp_score": 82, "useability_score": 80, "value_band": "around_500", "assessment_confidence": "low",
        "useability_reason": "Eigenständiger Gerätetyp mit üblichen Betriebsmitteln.",
        "dependencies": ["Passende Betriebsmittel und Anschlüsse"],
        "product_kind": "apparatus", "value_reason": "Eigenständiges Spezialgerät denkbar; Modell und Zustand sind offen.",
        "reasons": ["Geräteklasse passt zum Interesseprofil"], "risks": ["Nur Titel, keine Beschreibung"],
        "next_checks": ["Modell und Lieferumfang prüfen"],
    }
    result.update(updates)
    return result


def legacy_assessment(**updates):
    result = {key: value for key, value in assessment().items()
              if key not in {"scalp_score", "useability_score", "useability_reason", "dependencies"}}
    result["semantic_interest"] = 60
    result.update(updates)
    return result


def feed(items=None):
    return {"schema_version": 2, "snapshot_id": SNAPSHOT, "generated_at": stamp(NOW - timedelta(minutes=5)),
            "deals": [item()] if items is None else items}


def specification(ids=None, **updates):
    result = {"schema_version": 2, "source_snapshot_id": SNAPSHOT, "reviewed_at": stamp(NOW),
              "reviewer": "ChatGPT", "groups": [{"ids": [item()["id"]] if ids is None else ids,
                                                    "assessment": assessment()}]}
    result.update(updates)
    return result


def sealed(items=None, **spec_updates):
    inputs = [item()] if items is None else items
    return seal_reviews(feed(inputs), specification([entry["id"] for entry in inputs], **spec_updates))


class SealReviewsTests(unittest.TestCase):
    def test_group_expansion_and_exact_content_hash(self):
        inputs = [item(1), item(2)]
        document = sealed(inputs)
        self.assertEqual(document["source_snapshot_id"], SNAPSHOT)
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(len(document["reviews"]), 2)
        self.assertEqual(document["reviews"][0]["content_fingerprint"], content_fingerprint(inputs[0]))
        self.assertEqual(set(document["reviews"][0]), {"id", "content_fingerprint", "assessment"})
        self.assertNotIn("title", document["reviews"][0])

    def test_hash_includes_only_exact_semantic_text(self):
        original = content_fingerprint(item())
        self.assertEqual(original, content_fingerprint(item(price_eur=2, shipping_eur=12, bid_count=2, fact_score=0)))
        for changes in ({"title": "Autoklav Ersatzteil"}, {"description": "Nur Zubehör"}, {"condition": "Defekt"}, {"title": "Tisch-Autoklav "}):
            with self.subTest(changes=changes):
                self.assertNotEqual(original, content_fingerprint(item(**changes)))

    def test_different_snapshot_rejected(self):
        with self.assertRaises(ReviewsError):
            seal_reviews(feed(), specification(source_snapshot_id="sha256:" + "b" * 64))

    def test_unknown_and_duplicate_ids_rejected(self):
        for ids in (["missing"], [item()["id"], item()["id"]]):
            with self.assertRaises(ReviewsError):
                seal_reviews(feed(), specification(ids))
        groups = [{"ids": [item()["id"]], "assessment": assessment()}] * 2
        with self.assertRaises(ReviewsError):
            seal_reviews(feed(), specification(groups=groups))
        with self.assertRaises(ReviewsError):
            seal_reviews(feed([item(), item()]), specification())

    def test_malformed_header_rejected(self):
        for change in ({"schema_version": True}, {"schema_version": 3}, {"source_snapshot_id": "fake"},
                       {"reviewer": "heuristic"}, {"reviewed_at": "2026-11-07T22:20:00"},
                       {"reviewed_at": stamp(NOW - timedelta(hours=1))}):
            with self.subTest(change=change), self.assertRaises(ReviewsError):
                seal_reviews(feed(), specification(**change))

    def test_subsecond_review_timestamp_is_not_rounded_before_source(self):
        source = feed()
        source["generated_at"] = stamp(NOW + timedelta(microseconds=100))
        document = seal_reviews(source, specification(reviewed_at=stamp(NOW + timedelta(microseconds=200))))
        self.assertEqual(document["reviewed_at"], stamp(NOW + timedelta(microseconds=200)))

    def test_truncated_summaries_are_not_falsely_bound_to_full_text(self):
        with self.assertRaises(ReviewsError):
            seal_reviews(feed([item(description="partial", description_truncated=True)]), specification())

    def test_fake_prices_and_additional_fields_rejected(self):
        for change in ({"price_eur": 0}, {"eligibility": "eligible"}, {"action": "buy"},
                       {"scalp_score": 101}, {"value_band": "exactly500"}, {"scalp_score": True},
                       {"useability_score": None}, {"useability_score": True}, {"useability_score": -1},
                       {"useability_reason": ""}, {"useability_reason": "x" * 501},
                       {"dependencies": ["x"] * 6}, {"dependencies": ["x" * 241]},
                       {"semantic_interest": 80}, {"score_version": 2}):
            with self.subTest(change=change), self.assertRaises(ReviewsError):
                seal_reviews(feed(), specification(groups=[{"ids": [item()["id"]], "assessment": assessment(**change)}]))

    def test_urls_html_and_code_blocks_rejected(self):
        for value in ("https://example.invalid", "www.example.invalid", "<script>doSomething()</script>",
                      "```python\nrun()\n```", "javascript:alert(1)"):
            for field in ("value_reason", "useability_reason", "dependencies"):
                change = {field: [value] if field == "dependencies" else value}
                with self.subTest(value=value, field=field), self.assertRaises(ReviewsError):
                    seal_reviews(feed(), specification(groups=[{"ids": [item()["id"]], "assessment": assessment(**change)}]))

    def test_legacy_and_modern_envelopes_require_their_exact_assessment_shape(self):
        old_groups = [{"ids": [item()["id"]], "assessment": legacy_assessment()}]
        old = sealed(schema_version=1, groups=old_groups)
        self.assertEqual(old["schema_version"], 1)
        self.assertEqual(old["reviews"][0]["assessment"], legacy_assessment())
        for spec in (specification(schema_version=1), specification(groups=old_groups)):
            with self.subTest(spec=spec), self.assertRaises(ReviewsError):
                seal_reviews(feed(), spec)

    def test_component_kind_is_supported_only_in_modern_review_schema(self):
        new = sealed(groups=[{"ids": [item()["id"]], "assessment": assessment(product_kind="component")}])
        self.assertEqual(new["reviews"][0]["assessment"]["product_kind"], "component")
        with self.assertRaises(ReviewsError):
            sealed(schema_version=1, groups=[{"ids": [item()["id"]], "assessment": legacy_assessment(product_kind="component")}])

    def test_more_than_store_capacity_reviews_rejected(self):
        inputs = [item(number) for number in range(MAX_REVIEWS + 1)]
        with self.assertRaises(ReviewsError):
            sealed(inputs)

    def test_old_eight_hundred_entry_limit_no_longer_discards_valid_reviews(self):
        self.assertEqual(len(sealed([item(number) for number in range(801)])["reviews"]), 801)

    def test_invalid_sealed_hash_or_duplicate_entry_rejected(self):
        for mutation in ("hash", "duplicate", "extra"):
            document = sealed()
            if mutation == "hash":
                document["reviews"][0]["content_fingerprint"] = "wrong"
            elif mutation == "duplicate":
                document["reviews"].append(copy.deepcopy(document["reviews"][0]))
            else:
                document["reviews"][0]["price_eur"] = 0
            with self.subTest(mutation=mutation), self.assertRaises(ReviewsError):
                validate_reviews(document)


class DurableStoreTests(unittest.TestCase):
    def test_upgrade_keeps_original_review_evidence_and_exact_entry_shape(self):
        before = sealed()
        original = copy.deepcopy(before)
        store = upgrade_reviews(before)
        self.assertEqual(before, original)
        self.assertEqual(set(store), {"schema_version", "reviewer", "reviews"})
        self.assertEqual(store["schema_version"], 3)
        self.assertEqual(store["reviewer"], "ChatGPT")
        entry = store["reviews"][0]
        self.assertEqual(set(entry), {"id", "content_fingerprint", "score_version", "assessment",
                                      "reviewed_at", "source_snapshot_id", "ends_at"})
        self.assertEqual(entry["reviewed_at"], original["reviewed_at"])
        self.assertEqual(entry["source_snapshot_id"], original["source_snapshot_id"])
        self.assertEqual(entry["content_fingerprint"], original["reviews"][0]["content_fingerprint"])
        self.assertEqual(entry["assessment"], original["reviews"][0]["assessment"])
        self.assertEqual(entry["score_version"], 2)
        self.assertIsNone(entry["ends_at"])
        self.assertEqual(upgrade_reviews(store), store)

    def test_legacy_upgrade_never_invents_useability(self):
        original = sealed(schema_version=1, groups=[{"ids": [item()["id"]], "assessment": legacy_assessment()}])
        store = upgrade_reviews(original)
        self.assertEqual(store["reviews"][0]["score_version"], 1)
        self.assertEqual(store["reviews"][0]["assessment"], legacy_assessment())
        self.assertNotIn("useability_score", store["reviews"][0]["assessment"])

    def test_mixed_score_versions_keep_their_own_assessment_contract(self):
        old = sealed(schema_version=1, groups=[{"ids": [item()["id"]], "assessment": legacy_assessment()}])
        store = merge_reviews(old, sealed([item(2)]), NOW)
        result, metadata = apply_reviews([item(), item(2)], store, NOW)
        self.assertEqual(metadata["applied"], 2)
        self.assertEqual(result[0]["score_version"], 1)
        self.assertIsNone(result[0]["useability_score"])
        self.assertIsNone(result[0]["interest_score"])
        self.assertEqual(result[1]["score_version"], 2)
        self.assertEqual(result[1]["interest_score"], 66)

    def test_upgrade_can_attach_known_end_without_renewing_review(self):
        original = sealed([item(), item(2)])
        original["reviewed_at"] = stamp(NOW - timedelta(days=5))
        store = upgrade_reviews(original, feed=feed(), at=NOW)
        self.assertEqual(store["reviews"][0]["ends_at"], item()["ends_at"])
        self.assertIsNone(store["reviews"][1]["ends_at"])
        self.assertTrue(all(entry["reviewed_at"] == original["reviewed_at"] for entry in store["reviews"]))

    def test_v3_exact_schema_and_score_version_are_validated(self):
        mutations = ({"schema_version": True}, {"reviewer": "heuristic"}, {"reviewed_at": stamp(NOW)})
        for mutation in mutations:
            document = upgrade_reviews(sealed())
            document.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ReviewsError):
                validate_reviews(document)
        for mutation in ({"score_version": 3}, {"score_version": True}, {"score_version": 1},
                         {"reviewed_at": "2026-11-07T22:20:00"}, {"ends_at": "invalid"},
                         {"ends_at": "2026-11-07T22:20:00"}, {"source_snapshot_id": "fake"},
                         {"price_eur": 0}):
            document = upgrade_reviews(sealed())
            document["reviews"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ReviewsError):
                validate_reviews(document)
        for missing in ("ends_at", "score_version", "reviewed_at", "source_snapshot_id"):
            document = upgrade_reviews(sealed())
            del document["reviews"][0][missing]
            with self.subTest(missing=missing), self.assertRaises(ReviewsError):
                validate_reviews(document)

    def test_new_batch_keeps_unrelated_old_assessment_and_date(self):
        previous = sealed()
        previous["reviewed_at"] = stamp(NOW - timedelta(days=7))
        incoming = sealed([item(2)])
        incoming["source_snapshot_id"] = "sha256:" + "b" * 64
        incoming["reviews"][0]["assessment"] = assessment(scalp_score=99, useability_score=100)
        untouched = copy.deepcopy(previous)
        store = merge_reviews(previous, incoming, NOW)
        self.assertEqual(previous, untouched)
        self.assertEqual(store["reviews"][0], upgrade_reviews(previous)["reviews"][0])
        self.assertEqual(store["reviews"][1], upgrade_reviews(incoming)["reviews"][0])
        result, metadata = apply_reviews([item(), item(2)], store, NOW)
        self.assertEqual(metadata["applied"], 2)
        self.assertEqual(result[0]["interest_score"], 66)
        self.assertEqual(result[0]["semantic_assessed_at"], previous["reviewed_at"])
        self.assertEqual(result[1]["interest_score"], 99)
        self.assertEqual(metadata["oldest_reviewed_at"], previous["reviewed_at"])
        self.assertEqual(metadata["latest_reviewed_at"], incoming["reviewed_at"])
        self.assertIsNone(metadata["source_snapshot_id"])

    def test_repeated_import_is_idempotent_and_preserves_lifecycle_hint(self):
        document = sealed()
        first = merge_reviews(None, document, NOW, feed=feed())
        self.assertEqual(merge_reviews(first, document, NOW), first)
        self.assertEqual(merge_reviews(first, document, NOW, feed=feed()), first)
        self.assertEqual(merge_reviews(first, first, NOW), first)

    def test_newer_assessment_wins_and_older_import_never_overwrites(self):
        old = sealed()
        old["reviewed_at"] = stamp(NOW - timedelta(hours=2))
        old = upgrade_reviews(old, feed=feed(), at=NOW)
        current = sealed()
        current["reviews"][0]["assessment"] = assessment(scalp_score=95)
        merged = merge_reviews(old, current, NOW)
        self.assertEqual(merged["reviews"][0]["assessment"]["scalp_score"], 95)
        self.assertEqual(merged["reviews"][0]["reviewed_at"], stamp(NOW))
        self.assertEqual(merged["reviews"][0]["ends_at"], item()["ends_at"])
        self.assertEqual(merge_reviews(merged, old, NOW), merged)

    def test_conflicting_equal_time_assessment_content_or_provenance_fails_closed(self):
        old = upgrade_reviews(sealed())
        for field, value in (("assessment", assessment(scalp_score=90)),
                             ("content_fingerprint", "sha256:" + "f" * 64),
                             ("source_snapshot_id", "sha256:" + "b" * 64)):
            new = copy.deepcopy(old)
            new["reviews"][0][field] = value
            before = copy.deepcopy(old)
            with self.subTest(field=field), self.assertRaises(ReviewsError):
                merge_reviews(old, new, NOW)
            self.assertEqual(old, before)

    def test_merge_rejects_future_or_malformed_entries_atomically(self):
        old = upgrade_reviews(sealed())
        new = upgrade_reviews(sealed([item(2), item(3)]))
        new["reviews"][1]["reviewed_at"] = stamp(NOW + timedelta(seconds=1))
        with self.assertRaises(ReviewsError):
            merge_reviews(old, new, NOW)
        new["reviews"][1]["reviewed_at"] = stamp(NOW)
        new["reviews"][1]["assessment"]["price_eur"] = 0
        with self.assertRaises(ReviewsError):
            merge_reviews(old, new, NOW)
        self.assertEqual(old, upgrade_reviews(sealed()))

    def test_merge_prunes_only_explicit_ends_not_absence(self):
        old = upgrade_reviews(sealed([item(), item(2), item(3)]))
        old["reviews"][0]["ends_at"] = stamp(NOW)
        old["reviews"][1]["ends_at"] = stamp(NOW + timedelta(minutes=1))
        new = {"schema_version": 3, "reviewer": "ChatGPT", "reviews": []}
        store = merge_reviews(old, new, NOW, feed=feed([]))
        self.assertEqual([entry["id"] for entry in store["reviews"]], [item(2)["id"], item(3)["id"]])
        self.assertIsNone(store["reviews"][1]["ends_at"])

    def test_current_known_end_extension_is_applied_before_pruning(self):
        old = upgrade_reviews(sealed())
        old["reviews"][0]["ends_at"] = stamp(NOW - timedelta(minutes=1))
        old["reviews"][0]["reviewed_at"] = stamp(NOW - timedelta(days=3))
        new = {"schema_version": 3, "reviewer": "ChatGPT", "reviews": []}
        store = merge_reviews(old, new, NOW, feed=feed())
        self.assertEqual(len(store["reviews"]), 1)
        self.assertEqual(store["reviews"][0]["ends_at"], item()["ends_at"])
        self.assertEqual(store["reviews"][0]["reviewed_at"], old["reviews"][0]["reviewed_at"])
        self.assertEqual(store["reviews"][0]["source_snapshot_id"], old["reviews"][0]["source_snapshot_id"])

    def test_equal_assessments_reconcile_end_hints_without_shortening_retention(self):
        old = upgrade_reviews(sealed(), feed=feed(), at=NOW)
        new = copy.deepcopy(old)
        new["reviews"][0]["ends_at"] = stamp(NOW + timedelta(hours=3))
        for source in (None, feed([]), feed([item(last_seen_at=None)]), feed()):
            with self.subTest(source=source):
                merged = merge_reviews(old, new, NOW, feed=source)
                self.assertEqual(merged, new)
                self.assertEqual(merge_reviews(new, old, NOW, feed=source), new)

    def test_old_or_shorter_feed_cannot_shorten_known_end_in_upgrade_or_merge(self):
        original = upgrade_reviews(sealed(), feed=feed(), at=NOW)
        empty = {"schema_version": 3, "reviewer": "ChatGPT", "reviews": []}
        for age in (timedelta(days=1), timedelta(0)):
            stale = feed([item(last_seen_at=stamp(NOW - age), ends_at=stamp(NOW - timedelta(hours=1)))])
            stale["generated_at"] = stamp(NOW - age)
            with self.subTest(age=age):
                self.assertEqual(upgrade_reviews(original, feed=stale, at=NOW), original)
                self.assertEqual(merge_reviews(original, empty, NOW, feed=stale), original)
        self.assertEqual(original, upgrade_reviews(sealed(), feed=feed(), at=NOW))

    def test_newer_assessment_cannot_shorten_known_lifecycle_end(self):
        old = upgrade_reviews(sealed(), feed=feed(), at=NOW)
        old["reviews"][0]["reviewed_at"] = stamp(NOW - timedelta(hours=2))
        new = upgrade_reviews(sealed())
        new["reviews"][0]["assessment"] = assessment(scalp_score=94)
        new["reviews"][0]["ends_at"] = stamp(NOW - timedelta(hours=1))
        merged = merge_reviews(old, new, NOW)
        self.assertEqual(len(merged["reviews"]), 1)
        self.assertEqual(merged["reviews"][0]["ends_at"], old["reviews"][0]["ends_at"])
        self.assertEqual(merged["reviews"][0]["assessment"], new["reviews"][0]["assessment"])
        self.assertEqual(merged["reviews"][0]["reviewed_at"], new["reviews"][0]["reviewed_at"])

    def test_older_assessment_can_extend_lifecycle_without_replacing_newer_review(self):
        old = upgrade_reviews(sealed())
        old["reviews"][0]["reviewed_at"] = stamp(NOW - timedelta(days=1))
        old["reviews"][0]["ends_at"] = stamp(NOW + timedelta(hours=4))
        new = upgrade_reviews(sealed(), feed=feed(), at=NOW)
        new["reviews"][0]["assessment"] = assessment(scalp_score=93)
        merged = merge_reviews(new, old, NOW)
        self.assertEqual(merged["reviews"][0]["ends_at"], old["reviews"][0]["ends_at"])
        self.assertEqual(merged["reviews"][0]["reviewed_at"], new["reviews"][0]["reviewed_at"])
        self.assertEqual(merged["reviews"][0]["assessment"], new["reviews"][0]["assessment"])
        self.assertEqual(merge_reviews(old, new, NOW), merged)

    def test_stale_past_feed_hint_cannot_newly_bind_unknown_end_on_newer_review(self):
        original = upgrade_reviews(sealed())
        stale = feed([item(last_seen_at=stamp(NOW - timedelta(days=1)), ends_at=stamp(NOW - timedelta(hours=1)))])
        stale["generated_at"] = stamp(NOW - timedelta(days=1))
        empty = {"schema_version": 3, "reviewer": "ChatGPT", "reviews": []}
        self.assertEqual(upgrade_reviews(original, feed=stale, at=NOW), original)
        self.assertEqual(merge_reviews(original, empty, NOW, feed=stale), original)

    def test_older_stored_past_hint_cannot_delete_newer_review_without_known_end(self):
        old = upgrade_reviews(sealed())
        old["reviews"][0]["reviewed_at"] = stamp(NOW - timedelta(days=1))
        old["reviews"][0]["ends_at"] = stamp(NOW - timedelta(hours=1))
        new = upgrade_reviews(sealed())
        for existing, incoming in ((old, new), (new, old)):
            with self.subTest(existing=existing):
                self.assertEqual(merge_reviews(existing, incoming, NOW), new)

    def test_nonstale_past_feed_hint_can_establish_known_end_and_prune(self):
        original = upgrade_reviews(sealed())
        original["reviews"][0]["reviewed_at"] = stamp(NOW - timedelta(hours=3))
        source = feed([item(last_seen_at=stamp(NOW - timedelta(minutes=10)), ends_at=stamp(NOW - timedelta(hours=1)))])
        empty = {"schema_version": 3, "reviewer": "ChatGPT", "reviews": []}
        upgraded = upgrade_reviews(original, feed=source, at=NOW)
        self.assertEqual(upgraded["reviews"][0]["ends_at"], source["deals"][0]["ends_at"])
        self.assertEqual(upgraded["reviews"][0]["reviewed_at"], original["reviews"][0]["reviewed_at"])
        self.assertEqual(merge_reviews(original, empty, NOW, feed=source), empty)

    def test_old_source_observation_bound_cannot_be_hidden_by_fresh_snapshot_time(self):
        original = upgrade_reviews(sealed())
        source = feed([item(last_seen_at=stamp(NOW), source_observed_at=stamp(NOW - timedelta(days=1)),
                            ends_at=stamp(NOW - timedelta(hours=1)))])
        source["generated_at"] = stamp(NOW)
        self.assertEqual(upgrade_reviews(original, feed=source, at=NOW), original)
        source["deals"][0].pop("source_observed_at")
        source["generated_at"] = stamp(NOW - timedelta(days=1))
        self.assertEqual(upgrade_reviews(original, feed=source, at=NOW), original)

    def test_invalid_or_future_feed_fact_times_fail_closed(self):
        sources = [feed([item(last_seen_at="invalid")]), feed([item(last_seen_at=stamp(NOW + timedelta(seconds=1)))]),
                   feed([item(source_observed_at="invalid")]),
                   feed([item(source_observed_at=stamp(NOW + timedelta(seconds=1)))]),
                   feed([item(ends_at="2026-11-08T12:00:00")]), feed([item(), item()])]
        for field, value in (("generated_at", stamp(NOW + timedelta(seconds=1))),
                             ("generated_at", "invalid"), ("snapshot_id", "fake")):
            source = feed()
            source[field] = value
            sources.append(source)
        for source in sources:
            with self.subTest(source=source), self.assertRaises(ReviewsError):
                merge_reviews(None, sealed(), NOW, feed=source)

    def test_missing_source_time_does_not_invent_end_evidence(self):
        for updates in ({"last_seen_at": None}, {"ends_at": None}):
            store = upgrade_reviews(sealed(), feed=feed([item(**updates)]), at=NOW)
            self.assertIsNone(store["reviews"][0]["ends_at"])

    def test_full_store_merge_fails_without_silent_truncation_and_can_prune_first(self):
        old = upgrade_reviews(sealed([item(), item(2)]))
        new = upgrade_reviews(sealed([item(3)]))
        with patch("hunter.reviews.MAX_REVIEWS", 2):
            with self.assertRaises(ReviewsError):
                merge_reviews(old, new, NOW)
            old["reviews"][0]["ends_at"] = stamp(NOW)
            merged = merge_reviews(old, new, NOW)
            self.assertEqual([entry["id"] for entry in merged["reviews"]], [item(2)["id"], item(3)["id"]])


class ApplyReviewsTests(unittest.TestCase):
    def test_valid_review_annotates_with_interactive_provenance(self):
        source = item()
        before = copy.deepcopy(source)
        result, metadata = apply_reviews([source], sealed(), NOW)
        self.assertEqual(source, before)
        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(result[0]["assessment_basis"], "llm")
        self.assertEqual(result[0]["semantic_review_mode"], "interactive")
        self.assertEqual(result[0]["semantic_assessed_at"], stamp(NOW))
        self.assertEqual(result[0]["value_band"], "around_500")
        self.assertEqual(result[0]["score_version"], 2)
        self.assertEqual(result[0]["scalp_score"], 82)
        self.assertEqual(result[0]["semantic_interest"], 82)
        self.assertEqual(result[0]["useability_score"], 80)
        self.assertEqual(result[0]["interest_score"], 66)
        self.assertEqual(result[0]["score"], 66)
        self.assertEqual(result[0]["useability_reason"], assessment()["useability_reason"])
        self.assertEqual(result[0]["dependencies"], assessment()["dependencies"])
        self.assertEqual(result[0]["eligibility"], "review")
        self.assertIn("Reserve unbekannt", result[0]["risks"])

    def test_source_facts_and_newer_logistics_are_never_overwritten(self):
        source = item(price_eur=5, shipping_eur=7, bid_count=1, eligibility="excluded", fact_score=3)
        result, metadata = apply_reviews([source], sealed(), NOW)
        self.assertEqual(metadata["applied"], 1)
        for field in ("id", "title", "description", "condition", "price_eur", "shipping_eur", "bid_count",
                      "eligibility", "eligibility_reasons", "reserve_status", "last_seen_at", "ends_at",
                      "local_pickup_confirmed", "pickup_search_radius_km", "fact_score"):
            self.assertEqual(result[0][field], source[field], field)
        self.assertEqual(result[0]["interest_score"], 66)

    def test_helper_regression_cannot_overwrite_facts(self):
        source = item()
        with patch("hunter.reviews.apply_semantic", return_value={**source, "price_eur": 0,
                                                                  "bid_count": 0, "eligibility": "eligible",
                                                                  "semantic_interest": 99}):
            result, _ = apply_reviews([source], sealed(), NOW)
        self.assertEqual(result[0]["price_eur"], source["price_eur"])
        self.assertEqual(result[0]["eligibility"], source["eligibility"])

    def test_changed_text_does_not_apply(self):
        for change in ({"title": "Nur Gehäuse"}, {"description": "Keine Funktion"}, {"condition": "Defekt"}):
            with self.subTest(change=change):
                source = item(**change)
                result, metadata = apply_reviews([source], sealed(), NOW)
                self.assertEqual(result, [source])
                self.assertEqual(metadata["skipped_changed"], 1)
                self.assertEqual(metadata["status"], "partial")

    def test_expired_description_changes_evidence_not_review_lifetime(self):
        original = item(description="Vollständiges Gerät laut Verkäufer")
        document = sealed([original])
        current = item(description="")
        result, metadata = apply_reviews([current], document, NOW)
        self.assertEqual(result, [current])
        self.assertEqual(metadata["skipped_changed"], 1)
        self.assertEqual(metadata["skipped_stale"], 0)
        self.assertIsNone(metadata["expires_at"])

    def test_unknown_and_ambiguous_current_ids_do_not_apply(self):
        _, metadata = apply_reviews([item(2)], sealed(), NOW)
        self.assertEqual(metadata["skipped_missing"], 1)
        result, metadata = apply_reviews([item(), item()], sealed(), NOW)
        self.assertEqual(result, [item(), item()])
        self.assertEqual(metadata["skipped_changed"], 1)

    def test_future_review_is_rejected_but_old_unchanged_review_does_not_expire(self):
        document = sealed()
        result, metadata = apply_reviews([item()], document, NOW - timedelta(seconds=1))
        self.assertEqual(result, [item()])
        self.assertEqual(metadata["status"], "future")
        at = NOW + timedelta(hours=48, seconds=1)
        source = item(last_seen_at=stamp(at), ends_at=stamp(at + timedelta(hours=1)))
        result, metadata = apply_reviews([source], document, at)
        self.assertEqual(result[0]["semantic_assessed_at"], stamp(NOW))
        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(metadata["applied"], 1)
        self.assertIsNone(metadata["expires_at"])
        self.assertEqual(metadata["retention_basis"], "content_and_auction")

    def test_semantic_age_has_no_boundary_while_current_auction_remains_active(self):
        for version in (1, 2, 3):
            for age in (timedelta(hours=48), timedelta(hours=49), timedelta(days=7), timedelta(days=30)):
                document = sealed()
                if version == 1:
                    document = sealed(schema_version=1, groups=[{"ids": [item()["id"]], "assessment": legacy_assessment()}])
                elif version == 3:
                    document = upgrade_reviews(document)
                at = NOW + age
                source = item(last_seen_at=stamp(at), ends_at=stamp(at + timedelta(hours=1)))
                with self.subTest(version=version, age=age):
                    result, metadata = apply_reviews([source], document, at)
                    self.assertEqual(metadata["applied"], 1)
                    self.assertEqual(result[0]["semantic_assessed_at"], stamp(NOW))

    def test_future_entry_does_not_poison_valid_past_entry_or_renew_dates(self):
        document = upgrade_reviews(sealed([item(), item(2)]))
        document["reviews"][0]["reviewed_at"] = stamp(NOW - timedelta(days=3))
        document["reviews"][1]["reviewed_at"] = stamp(NOW + timedelta(seconds=1))
        result, metadata = apply_reviews([item(), item(2)], document, NOW)
        self.assertEqual(result[0]["semantic_assessed_at"], stamp(NOW - timedelta(days=3)))
        self.assertEqual(result[1], item(2))
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(metadata["skipped_future"], 1)
        self.assertEqual(metadata["reviewed_at"], stamp(NOW - timedelta(days=3)))
        self.assertEqual(metadata["oldest_reviewed_at"], metadata["latest_reviewed_at"])

    def test_store_end_hint_never_substitutes_for_current_auction_facts(self):
        document = upgrade_reviews(sealed())
        document["reviews"][0]["ends_at"] = stamp(NOW + timedelta(days=5))
        for source in (item(ends_at=None), item(ends_at=stamp(NOW)),
                       item(last_seen_at=stamp(NOW + timedelta(seconds=1)))):
            with self.subTest(source=source):
                result, metadata = apply_reviews([source], document, NOW)
                self.assertEqual(result, [source])
                self.assertEqual(metadata["skipped_stale"], 1)
        document["reviews"][0]["ends_at"] = stamp(NOW - timedelta(days=1))
        result, metadata = apply_reviews([item()], document, NOW)
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(result[0]["ends_at"], item()["ends_at"])

    def test_truncated_current_description_cannot_claim_a_full_text_match(self):
        source = item(description_truncated=True)
        result, metadata = apply_reviews([source], sealed(), NOW)
        self.assertEqual(result, [source])
        self.assertEqual(metadata["skipped_changed"], 1)

    def test_future_unknown_and_expired_source_skipped(self):
        for change in ({"last_seen_at": stamp(NOW + timedelta(seconds=1))}, {"last_seen_at": None},
                       {"ends_at": stamp(NOW)}, {"ends_at": None}):
            with self.subTest(change=change):
                source = item(**change)
                result, metadata = apply_reviews([source], sealed(), NOW)
                self.assertEqual(result, [source])
                self.assertEqual(metadata["skipped_stale"], 1)

    def test_old_observations_keep_valid_review_without_revalidation(self):
        reviewed = NOW - timedelta(hours=2)
        for age in (timedelta(hours=7), timedelta(hours=23), timedelta(hours=24), timedelta(days=7)):
            with self.subTest(age=age):
                source = item(last_seen_at=stamp(NOW - age), stale=True, carried=True)
                snapshot = feed([source])
                snapshot["generated_at"] = stamp(reviewed - timedelta(minutes=5))
                document = seal_reviews(snapshot, specification(reviewed_at=stamp(reviewed)))
                result, metadata = apply_reviews([source], document, NOW)
                self.assertEqual(metadata["applied"], 1)
                self.assertEqual(result[0]["last_seen_at"], source["last_seen_at"])
                self.assertEqual(result[0]["semantic_assessed_at"], stamp(reviewed))
                self.assertTrue(result[0]["stale"])
                self.assertTrue(result[0]["carried"])
                self.assertEqual(result[0]["eligibility"], source["eligibility"])

    def test_source_retention_and_review_lifetime_are_independent(self):
        document = sealed()
        later = NOW + timedelta(hours=47)
        source = item(last_seen_at=stamp(later - timedelta(hours=23)),
                      ends_at=stamp(later + timedelta(hours=1)), stale=True)
        result, metadata = apply_reviews([source], document, later)
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(result[0]["semantic_assessed_at"], stamp(NOW))
        self.assertEqual(result[0]["last_seen_at"], source["last_seen_at"])
        self.assertTrue(result[0]["stale"])

    def test_newer_api_llm_is_preserved(self):
        source = item(assessment_basis="llm", value_band="around_50", semantic_interest=55,
                      score_version=2, scalp_score=55, useability_score=90,
                      semantic_assessed_at=stamp(NOW + timedelta(minutes=1)))
        result, metadata = apply_reviews([source], sealed(), NOW + timedelta(minutes=2))
        self.assertEqual(result, [source])
        self.assertEqual(metadata["skipped_newer"], 1)

    def test_newest_batch_date_does_not_allow_old_entry_to_replace_newer_api(self):
        old = sealed()
        old["reviewed_at"] = stamp(NOW - timedelta(days=5))
        store = merge_reviews(old, sealed([item(2)]), NOW)
        source = item(assessment_basis="llm", semantic_assessed_at=stamp(NOW - timedelta(days=1)),
                      scalp_score=55, useability_score=90, value_band="around_50")
        result, metadata = apply_reviews([source, item(2)], store, NOW)
        self.assertEqual(result[0], source)
        self.assertEqual(metadata["skipped_newer"], 1)
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(metadata["reviewed_at"], stamp(NOW))

    def test_legacy_review_preserves_old_scalp_but_does_not_claim_dual_review(self):
        document = sealed(schema_version=1, groups=[{"ids": [item()["id"]], "assessment": legacy_assessment()}])
        source = item(score_version=2, scalp_score=78, useability_score=80,
                      useability_reason="Heuristische Eigenständigkeit", dependencies=["Betriebsmittel"])
        result, metadata = apply_reviews([source], document, NOW)
        row = result[0]
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(row["score_version"], 1)
        self.assertEqual(row["scalp_score"], 67)  # Old 60*.7 + 25 facts, not raw 60.
        self.assertEqual(row["semantic_interest"], 67)
        self.assertIsNone(row["useability_score"])
        self.assertIsNone(row["interest_score"])
        self.assertIsNone(row["score"])
        self.assertEqual(row["dependencies"], [])
        self.assertIn("noch nicht beurteilt", row["useability_reason"])
        self.assertEqual(row["semantic_assessed_at"], stamp(NOW))
        self.assertEqual(row["assessment_basis"], "llm")
        self.assertEqual(row["price_eur"], source["price_eur"])

    def test_legacy_review_cannot_displace_newer_dual_api_review(self):
        document = sealed(schema_version=1, groups=[{"ids": [item()["id"]], "assessment": legacy_assessment()}])
        source = item(score_version=2, scalp_score=80, useability_score=75, assessment_basis="llm",
                      semantic_assessed_at=stamp(NOW + timedelta(minutes=1)))
        result, metadata = apply_reviews([source], document, NOW + timedelta(minutes=2))
        self.assertEqual(result, [source])
        self.assertEqual(metadata["skipped_newer"], 1)

    def test_older_llm_can_be_replaced_by_interactive_review(self):
        source = item(assessment_basis="llm", semantic_assessed_at=stamp(NOW - timedelta(minutes=1)))
        result, metadata = apply_reviews([source], sealed(), NOW)
        self.assertEqual(metadata["applied"], 1)
        self.assertEqual(result[0]["semantic_review_mode"], "interactive")

    def test_invalid_document_is_atomic_and_absent_is_normal(self):
        document = sealed([item(), item(2)])
        document["reviews"][1]["assessment"]["price_eur"] = 0
        inputs = [item(), item(2)]
        result, metadata = apply_reviews(inputs, document, NOW)
        self.assertEqual(result, inputs)
        self.assertEqual(metadata["status"], "invalid")
        self.assertEqual(metadata["applied"], 0)
        result, metadata = apply_reviews(inputs, None, NOW)
        self.assertEqual(result, inputs)
        self.assertEqual(metadata["status"], "absent")


class ReviewFileTests(unittest.TestCase):
    def run_cli(self, root, *, existing=None, output=None):
        arguments = ["--feed", str(root / "feed.json"), "--ratings", str(root / "ratings.json"),
                     "--output", str(output if output is not None else root / "current.json")]
        if existing is not None:
            arguments.extend(["--existing", str(existing)])
        with patch("scripts.pack_reviews.datetime") as clock, patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            clock.now.return_value = NOW
            return pack_reviews(arguments)

    def write_inputs(self, root, inputs=None, spec=None):
        (root / "feed.json").write_text(json.dumps(feed(inputs)), encoding="utf-8")
        (root / "ratings.json").write_text(json.dumps(specification() if spec is None else spec), encoding="utf-8")

    def test_load_and_cli_round_trip_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # This subprocess smoke test uses real current times; the merge
            # regression tests below pin time in-process for repeatability.
            at = datetime.now(timezone.utc)
            source = feed([item(last_seen_at=stamp(at - timedelta(minutes=10)),
                                ends_at=stamp(at + timedelta(hours=2)))])
            source["generated_at"] = stamp(at - timedelta(minutes=5))
            spec = specification(reviewed_at=stamp(at))
            (root / "feed.json").write_text(json.dumps(source), encoding="utf-8")
            (root / "ratings.json").write_text(json.dumps(spec), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "pack_reviews.py"
            process = subprocess.run([sys.executable, str(script), "--feed", str(root / "feed.json"),
                                      "--ratings", str(root / "ratings.json"), "--output", str(root / "reviews" / "current.json")],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            loaded = load_reviews(root / "reviews" / "current.json")
            self.assertEqual(loaded, upgrade_reviews(seal_reviews(source, spec), feed=source, at=at))

    def test_cli_default_merges_old_and_new_reviews_without_renewing_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = sealed()
            old["reviewed_at"] = stamp(NOW - timedelta(days=7))
            output = root / "current.json"
            output.write_text(json.dumps(old), encoding="utf-8")
            self.write_inputs(root, [item(2)], specification([item(2)["id"]]))
            self.assertEqual(self.run_cli(root), 0)
            merged = load_reviews(output)
            self.assertEqual(len(merged["reviews"]), 2)
            self.assertEqual(merged["reviews"][0], upgrade_reviews(old)["reviews"][0])
            self.assertEqual(merged["reviews"][1]["ends_at"], item(2)["ends_at"])
            first_bytes = output.read_bytes()
            self.assertEqual(self.run_cli(root), 0)
            self.assertEqual(output.read_bytes(), first_bytes)

    def test_cli_explicit_existing_adds_to_output_without_dropping_unrelated_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_inputs(root, [item(3)], specification([item(3)["id"]]))
            (root / "current.json").write_text(json.dumps(sealed()), encoding="utf-8")
            additional = root / "additional.json"
            additional.write_text(json.dumps(sealed([item(2)])), encoding="utf-8")
            unchanged = additional.read_bytes()
            self.assertEqual(self.run_cli(root, existing=additional), 0)
            merged = load_reviews(root / "current.json")
            self.assertEqual([entry["id"] for entry in merged["reviews"]], [item(n)["id"] for n in (1, 2, 3)])
            self.assertEqual(additional.read_bytes(), unchanged)
            self.assertEqual(self.run_cli(root, existing=root / "current.json"), 0)

    def test_cli_new_output_can_use_explicit_existing_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_inputs(root, [item(2)], specification([item(2)["id"]]))
            base = root / "base.json"
            base.write_text(json.dumps(sealed()), encoding="utf-8")
            self.assertEqual(self.run_cli(root, existing=base), 0)
            self.assertEqual(len(load_reviews(root / "current.json")["reviews"]), 2)

    def test_cli_conflict_or_future_review_leaves_valid_store_unchanged(self):
        for mutation in ("conflict", "future", "malformed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = specification()
                if mutation == "conflict":
                    spec["groups"][0]["assessment"]["scalp_score"] = 91
                elif mutation == "future":
                    spec["reviewed_at"] = stamp(NOW + timedelta(seconds=1))
                else:
                    spec["groups"][0]["assessment"]["price_eur"] = 0
                self.write_inputs(root, spec=spec)
                output = root / "current.json"
                output.write_text(json.dumps(sealed()), encoding="utf-8")
                unchanged = output.read_bytes()
                self.assertEqual(self.run_cli(root), 2)
                self.assertEqual(output.read_bytes(), unchanged)
                self.assertFalse(list(root.glob(".fundgrube-reviews-*.tmp")))

    def test_cli_valid_new_batch_cannot_replace_an_invalid_existing_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_inputs(root)
            output = root / "current.json"
            output.write_text("unrecognized existing content", encoding="utf-8")
            unchanged = output.read_bytes()
            self.assertEqual(self.run_cli(root), 2)
            self.assertEqual(output.read_bytes(), unchanged)

    def test_cli_updates_known_end_before_pruning_and_keeps_absent_unknown_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = upgrade_reviews(sealed([item(), item(2), item(3)]))
            previous["reviews"][0]["ends_at"] = stamp(NOW)
            previous["reviews"][1]["ends_at"] = stamp(NOW)
            (root / "current.json").write_text(json.dumps(previous), encoding="utf-8")
            self.write_inputs(root, [item()], specification(groups=[]))
            self.assertEqual(self.run_cli(root), 0)
            merged = load_reviews(root / "current.json")
            self.assertEqual([entry["id"] for entry in merged["reviews"]], [item()["id"], item(3)["id"]])
            self.assertEqual(merged["reviews"][0]["ends_at"], item()["ends_at"])
            self.assertEqual(merged["reviews"][0]["reviewed_at"], stamp(NOW))

    def test_cli_stale_feed_does_not_prune_newer_review_or_shorten_known_end(self):
        for with_known_end in (False, True):
            with self.subTest(with_known_end=with_known_end), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = upgrade_reviews(sealed(), feed=feed() if with_known_end else None, at=NOW)
                output = root / "current.json"
                output.write_text(json.dumps(original), encoding="utf-8")
                stale = feed([item(last_seen_at=stamp(NOW - timedelta(days=1)),
                                   ends_at=stamp(NOW - timedelta(hours=1)))])
                stale["generated_at"] = stamp(NOW - timedelta(days=1))
                (root / "feed.json").write_text(json.dumps(stale), encoding="utf-8")
                (root / "ratings.json").write_text(json.dumps(specification(groups=[])), encoding="utf-8")
                self.assertEqual(self.run_cli(root), 0)
                self.assertEqual(load_reviews(output), original)

    def test_cli_atomic_replace_failure_keeps_old_store_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_inputs(root, [item(2)], specification([item(2)["id"]]))
            output = root / "current.json"
            output.write_text(json.dumps(sealed()), encoding="utf-8")
            unchanged = output.read_bytes()
            with patch("scripts.pack_reviews.os.replace", side_effect=OSError("do not leak details")):
                self.assertEqual(self.run_cli(root), 2)
            self.assertEqual(output.read_bytes(), unchanged)
            self.assertFalse(list(root.glob(".fundgrube-reviews-*.tmp")))

    def test_cli_rejects_symlinked_inputs_output_and_ancestors(self):
        for linked in ("feed", "ratings", "output", "existing", "ancestor"):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_inputs(root)
                existing = root / "base.json"
                existing.write_text(json.dumps(sealed()), encoding="utf-8")
                additional = None
                output = root / "current.json"
                if linked in {"feed", "ratings"}:
                    path = root / f"{linked}.json"
                    real = root / f"real-{linked}.json"
                    path.rename(real)
                    path.symlink_to(real)
                elif linked == "output":
                    output.symlink_to(existing)
                elif linked == "existing":
                    additional = root / "base-link.json"
                    additional.symlink_to(existing)
                else:
                    actual = root / "actual"
                    actual.mkdir()
                    alias = root / "alias"
                    alias.symlink_to(actual, target_is_directory=True)
                    output = alias / "nested" / "current.json"
                unchanged = existing.read_bytes()
                self.assertEqual(self.run_cli(root, existing=additional, output=output), 2)
                self.assertEqual(existing.read_bytes(), unchanged)

    def test_cli_rejects_hardlink_to_an_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_inputs(root)
            output = root / "current.json"
            output.hardlink_to(root / "feed.json")
            unchanged = (root / "feed.json").read_bytes()
            self.assertEqual(self.run_cli(root), 2)
            self.assertEqual((root / "feed.json").read_bytes(), unchanged)

    def test_oversized_duplicate_field_and_nonfinite_json_rejected(self):
        contents = ["x" * (MAX_REVIEW_BYTES + 1), '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}']
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "review.json"
            for content in contents:
                target.write_text(content, encoding="utf-8")
                with self.subTest(content=content[:30]), self.assertRaises(ReviewsError):
                    read_json_file(target)

    def test_symbolic_link_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(sealed()), encoding="utf-8")
            target = root / "link.json"
            target.symlink_to(source)
            with self.assertRaises(ReviewsError):
                load_reviews(target)
            actual = root / "actual"
            actual.mkdir()
            (actual / "review.json").write_text(json.dumps(sealed()), encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(ReviewsError):
                load_reviews(alias / "review.json")

    def test_failed_cli_leaves_existing_output_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "feed.json").write_text(json.dumps(feed()), encoding="utf-8")
            (root / "ratings.json").write_text(json.dumps(specification(["not-in-feed"])), encoding="utf-8")
            output = root / "current.json"
            output.write_text("unchanged", encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "pack_reviews.py"
            process = subprocess.run([sys.executable, str(script), "--feed", str(root / "feed.json"),
                                      "--ratings", str(root / "ratings.json"), "--output", str(output)],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "unchanged")

    def test_cli_cannot_overwrite_its_source_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_text = json.dumps(feed())
            source = root / "feed.json"
            source.write_text(source_text, encoding="utf-8")
            (root / "ratings.json").write_text(json.dumps(specification()), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "pack_reviews.py"
            process = subprocess.run([sys.executable, str(script), "--feed", str(source),
                                      "--ratings", str(root / "ratings.json"), "--output", str(source)],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(source.read_text(encoding="utf-8"), source_text)


if __name__ == "__main__":
    unittest.main()
