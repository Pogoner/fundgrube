import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hunter.reviews import (
    MAX_REVIEW_BYTES, ReviewsError, apply_reviews, content_fingerprint,
    load_reviews, read_json_file, seal_reviews, validate_reviews,
)

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
        "semantic_interest": 82, "value_band": "around_500", "assessment_confidence": "low",
        "product_kind": "apparatus", "value_reason": "Eigenständiges Spezialgerät denkbar; Modell und Zustand sind offen.",
        "reasons": ["Geräteklasse passt zum Interesseprofil"], "risks": ["Nur Titel, keine Beschreibung"],
        "next_checks": ["Modell und Lieferumfang prüfen"],
    }
    result.update(updates)
    return result


def feed(items=None):
    return {"schema_version": 2, "snapshot_id": SNAPSHOT, "generated_at": stamp(NOW - timedelta(minutes=5)),
            "deals": [item()] if items is None else items}


def specification(ids=None, **updates):
    result = {"schema_version": 1, "source_snapshot_id": SNAPSHOT, "reviewed_at": stamp(NOW),
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
        for change in ({"schema_version": True}, {"schema_version": 2}, {"source_snapshot_id": "fake"},
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
                       {"semantic_interest": 101}, {"value_band": "exactly500"}, {"semantic_interest": True}):
            with self.subTest(change=change), self.assertRaises(ReviewsError):
                seal_reviews(feed(), specification(groups=[{"ids": [item()["id"]], "assessment": assessment(**change)}]))

    def test_urls_html_and_code_blocks_rejected(self):
        for value in ("https://example.invalid", "www.example.invalid", "<script>doSomething()</script>",
                      "```python\nrun()\n```", "javascript:alert(1)"):
            with self.subTest(value=value), self.assertRaises(ReviewsError):
                seal_reviews(feed(), specification(groups=[{"ids": [item()["id"]], "assessment": assessment(value_reason=value)}]))

    def test_more_than_eight_hundred_reviews_rejected(self):
        inputs = [item(number) for number in range(801)]
        with self.assertRaises(ReviewsError):
            sealed(inputs)

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
        self.assertLessEqual(result[0]["interest_score"], 15)

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

    def test_unknown_and_ambiguous_current_ids_do_not_apply(self):
        _, metadata = apply_reviews([item(2)], sealed(), NOW)
        self.assertEqual(metadata["skipped_missing"], 1)
        result, metadata = apply_reviews([item(), item()], sealed(), NOW)
        self.assertEqual(result, [item(), item()])
        self.assertEqual(metadata["skipped_changed"], 1)

    def test_future_and_expired_review_rejected(self):
        document = sealed()
        result, metadata = apply_reviews([item()], document, NOW - timedelta(seconds=1))
        self.assertEqual(result, [item()])
        self.assertEqual(metadata["status"], "future")
        at = NOW + timedelta(hours=48, seconds=1)
        source = item(last_seen_at=stamp(at), ends_at=stamp(at + timedelta(hours=1)))
        result, metadata = apply_reviews([source], document, at)
        self.assertEqual(result, [source])
        self.assertEqual(metadata["status"], "expired")
        self.assertEqual(metadata["skipped_stale"], 1)

    def test_exact_forty_eight_hour_boundary_allowed(self):
        at = NOW + timedelta(hours=48)
        source = item(last_seen_at=stamp(at), ends_at=stamp(at + timedelta(hours=1)))
        _, metadata = apply_reviews([source], sealed(), at)
        self.assertEqual(metadata["applied"], 1)

    def test_stale_future_unknown_and_expired_source_skipped(self):
        for change in ({"last_seen_at": stamp(NOW - timedelta(hours=6, seconds=1))},
                       {"last_seen_at": stamp(NOW + timedelta(seconds=1))}, {"last_seen_at": None},
                       {"ends_at": stamp(NOW)}, {"ends_at": None}):
            with self.subTest(change=change):
                source = item(**change)
                result, metadata = apply_reviews([source], sealed(), NOW)
                self.assertEqual(result, [source])
                self.assertEqual(metadata["skipped_stale"], 1)

    def test_newer_api_llm_is_preserved(self):
        source = item(assessment_basis="llm", value_band="around_50", semantic_interest=55,
                      semantic_assessed_at=stamp(NOW + timedelta(minutes=1)))
        result, metadata = apply_reviews([source], sealed(), NOW + timedelta(minutes=2))
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
    def test_load_and_cli_round_trip_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "feed.json").write_text(json.dumps(feed()), encoding="utf-8")
            (root / "ratings.json").write_text(json.dumps(specification()), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "pack_reviews.py"
            process = subprocess.run([sys.executable, str(script), "--feed", str(root / "feed.json"),
                                      "--ratings", str(root / "ratings.json"), "--output", str(root / "reviews" / "current.json")],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            loaded = load_reviews(root / "reviews" / "current.json")
            self.assertEqual(loaded, sealed())

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
