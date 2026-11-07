#!/usr/bin/env python3
"""Bind authored grouped reviews to an exact local snapshot; never call a model."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunter.reviews import MAX_REVIEW_BYTES, ReviewsError, read_json_file, seal_reviews


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reviews/current.json"))
    args = parser.parse_args(argv)
    temporary = None
    try:
        if args.output.resolve() in {args.feed.resolve(), args.ratings.resolve()}:
            raise ReviewsError("Bewertungsausgabe darf keine der Eingabedateien überschreiben.")
        feed = read_json_file(args.feed, max_bytes=16_000_000)
        ratings = read_json_file(args.ratings)
        document = seal_reviews(feed, ratings)
        encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_REVIEW_BYTES:
            raise ReviewsError("Ausgabedokument überschreitet die erlaubte Dateigröße.")
        if args.output.is_symlink() or args.output.parent.is_symlink():
            raise ReviewsError("Bewertungsausgabe darf kein symbolischer Link sein.")
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
    print(f"{len(document['reviews'])} interaktive Bewertungen an den Quell-Snapshot gebunden.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
