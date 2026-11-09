#!/usr/bin/env python3
"""Summarize source health separately from successful snapshot publication."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunter.feed import AOG_PROVIDER, STATUS_VALUES

MAX_MANIFEST_BYTES = 1024 * 1024
WARNING = "::warning title=Fundgrube-Quellenstand::Der Quellenabgleich ist unvollständig oder fehlgeschlagen. Quellenstatus und Abdeckung in der Laufzusammenfassung prüfen; ein erfolgreicher Build bestätigt keinen Vollscan."


def _cell(value: object) -> str:
    if value is None:
        return "unbekannt"
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if not isinstance(value, (str, int, float)):
        return "unbekannt"
    # Values stay in one table cell; listing text and blocker messages are never
    # rendered. Escape HTML and Markdown, including links and table separators.
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).split())[:200]
    return re.sub(r"([\\`*_\[\]()|#!])", r"\\\1", html.escape(text))


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def summarize(manifest: dict) -> tuple[str, bool]:
    if (not isinstance(manifest, dict) or manifest.get("kind") != "manifest"
            or not isinstance(manifest.get("source_status"), str)
            or manifest["source_status"] not in STATUS_VALUES
            or not isinstance(manifest.get("coverage"), dict)):
        raise ValueError("Invalid source manifest")
    status, coverage = manifest["source_status"], manifest["coverage"]
    if coverage.get("traversal_completed") is not None and type(coverage["traversal_completed"]) is not bool:
        raise ValueError("Invalid traversal status")
    searches = coverage.get("searches", {})
    if not isinstance(searches, dict) or any(not isinstance(value, dict) for value in searches.values()):
        raise ValueError("Invalid search coverage")
    is_aog = any(manifest.get(key) in ("aog", AOG_PROVIDER)
                 for key in ("provider", "source_provider", "source"))
    inactive = status in {"disabled", "unconfigured", "demo"}
    warning = status in {"partial", "error"} or (is_aog and not inactive and coverage.get("traversal_completed") is False)
    rows = [("Quellenstatus", status), ("Quelle", manifest.get("source", manifest.get("provider"))),
            ("Snapshot erzeugt (UTC)", manifest.get("generated_at")),
            ("Quellenlauf begonnen (UTC)", coverage.get("scan_started_at")),
            ("Quellenlauf beendet (UTC)", coverage.get("scan_finished_at")),
            ("Letzter erfolgreicher Vollscan (UTC)", coverage.get("last_successful_full_scan_at")),
            ("Alle gemeldeten Seitenpositionen durchlaufen", coverage.get("traversal_completed")),
            ("Datenanfragen", coverage.get("api_requests_this_run")),
            ("Höchstens Datenanfragen", coverage.get("max_api_requests_per_run")),
            ("Eindeutige Quellentreffer", coverage.get("unique_records")),
            ("Duplikate", coverage.get("duplicate_records")),
            ("Kapazitätsbedingt ausgelassene Artikel", coverage.get("retention_omitted_records"))]
    lines = ["## Fundgrube: Quellenstand", "", "| Prüfpunkt | Ergebnis |", "| --- | --- |"]
    lines.extend(f"| {label} | {_cell(value)} |" for label, value in rows)
    for key, label in (("general", "Allgemeine Suche"), ("pickup", "Abholsuche")):
        if key not in searches:
            continue
        search = searches[key]
        for field, name in (("enabled", "aktiviert"), ("stop_reason", "Abschlussgrund"),
                            ("next_offset", "nächster Offset"), ("remaining_reported", "verbleibende Positionen"),
                            ("sparse_slots_estimate", "geschätzte unbelegte Positionen")):
            lines.append(f"| {label}: {name} | {_cell(search.get(field))} |")
    lines.extend(["", "Ein erfolgreicher Build allein bestätigt keinen erfolgreichen Quellenabruf."])
    if inactive:
        lines.append("Demo-Daten sind fiktiv; bei disabled oder unconfigured wurde keine aktive Quelle bestätigt.")
    if warning:
        lines.append("Der Quellenabgleich ist unvollständig oder fehlgeschlagen. Erhaltene ältere Beobachtungen sind keine neue Verifikation.")
    if is_aog:
        lines.append("Bei AOG bleibt complete=false: Auch durchlaufene Offsets garantieren keine vollständige eBay-Abdeckung. Positionslücken sind keine nachgewiesenen fehlenden Einzelauktionen.")
    return "\n".join(lines) + "\n", warning


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("site/api/manifest.json"))
    args = parser.parse_args(argv)
    try:
        with args.manifest.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest too large")
        manifest = json.loads(raw, object_pairs_hook=_object)
        summary, warning = summarize(manifest)
        if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(destination).open("a", encoding="utf-8") as handle:
                handle.write(summary)
        else:
            print(summary, end="")
    except (OSError, ValueError, UnicodeError, RecursionError):
        print("Quellenstatus konnte nicht aus einem gültigen Manifest zusammengefasst werden.", file=sys.stderr)
        return 2
    if warning:
        print(WARNING)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
