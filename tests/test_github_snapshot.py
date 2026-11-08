"""Snapshot publication tests; no live API calls or credentials are needed."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from hunter import feed as publisher
from scripts import github_snapshot as snapshot

OLD = "a" * 40
MAIN = "b" * 40
TREE = "c" * 40
NEW = "d" * 40
OTHER = "e" * 40
REPOSITORY = "Pogoner/fundgrube"


class FakeGitHub:
    def __init__(self, head=OLD):
        self.repository = REPOSITORY
        self.token = "test-secret-token"
        self.data_head = head
        self.calls = []
        self.on_commit = None
        self.state = {"items": [], "cursor": 3}
        self.ref_error = None

    def head(self, branch):
        return MAIN if branch == "main" else self.data_head

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/contents/state.json?ref="):
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(json.dumps(self.state).encode()).decode()}
        if path == "/git/trees":
            return {"sha": TREE}
        if path == "/git/commits":
            if self.on_commit:
                self.on_commit()
            return {"sha": NEW}
        if path in {"/git/refs", "/git/refs/heads/data"}:
            if self.ref_error:
                raise self.ref_error
            self.data_head = body["sha"]
            return {"ref": "refs/heads/data", "object": {"type": "commit", "sha": self.data_head}}
        raise AssertionError(f"Unexpected API request: {method} {path}")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.state = self.root / "runtime"
        (self.site / "api" / "pages").mkdir(parents=True)
        (self.site / "data").mkdir()
        self.state.mkdir()
        self.write(self.site / "api" / "manifest.json", {"schema_version": 1})
        self.write(self.site / "api" / "pages" / "001.json", {"items": []})
        self.write(self.site / "data" / "deals.json", {"items": []})
        (self.site / "api" / "overview.md").write_text("# Keine Angebote\n", encoding="utf-8")
        self.api = FakeGitHub()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def prepared(self):
        snapshot.restore(self.api, self.state)
        self.api.calls.clear()

    def test_restore_uses_immutable_head(self):
        result = snapshot.restore(self.api, self.state)
        self.assertTrue(result["restored"])
        self.assertEqual(json.loads((self.state / "state.json").read_text()), self.api.state)
        self.assertIn(("GET", f"/contents/state.json?ref={OLD}", None), self.api.calls)
        marker = json.loads((self.state / snapshot.MARKER).read_text())
        self.assertEqual(marker["head"], OLD)

    def test_restore_new_branch_records_main_parent_without_fabricating_state(self):
        self.api.data_head = None
        result = snapshot.restore(self.api, self.state)
        self.assertFalse(result["restored"])
        self.assertFalse((self.state / "state.json").exists())
        self.assertEqual(json.loads((self.state / snapshot.MARKER).read_text())["base"], MAIN)

    def test_restore_does_not_reuse_unrelated_local_state(self):
        self.api.data_head = None
        self.write(self.state / "state.json", {"private": "old"})
        with self.assertRaisesRegex(snapshot.SnapshotError, "fresh state"):
            snapshot.restore(self.api, self.state)

    def test_restore_handles_large_contents_via_blob(self):
        self.api.request = Mock(side_effect=[
            {"type": "file", "encoding": "none", "sha": TREE},
            {"encoding": "base64", "content": base64.b64encode(b'{"items":[]}').decode()},
        ])
        snapshot.restore(self.api, self.state)
        self.api.request.assert_any_call("GET", f"/git/blobs/{TREE}")

    def test_restore_rejects_malformed_remote_state(self):
        self.api.request = Mock(return_value={"type": "file", "encoding": "base64", "content": "%%%"})
        with self.assertRaisesRegex(snapshot.SnapshotError, "decoded"):
            snapshot.restore(self.api, self.state)
        self.assertFalse((self.state / snapshot.MARKER).exists())

    def test_publish_creates_complete_generated_tree_and_never_forces(self):
        self.prepared()
        # Files outside the allowlisted generated paths must never be uploaded.
        (self.site / "index.html").write_text("site source", encoding="utf-8")
        (self.state / "credentials.txt").write_text("not published", encoding="utf-8")
        result = snapshot.publish(self.api, self.state, self.site)
        self.assertEqual(result["head"], NEW)
        tree_body = next(body for method, path, body in self.api.calls if path == "/git/trees")
        self.assertNotIn("base_tree", tree_body)
        paths = {entry["path"] for entry in tree_body["tree"]}
        self.assertEqual(paths, {"api/manifest.json", "api/pages/001.json", "api/overview.md", "data/deals.json", "state.json"})
        self.assertIn(("PATCH", "/git/refs/heads/data", {"sha": NEW, "force": False}), self.api.calls)
        commit_body = next(body for _, path, body in self.api.calls if path == "/git/commits")
        self.assertEqual(commit_body["parents"], [OLD])

    def test_new_branch_has_main_parent_and_ref_is_created(self):
        self.api.data_head = None
        snapshot.restore(self.api, self.state)
        self.write(self.state / "state.json", {"items": []})
        snapshot.publish(self.api, self.state, self.site)
        commit_body = next(body for _, path, body in self.api.calls if path == "/git/commits")
        self.assertEqual(commit_body["parents"], [MAIN])
        self.assertIn(("POST", "/git/refs", {"ref": "refs/heads/data", "sha": NEW}), self.api.calls)

    def test_concurrent_change_before_publish_performs_no_writes(self):
        self.prepared()
        self.api.data_head = OTHER
        with self.assertRaisesRegex(snapshot.SnapshotError, "changed after restore"):
            snapshot.publish(self.api, self.state, self.site)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.data_head, OTHER)

    def test_concurrent_change_during_publish_never_updates_ref(self):
        self.prepared()
        self.api.on_commit = lambda: setattr(self.api, "data_head", OTHER)
        with self.assertRaisesRegex(snapshot.SnapshotError, "changed during publication"):
            snapshot.publish(self.api, self.state, self.site)
        self.assertFalse(any("/git/refs" in path for _, path, _ in self.api.calls))
        self.assertEqual(self.api.data_head, OTHER)

    def test_server_rejection_is_not_retried_with_force(self):
        self.prepared()
        self.api.ref_error = snapshot.GitHubError(422)
        with self.assertRaises(snapshot.GitHubError):
            snapshot.publish(self.api, self.state, self.site)
        attempts = [call for call in self.api.calls if call[1] == "/git/refs/heads/data"]
        self.assertEqual(len(attempts), 1)
        self.assertFalse(attempts[0][2]["force"])

    def test_publish_requires_restore_and_matching_repository(self):
        with self.assertRaisesRegex(snapshot.SnapshotError, "Run restore"):
            snapshot.publish(self.api, self.state, self.site)
        self.prepared()
        self.api.repository = "someone/else"
        with self.assertRaisesRegex(snapshot.SnapshotError, "different repository"):
            snapshot.publish(self.api, self.state, self.site)

    def test_credential_fields_are_rejected_before_network_writes(self):
        self.prepared()
        self.write(self.site / "api" / "pages" / "001.json", {"nested": [{"client_secret": "never-print-this"}]})
        with self.assertRaises(snapshot.SnapshotError) as caught:
            snapshot.publish(self.api, self.state, self.site)
        self.assertNotIn("never-print-this", str(caught.exception))
        self.assertEqual(self.api.calls, [])

    def test_current_github_token_is_rejected_even_in_text(self):
        self.prepared()
        (self.site / "api" / "overview.md").write_text(self.api.token, encoding="utf-8")
        with self.assertRaisesRegex(snapshot.SnapshotError, "credential"):
            snapshot.publish(self.api, self.state, self.site)

    def test_symlink_to_file_outside_api_is_rejected(self):
        self.prepared()
        (self.site / "api" / "secret.json").symlink_to(self.state / "state.json")
        with self.assertRaisesRegex(snapshot.SnapshotError, "symbolic link"):
            snapshot.collect_files(self.site, self.state)

    def test_hidden_or_executable_api_files_are_rejected(self):
        self.prepared()
        for filename in [".secret.json", "tool.py"]:
            with self.subTest(filename=filename):
                path = self.site / "api" / filename
                path.write_text("{}", encoding="utf-8")
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.collect_files(self.site, self.state)
                path.unlink()

    def test_required_manifest_cannot_be_missing(self):
        self.prepared()
        (self.site / "api" / "manifest.json").unlink()
        with self.assertRaisesRegex(snapshot.SnapshotError, "missing"):
            snapshot.collect_files(self.site, self.state)

    def test_size_limits_apply_before_upload(self):
        self.prepared()
        with patch.object(snapshot, "MAX_FILE_BYTES", 1):
            with self.assertRaisesRegex(snapshot.SnapshotError, "size limit"):
                snapshot.collect_files(self.site, self.state)

    def test_total_and_file_count_limits_still_apply_before_upload(self):
        self.prepared()
        with patch.object(snapshot, "MAX_TOTAL_BYTES", 1):
            with self.assertRaisesRegex(snapshot.SnapshotError, "total safe size limit"):
                snapshot.collect_files(self.site, self.state)
        with patch.object(snapshot, "MAX_FILES", 1):
            with self.assertRaisesRegex(snapshot.SnapshotError, "too many files"):
                snapshot.collect_files(self.site, self.state)

    def test_5000_observed_items_and_old_reviews_fit_offline_snapshot_bounds(self):
        # Representative aggregator records: no full descriptions, but detailed
        # durable semantic reasons and explicitly unverified source facts.
        at, observed, reviewed = "2026-11-08T09:00:00Z", "2026-11-08T08:45:00Z", "2026-11-04T09:00:00Z"
        base = {
            "description": "", "content_updated_at": None, "image": "",
            "price_eur": 1, "price_kind": "auction_price", "shipping_eur": 8,
            "bid_count": 0, "ends_at": "2026-11-09T09:00:00Z", "last_seen_at": observed,
            "condition": "Als Ersatzteil / defekt", "city": "", "postcode": "",
            "distance_km": None, "local_pickup_confirmed": False, "pickup_available": False,
            "is_auction": True, "buying_options": ["AUCTION"], "reserve_status": "unknown",
            "eligibility": "review", "eligibility_without_time_window": "review",
            "eligibility_reasons": ["Mindestpreisstatus aus der Quelle nicht belegt."],
            "score_version": 2, "scalp_score": 80, "useability_score": 90,
            "interest_score": 72, "score": 72, "semantic_interest": 80,
            "value_band": "around_500", "product_kind": "apparatus",
            "assessment_basis": "llm", "assessment_confidence": "low", "semantic_assessed": True,
            "semantic_review_mode": "interactive", "semantic_assessed_at": reviewed,
            "value_reason": "Fiktive Testbewertung: vollständige professionelle Laborapparatur mit plausibel hohem wirtschaftlichem Potenzial. Modell, Lieferumfang und tatsächlicher Zustand bleiben ungeprüft; die Einordnung ist keine Wertermittlung oder Funktionsbestätigung.",
            "useability_reason": "Fiktive Testbewertung: eigenständig mit üblicher Stromversorgung und passenden Verbrauchsmitteln nutzbar. Kein bestimmtes übergeordnetes Anlagensystem vorausgesetzt; ein Funktionstest ist weiterhin erforderlich.",
            "dependencies": ["Übliche Stromversorgung", "Passende Verbrauchsmittel und sichere Aufstellung"],
            "reasons": ["Fiktive Testbewertung eines vollständigen Geräts statt einer speziellen Ersatzbaugruppe."],
            "risks": ["Keine unabhängige Funktionsprüfung; fehlendes Zubehör und konkrete Defekte im Original prüfen.",
                      "Mindestpreisstatus unbekannt; aus einem Euro darf kein garantierter Endpreis abgeleitet werden."],
            "next_checks": ["Modell und Lieferumfang im Originalangebot prüfen.",
                            "Aktuellen Preis, Gebote, Versandbedingungen und Funktionsangaben vor einer Entscheidung prüfen."],
            "source_provider": publisher.AOG_PROVIDER, "source_url": publisher.AOG_SOURCE_URL,
            "source_observed_at": observed, "verification_level": "aggregator", "stale": False, "carried": False,
        }
        items = [{**base, "id": f"v1|{100000000000 + number}|0",
                  "title": f"Fiktive eigenständige Laborapparatur {number} mit Zubehör – ungeprüft",
                  "url": f"https://www.ebay.de/itm/{100000000000 + number}"} for number in range(5000)]
        feed = {
            "schema_version": 3, "generated_at": at, "last_successful_scan_at": at,
            "source_status": "partial", "source": publisher.AOG_PROVIDER, "provider": publisher.AOG_PROVIDER,
            "criteria": {"lookahead_hours": 72, "description_max_age_hours": 24},
            "coverage": {"complete": False, "retention_capacity": 5000}, "deals": items,
        }
        site, state = self.root / "large-site", self.root / "large-state"
        state.mkdir()
        snapshot.atomic_write(state / "state.json", json.dumps({"schema_version": 2, "records": items},
                                                               ensure_ascii=False, indent=2))
        with patch.object(snapshot, "GitHub") as github:
            publisher.write_feed(site, feed)
            entries = snapshot.collect_files(site, state)
            github.assert_not_called()
        manifest = json.loads((site / "api" / "manifest.json").read_text())
        self.assertEqual(manifest["counts"], {"total": 5000, "pages": 100, "page_size": 50})
        self.assertEqual(manifest["stats"]["omitted_by_capacity"], 0)
        self.assertFalse(manifest["coverage"]["complete"])
        self.assertEqual(len(entries), 5107)
        sizes = {entry["path"]: len(entry["content"].encode("utf-8")) for entry in entries}
        self.assertLess(max(sizes.values()), snapshot.MAX_FILE_BYTES)
        self.assertLess(sum(sizes.values()), snapshot.MAX_TOTAL_BYTES)
        self.assertLess(len(entries), snapshot.MAX_FILES)
        self.assertGreater(sum(sizes.values()), 16 * 1024 * 1024)
        self.assertGreater(sizes["data/deals.json"], 8 * 1024 * 1024)
        dashboard = json.loads((site / "data" / "deals.json").read_text())
        for row in (dashboard["deals"][0], dashboard["deals"][-1]):
            self.assertEqual(row["semantic_assessed_at"], reviewed)
            self.assertEqual(row["assessment_basis"], "llm")
            self.assertEqual(row["interest_score"], 72)
            self.assertEqual(row["source_observed_at"], observed)
        self.capacity_metrics = {"files": len(entries), "total_bytes": sum(sizes.values()),
                                 "largest_bytes": max(sizes.values()),
                                 "dashboard_bytes": sizes["data/deals.json"], "state_bytes": sizes["state.json"]}

    def test_validate_is_offline(self):
        self.prepared()
        with patch.dict("os.environ", {}, clear=True), patch.object(snapshot, "GitHub") as github, contextlib.redirect_stdout(io.StringIO()):
            result = snapshot.main(["validate", "--site-dir", str(self.site), "--state-dir", str(self.state)])
        self.assertEqual(result, 0)
        github.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_repository_cannot_inject_url(self):
        for value in ["https://evil.example/repo", "owner/repo?token=x", "../repo", "owner/repo/extra"]:
            with self.subTest(value=value), self.assertRaises(snapshot.SnapshotError):
                snapshot.GitHub(value, "token")

    def test_redirects_are_refused(self):
        self.assertIsNone(snapshot.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org"))

    def test_http_error_body_and_token_are_not_exposed(self):
        api = snapshot.GitHub(REPOSITORY, "super-secret")
        api.opener = Mock()
        api.opener.open.side_effect = urllib.error.HTTPError("https://example.org/super-secret", 403, "super-secret", {}, io.BytesIO(b"super-secret"))
        with self.assertRaises(snapshot.GitHubError) as caught:
            api.request("POST", "/git/trees", {"tree": []})
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("super-secret", str(caught.exception))
        self.assertEqual(api.opener.open.call_count, 1)

    def test_path_escape_is_refused(self):
        api = snapshot.GitHub(REPOSITORY, "token")
        for path in ["//evil.example", "/../other", "https://evil.example"]:
            with self.subTest(path=path), self.assertRaises(snapshot.SnapshotError):
                api.request("GET", path)


if __name__ == "__main__":
    unittest.main()
