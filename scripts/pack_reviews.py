#!/usr/bin/env python3
"""Bind new reviews to a snapshot and merge into the durable per-item store."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunter.reviews import MAX_REVIEW_BYTES, ReviewsError, merge_reviews, read_json_file, seal_reviews


def _same_file(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve() or (first.exists() and second.exists() and first.samefile(second))


def _safe_output(output: Path) -> None:
    target = output.absolute()
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ReviewsError("Bewertungsausgabe darf keinen symbolischen Link enthalten.")
    if target.exists() and not target.is_file():
        raise ReviewsError("Bewertungsausgabe ist keine reguläre Datei.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reviews/current.json"))
    parser.add_argument("--existing", type=Path,
                        help="Weiteren vorhandenen Bestand übernehmen; vorhandene Ausgabe bleibt erhalten.")
    args = parser.parse_args(argv)
    temporary = None
    try:
        _safe_output(args.output)
        if any(_same_file(args.output, source) for source in (args.feed, args.ratings)):
            raise ReviewsError("Bewertungsausgabe darf keine der Eingabedateien überschreiben.")
        at = datetime.now(timezone.utc)
        feed = read_json_file(args.feed, max_bytes=32 * 1024 * 1024)
        ratings = read_json_file(args.ratings)
        incoming = seal_reviews(feed, ratings)
        # Always preserve an existing output, even when another store is added
        # explicitly. --existing is not an implicit destructive replacement.
        existing = read_json_file(args.output) if args.output.exists() else None
        if args.existing is not None:
            additional = read_json_file(args.existing)
            if not _same_file(args.existing, args.output):
                existing = merge_reviews(existing, additional, at, feed=feed)
        document = merge_reviews(existing, incoming, at, feed=feed)
        encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_REVIEW_BYTES:
            raise ReviewsError("Ausgabedokument überschreitet die erlaubte Dateigröße.")
        _safe_output(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=args.output.parent, prefix=".fundgrube-reviews-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
        os.replace(temporary, args.output)
        temporary = None
    except (ReviewsError, OSError) as error:
        message = str(error) if isinstance(error, ReviewsError) else "Bewertungsdatei konnte nicht geschrieben werden."
        print(f"Bewertungsimport fehlgeschlagen: {message}", file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"{len(incoming['reviews'])} neue Einträge verarbeitet; {len(document['reviews'])} interaktive Bewertungen im Bestand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
