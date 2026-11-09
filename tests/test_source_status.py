"""Source-status summaries do not turn partial crawls into successful scans."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import report_source_status as report


def manifest(status="partial", traversal=False):
    return {"kind": "manifest", "source_status": status, "provider": "aog",
            "source": "auktionen-ohne-gebot", "generated_at": "2026-11-09T14:00:00Z",
            "coverage": {"traversal_completed": traversal, "complete": False,
                         "api_requests_this_run": 71, "max_api_requests_per_run": 160,
                         "last_successful_full_scan_at": "2026-11-09T02:12:08Z",
                         "searches": {"general": {"enabled": True, "stop_reason": "repeated_page", "remaining_reported": 1060},
                                      "pickup": {"enabled": True, "stop_reason": "reported_end", "sparse_slots_estimate": 257}}}}


class SourceStatusTests(unittest.TestCase):
    def test_partial_source_stays_warning_without_losing_prior_success(self):
        text, warning = report.summarize(manifest())
        self.assertTrue(warning)
        self.assertIn("| Quellenstatus | partial |", text)
        self.assertIn("2026-11-09T02:12:08Z", text)
        self.assertIn("repeated\\_page", text)
        self.assertIn("1060", text)
        self.assertIn("257", text)
        self.assertIn("complete=false", text)
        self.assertIn("keine nachgewiesenen fehlenden Einzelauktionen", text)

    def test_live_completed_offsets_do_not_claim_market_completeness(self):
        text, warning = report.summarize(manifest("live", True))
        self.assertFalse(warning)
        self.assertIn("| Alle gemeldeten Seitenpositionen durchlaufen | ja |", text)
        self.assertIn("keine vollständige eBay-Abdeckung", text)
        self.assertTrue(report.summarize(manifest("live", False))[1])

    def test_inactive_sources_are_not_failed_crawls(self):
        for status in ("disabled", "unconfigured", "demo"):
            with self.subTest(status=status):
                text, warning = report.summarize(manifest(status))
                self.assertFalse(warning)
                self.assertIn("keine aktive Quelle bestätigt", text)
        value = manifest("error")
        value["provider"], value["source"] = "ebay", "ebay"
        self.assertTrue(report.summarize(value)[1])

    def test_untrusted_values_cannot_inject_markdown_or_workflow_commands(self):
        value = manifest()
        value["source"] = "<script>|[click](https://example.com)\n::error::injected"
        value["blockers"] = [{"message": "SECRET MARKER\n::error::untrusted blocker"}]
        text, _ = report.summarize(value)
        self.assertNotIn("<script>", text)
        self.assertNotIn("[click](", text)
        self.assertNotIn("\n::error", text)
        self.assertNotIn("SECRET MARKER", text)
        self.assertIn("&lt;script&gt;\\|\\[click\\]", text)

    def test_cli_appends_summary_and_emits_only_static_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, summary = root / "manifest.json", root / "summary.md"
            source.write_text(json.dumps(manifest()))
            summary.write_text("Existing step\n")
            output = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), contextlib.redirect_stdout(output):
                result = report.main(["--manifest", str(source)])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), report.WARNING + "\n")
            self.assertTrue(summary.read_text().startswith("Existing step\n## Fundgrube:"))

    def test_missing_malformed_or_oversize_input_fails_without_a_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, summary = root / "manifest.json", root / "summary.md"
            cases = [None, "not JSON", "[]", '{}',
                     '{"kind":"manifest","source_status":"live","source_status":"partial","coverage":{}}',
                     json.dumps({**manifest(), "coverage": []}),
                     json.dumps(manifest("live", "true")),
                     " " * (report.MAX_MANIFEST_BYTES + 1)]
            for raw in cases:
                with self.subTest(input=raw[:60] if raw else raw):
                    source.unlink(missing_ok=True)
                    if raw is not None:
                        source.write_text(raw)
                    stderr = io.StringIO()
                    with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), contextlib.redirect_stderr(stderr):
                        self.assertEqual(report.main(["--manifest", str(source)]), 2)
                    self.assertFalse(summary.exists())
                    self.assertEqual(stderr.getvalue(), "Quellenstatus konnte nicht aus einem gültigen Manifest zusammengefasst werden.\n")


if __name__ == "__main__":
    unittest.main()
