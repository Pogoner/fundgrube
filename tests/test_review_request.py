import base64
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hunter.reviews import ReviewsError, content_fingerprint
from scripts.github_snapshot import GitHubError, SnapshotError
from scripts import process_review_request as worker

NOW = datetime.now(timezone.utc)
HEAD, SOURCE, TREE, BLOB, NEW_TREE, COMMIT = (character * 40 for character in "abcdef")
SNAPSHOT = "sha256:" + "1" * 64


def stamp(at):
    return at.isoformat().replace("+00:00", "Z")


def offer(number):
    return {
        "id": f"v1|{100000000000 + number}|0", "title": "Tischgerät",
        "description": "", "condition": "Gebraucht", "description_truncated": False,
        "last_seen_at": stamp(NOW - timedelta(minutes=10)),
        "ends_at": stamp(NOW + timedelta(hours=2)),
    }


def assessment():
    return {
        "scalp_score": 52, "useability_score": 90,
        "useability_reason": "Mit üblicher Versorgung eigenständig verwendbar.",
        "dependencies": ["Stromversorgung"], "value_band": "around_50",
        "assessment_confidence": "low", "product_kind": "apparatus",
        "value_reason": "Vollständiges Tischgerät mit unsicherem Zustand.",
        "reasons": ["Eigenständiges Gerät"], "risks": ["Funktion nicht geprüft"],
        "next_checks": ["Lieferumfang und Zustand prüfen"],
    }


class FakeGitHub:
    token = "fake-test-token-never-published"

    def __init__(self, feed):
        self.feed = feed
        self.manifest = {"snapshot_id": SNAPSHOT, "counts": {"total": len(feed["deals"])}}
        self.heads = [HEAD]
        self.calls = []
        self.large_feed = False
        self.feed_bytes = None
        self.patch_error = False
        self.confirm_sha = COMMIT

    def head(self, branch):
        if branch != "main":
            raise AssertionError(branch)
        self.calls.append(("HEAD", branch, None))
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == f"/contents/api/manifest.json?ref={SOURCE}":
            return self.encoded(json.dumps(self.manifest).encode())
        if method == "GET" and path == f"/contents/data/deals.json?ref={SOURCE}":
            if self.large_feed:
                return {"type": "file", "encoding": "none", "sha": BLOB}
            return self.encoded(self.raw_feed())
        if method == "GET" and path == f"/git/blobs/{BLOB}":
            return self.encoded(self.raw_feed())
        if method == "GET" and path == f"/git/commits/{HEAD}":
            return {"tree": {"sha": TREE}}
        if method == "POST" and path == "/git/blobs":
            return {"sha": BLOB}
        if method == "POST" and path == "/git/trees":
            return {"sha": NEW_TREE}
        if method == "POST" and path == "/git/commits":
            return {"sha": COMMIT}
        if method == "PATCH" and path == "/git/refs/heads/main":
            if self.patch_error:
                raise GitHubError(422)
            return {"object": {"sha": self.confirm_sha}}
        raise AssertionError((method, path))

    @staticmethod
    def encoded(raw):
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(raw).decode()}

    def raw_feed(self):
        return self.feed_bytes if self.feed_bytes is not None else json.dumps(self.feed).encode()


class ReviewRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "reviews").mkdir()
        self.feed = {"snapshot_id": SNAPSHOT, "generated_at": stamp(NOW - timedelta(minutes=5)),
                     "deals": [offer(1), offer(2)]}
        self.api = FakeGitHub(self.feed)
        self.ratings = {
            "schema_version": 2, "reviewer": "ChatGPT", "source_snapshot_id": SNAPSHOT,
            "reviewed_at": stamp(NOW - timedelta(minutes=1)),
            "groups": [{"ids": [offer(1)["id"]], "assessment": assessment()}],
        }
        self.request = {"schema_version": 1, "request": {
            "id": "review-2026-11-09-001", "source_commit": SOURCE, "ratings": self.ratings,
        }}
        self.old_review = {
            "id": offer(2)["id"], "content_fingerprint": content_fingerprint(offer(2)),
            "score_version": 2, "assessment": assessment(), "reviewed_at": stamp(NOW - timedelta(minutes=20)),
            "source_snapshot_id": "sha256:" + "2" * 64, "ends_at": offer(2)["ends_at"],
        }
        self.write(worker.STORE_PATH, {"schema_version": 3, "reviewer": "ChatGPT", "reviews": [self.old_review]})
        self.write(worker.PENDING_PATH, self.request)
        self.initial_store = (self.root / worker.STORE_PATH).read_bytes()
        self.initial_pending = (self.root / worker.PENDING_PATH).read_bytes()

    def write(self, path, document):
        (self.root / path).write_text(json.dumps(document), encoding="utf-8")

    def run_request(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return worker.process_request(self.api, self.root, HEAD, sleep=lambda delay: None, monotonic=lambda: 1)

    def assert_untouched(self):
        self.assertEqual((self.root / worker.STORE_PATH).read_bytes(), self.initial_store)
        self.assertEqual((self.root / worker.PENDING_PATH).read_bytes(), self.initial_pending)
        self.assertFalse((self.root / worker.RECEIPT_PATH).exists())

    def writes(self):
        return [call for call in self.api.calls if call[0] in {"POST", "PATCH"}]

    def test_idle_request_needs_no_github_or_store(self):
        self.write(worker.PENDING_PATH, worker.EMPTY_REQUEST)
        (self.root / worker.STORE_PATH).unlink()
        self.assertEqual(worker.process_request(None, self.root, ""), {"status": "idle"})

    def test_pack_preserves_existing_reviews_and_commits_all_outputs_atomically(self):
        result = self.run_request()
        self.assertEqual(result, {"status": "packed", "request_id": self.request["request"]["id"],
                                  "commit": COMMIT, "submitted_reviews": 1, "stored_reviews": 2})
        current = json.loads((self.root / worker.STORE_PATH).read_text())
        by_id = {review["id"]: review for review in current["reviews"]}
        self.assertEqual(by_id[offer(2)["id"]], self.old_review)
        new = by_id[offer(1)["id"]]
        self.assertEqual(new["reviewed_at"], self.ratings["reviewed_at"])
        self.assertEqual(new["assessment"], assessment())
        self.assertEqual(new["source_snapshot_id"], SNAPSHOT)
        self.assertEqual(new["content_fingerprint"], content_fingerprint(offer(1)))
        self.assertEqual(json.loads((self.root / worker.PENDING_PATH).read_text()), worker.EMPTY_REQUEST)
        receipt = json.loads((self.root / worker.RECEIPT_PATH).read_text())
        self.assertEqual(receipt["request_commit"], HEAD)
        self.assertEqual(receipt["source_commit"], SOURCE)
        self.assertEqual(receipt["submitted_reviews"], 1)
        self.assertEqual(receipt["stored_reviews"], 2)
        tree = next(body for method, path, body in self.writes() if path == "/git/trees")
        self.assertEqual(tree["base_tree"], TREE)
        self.assertEqual({entry["path"] for entry in tree["tree"]},
                         {worker.STORE_PATH, worker.PENDING_PATH, worker.RECEIPT_PATH})
        commit = next(body for method, path, body in self.writes() if path == "/git/commits")
        self.assertEqual(commit["parents"], [HEAD])
        self.assertEqual(self.writes()[-1], ("PATCH", "/git/refs/heads/main", {"sha": COMMIT, "force": False}))

    def test_large_full_feed_uses_pinned_blob_endpoint(self):
        self.api.large_feed = True
        self.run_request()
        self.assertIn(("GET", f"/git/blobs/{BLOB}", None), self.api.calls)
        reads = [path for method, path, _ in self.api.calls if method == "GET"]
        self.assertIn(f"/contents/data/deals.json?ref={SOURCE}", reads)
        self.assertIn(f"/contents/api/manifest.json?ref={SOURCE}", reads)
        self.assertFalse(any("ref=data" in path or "http" in path for path in reads))

    def test_manifest_mismatch_or_wrong_count_stops_before_writes(self):
        for manifest in ({"snapshot_id": "sha256:" + "9" * 64, "counts": {"total": 2}},
                         {"snapshot_id": SNAPSHOT, "counts": {"total": 3}},
                         {"snapshot_id": SNAPSHOT, "counts": {"total": True}}):
            with self.subTest(manifest=manifest):
                self.api.manifest = manifest
                with self.assertRaises(ReviewsError):
                    self.run_request()
                self.assertFalse(self.writes())
                self.assert_untouched()

    def test_duplicate_json_fields_in_remote_feed_are_rejected(self):
        self.api.feed_bytes = ('{"snapshot_id":"' + SNAPSHOT + '","snapshot_id":"duplicate"}').encode()
        with self.assertRaises(ReviewsError):
            self.run_request()
        self.assertFalse(self.writes())
        self.assert_untouched()

    def test_pack_validation_failures_preserve_all_files(self):
        mutations = [
            lambda: self.feed["deals"].append(copy.deepcopy(offer(1))),
            lambda: self.ratings["groups"][0]["ids"].append(offer(1)["id"]),
            lambda: self.ratings["groups"][0]["ids"].append(offer(99)["id"]),
            lambda: self.ratings["groups"][0]["assessment"].update({"estimated_price": 20}),
            lambda: self.feed["deals"][0].update({"description_truncated": True}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                original_feed, original_ratings = copy.deepcopy(self.feed), copy.deepcopy(self.ratings)
                mutate()
                self.api.manifest["counts"]["total"] = len(self.feed["deals"])
                self.write(worker.PENDING_PATH, self.request)
                self.initial_pending = (self.root / worker.PENDING_PATH).read_bytes()
                with self.assertRaises(ReviewsError):
                    self.run_request()
                self.assertFalse(self.writes())
                self.assert_untouched()
                self.feed.clear()
                self.feed.update(original_feed)
                self.ratings.clear()
                self.ratings.update(original_ratings)

    def test_main_movement_before_or_during_publication_never_updates_ref(self):
        for heads in (["9" * 40], [HEAD, "9" * 40], [HEAD, HEAD, "9" * 40]):
            with self.subTest(heads=heads):
                self.api.calls.clear()
                self.api.heads = list(heads)
                with self.assertRaises(SnapshotError):
                    self.run_request()
                self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls))
                self.assert_untouched()

    def test_rejected_or_unconfirmed_ref_update_leaves_local_store_and_request_untouched(self):
        for reject in (True, False):
            with self.subTest(reject=reject):
                self.api.patch_error = reject
                self.api.confirm_sha = "9" * 40
                with self.assertRaises(SnapshotError):
                    self.run_request()
                self.assert_untouched()

    def test_request_rejects_arbitrary_paths_and_nonimmutable_refs(self):
        for change in ({"source_commit": "data"}, {"source_commit": "../main"},
                       {"id": "bad\nrequest"}, {"ratings_path": "https://example.invalid/input"}):
            with self.subTest(change=change):
                request = copy.deepcopy(self.request)
                request["request"].update(change)
                self.write(worker.PENDING_PATH, request)
                with self.assertRaises((ReviewsError, SnapshotError)):
                    self.run_request()
                self.assertFalse(self.api.calls)

    def test_duplicate_pending_json_keys_and_symbolic_links_rejected(self):
        (self.root / worker.PENDING_PATH).write_text('{"schema_version":1,"schema_version":1,"request":null}')
        with self.assertRaises(ReviewsError):
            worker.read_request(self.root)
        (self.root / worker.PENDING_PATH).unlink()
        (self.root / worker.PENDING_PATH).symlink_to(self.root / worker.STORE_PATH)
        with self.assertRaises(ReviewsError):
            worker.read_request(self.root)
        self.assertFalse(self.api.calls)

    def test_missing_durable_store_stops_instead_of_replacing_history(self):
        (self.root / worker.STORE_PATH).unlink()
        with self.assertRaises(ReviewsError):
            self.run_request()
        self.assertFalse(self.writes())

    def test_empty_batch_and_reused_request_id_are_rejected(self):
        self.request["request"]["ratings"]["groups"] = []
        self.write(worker.PENDING_PATH, self.request)
        with self.assertRaises(ReviewsError):
            self.run_request()
        self.assertFalse(self.api.calls)
        self.request["request"]["ratings"]["groups"] = [{"ids": [offer(1)["id"]], "assessment": assessment()}]
        self.write(worker.PENDING_PATH, self.request)
        self.write(worker.RECEIPT_PATH, {"request_id": self.request["request"]["id"]})
        with self.assertRaises(ReviewsError):
            self.run_request()
        self.assertFalse(self.writes())

    def test_size_limit_is_enforced_before_pack_or_write(self):
        with patch.object(worker, "MAX_FEED_BYTES", 10), self.assertRaises(SnapshotError):
            self.run_request()
        self.assertFalse(self.writes())
        self.assert_untouched()

    def test_main_guard_rejects_pull_request_environment(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/12/merge"}), redirect_stderr(io.StringIO()):
            self.assertEqual(worker.main(), 2)


if __name__ == "__main__":
    unittest.main()
