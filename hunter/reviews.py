"""Bind interactive ChatGPT screening to exact item text, without network calls.

The fingerprint binds content; it is not a signature proving reviewer identity.
The repository's write permissions remain the authority for authored reviews.
All imported review text is data, never instructions or executable code.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hunter.ranking import apply_semantic
from hunter.semantic import ANNOTATION_FIELDS, SemanticError, _validate_assessment

MAX_REVIEW_BYTES = 2_000_000
MAX_REVIEWS = 800
MAX_REVIEW_AGE = timedelta(hours=48)
MAX_SOURCE_AGE = timedelta(hours=6)
HEADER_FIELDS = {"schema_version", "source_snapshot_id", "reviewed_at", "reviewer"}
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.|:-]{0,199}\Z")
UNSAFE_TEXT = re.compile(r"https?://|www\.|javascript\s*:|data\s*:|mailto\s*:|<\s*/?\s*[A-Za-z][^>]*>", re.I)


class ReviewsError(ValueError):
    """A bounded, non-secret-bearing review import or validation failure."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReviewsError("Bewertungsimport benötigt einen Zeitpunkt mit Zeitzone.")
    return value.astimezone(timezone.utc)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        return None


def _iso(value: datetime) -> str:
    precision = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=precision).replace("+00:00", "Z")


def _bounded_json(value: Any, max_bytes: int = MAX_REVIEW_BYTES) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ReviewsError("Bewertungsdokument enthält ungültige JSON-Daten.") from None
    if len(encoded) > max_bytes:
        raise ReviewsError("Bewertungsdokument überschreitet die erlaubte Größe.")
    return encoded


def _item_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value) or UNSAFE_TEXT.search(value):
        raise ReviewsError("Bewertungsdokument enthält eine ungültige Angebots-ID.")
    return value


def content_fingerprint(item: dict[str, Any]) -> str:
    """Hash exact normalized text; prices and logistics deliberately do not bind."""
    if not isinstance(item, dict):
        raise ReviewsError("Angebotstext hat ein ungültiges Format.")
    data = {"scheme": "fundgrube-interactive-content-v1"}
    for field, maximum in (("title", 1_000), ("description", 20_000), ("condition", 1_000)):
        value = item.get(field, "")
        if not isinstance(value, str) or len(value) > maximum:
            raise ReviewsError("Angebotstext fehlt oder überschreitet die erlaubte Länge.")
        data[field] = value
    return "sha256:" + hashlib.sha256(_bounded_json(data)).hexdigest()


def _assessment(value: Any) -> dict[str, Any]:
    try:
        result = _validate_assessment(value)
    except (SemanticError, TypeError):
        raise ReviewsError("Bewertung entspricht nicht dem begrenzten semantischen Schema.") from None
    texts = [result["value_reason"], *result["reasons"], *result["risks"], *result["next_checks"]]
    if any(UNSAFE_TEXT.search(text) or "```" in text for text in texts):
        raise ReviewsError("Bewertungen dürfen keine URLs, HTML-Tags oder Codeblöcke enthalten.")
    return result


def _header(document: Any, payload_name: str) -> dict[str, Any]:
    _bounded_json(document)
    if not isinstance(document, dict) or set(document) != HEADER_FIELDS | {payload_name}:
        raise ReviewsError("Bewertungsdokument enthält fehlende oder unerwartete Felder.")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ReviewsError("Unbekannte Bewertungsschema-Version.")
    snapshot_id = document["source_snapshot_id"]
    if not isinstance(snapshot_id, str) or not HASH_PATTERN.fullmatch(snapshot_id):
        raise ReviewsError("Bewertungsdokument enthält keine gültige Quell-Snapshot-ID.")
    reviewed_at = _time(document["reviewed_at"])
    if reviewed_at is None or document["reviewer"] != "ChatGPT":
        raise ReviewsError("Bewertungszeitpunkt oder Urheberangabe ist ungültig.")
    return {"schema_version": 1, "source_snapshot_id": snapshot_id,
            "reviewed_at": _iso(reviewed_at), "reviewer": "ChatGPT"}


def validate_reviews(document: Any) -> dict[str, Any]:
    """Validate the complete document before any annotation can be applied."""
    header = _header(document, "reviews")
    reviews = document["reviews"]
    if not isinstance(reviews, list) or len(reviews) > MAX_REVIEWS:
        raise ReviewsError("Bewertungsdokument enthält zu viele oder ungültige Einträge.")
    result = []
    ids = set()
    for entry in reviews:
        if not isinstance(entry, dict) or set(entry) != {"id", "content_fingerprint", "assessment"}:
            raise ReviewsError("Bewertungseintrag enthält fehlende oder unerwartete Felder.")
        item_id = _item_id(entry["id"])
        fingerprint = entry["content_fingerprint"]
        if item_id in ids:
            raise ReviewsError("Bewertungsdokument enthält eine doppelte Angebots-ID.")
        if not isinstance(fingerprint, str) or not HASH_PATTERN.fullmatch(fingerprint):
            raise ReviewsError("Bewertungseintrag enthält keinen gültigen Inhaltsfingerprint.")
        ids.add(item_id)
        result.append({"id": item_id, "content_fingerprint": fingerprint,
                       "assessment": _assessment(entry["assessment"])})
    return {**header, "reviews": result}


def seal_reviews(feed: dict[str, Any], specification: dict[str, Any]) -> dict[str, Any]:
    """Expand authored groups and bind each rating to this exact source snapshot.

    A summary marked description_truncated cannot establish the full text hash;
    callers must obtain complete item text rather than pretend to have read it.
    """
    header = _header(specification, "groups")
    if not isinstance(feed, dict) or feed.get("snapshot_id") != header["source_snapshot_id"]:
        raise ReviewsError("Bewertungen und Angebotsfeed gehören zu unterschiedlichen Snapshots.")
    generated_at = _time(feed.get("generated_at"))
    reviewed_at = _time(header["reviewed_at"])
    if generated_at is None or reviewed_at < generated_at:
        raise ReviewsError("Bewertungszeitpunkt liegt vor dem Quell-Snapshot oder dessen Zeitpunkt fehlt.")
    items = feed.get("deals")
    if not isinstance(items, list) or len(items) > 10_000:
        raise ReviewsError("Quell-Snapshot enthält keine gültige begrenzte Angebotsliste.")
    by_id = {}
    for item in items:
        if not isinstance(item, dict):
            raise ReviewsError("Quell-Snapshot enthält einen ungültigen Angebotseintrag.")
        item_id = _item_id(item.get("id"))
        if item_id in by_id:
            raise ReviewsError("Quell-Snapshot enthält doppelte Angebots-IDs.")
        by_id[item_id] = item
    groups = specification["groups"]
    if not isinstance(groups, list) or len(groups) > MAX_REVIEWS:
        raise ReviewsError("Bewertungsgruppen haben ein ungültiges Format.")
    reviews = []
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"assessment", "ids"}:
            raise ReviewsError("Bewertungsgruppe enthält fehlende oder unerwartete Felder.")
        answer = _assessment(group["assessment"])
        ids = group["ids"]
        if not isinstance(ids, list) or not ids or len(ids) + len(reviews) > MAX_REVIEWS:
            raise ReviewsError("Bewertungsgruppe enthält keine oder zu viele Angebots-IDs.")
        for value in ids:
            item_id = _item_id(value)
            if item_id in seen or item_id not in by_id:
                raise ReviewsError("Bewertung enthält eine doppelte oder nicht zum Snapshot gehörende Angebots-ID.")
            source = by_id[item_id]
            if source.get("description_truncated") is True:
                raise ReviewsError("Gekürzte Beschreibung kann nicht als vollständig gelesener Inhalt gebunden werden.")
            seen.add(item_id)
            reviews.append({"id": item_id, "content_fingerprint": content_fingerprint(source),
                            "assessment": copy.deepcopy(answer)})
    document = {**header, "reviews": reviews}
    return validate_reviews(document)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewsError("JSON-Dokument enthält doppelte Feldnamen.")
        result[key] = value
    return result


def read_json_file(path: str | Path, *, max_bytes: int = MAX_REVIEW_BYTES) -> Any:
    """Bounded local JSON read; never follow a final symbolic link."""
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ReviewsError("Bewertungsdatei fehlt oder ist keine reguläre Datei.")
    try:
        with target.open("rb") as handle:
            encoded = handle.read(max_bytes + 1)
        if len(encoded) > max_bytes:
            raise ReviewsError("JSON-Datei überschreitet die erlaubte Größe.")
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ReviewsError("Nichtendliche JSON-Zahl ist unzulässig.")))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ReviewsError("JSON-Datei konnte nicht sicher gelesen werden.") from None
    return value


def load_reviews(path: str | Path) -> dict[str, Any]:
    return validate_reviews(read_json_file(path))


def apply_reviews(
    items: list[dict[str, Any]], review_doc: dict[str, Any] | None, at: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply fresh, content-matched annotations; facts and newer LLM work survive."""
    at = _utc(at)
    output = [dict(item) for item in items]
    metadata: dict[str, Any] = {
        "status": "absent", "applied": 0, "total": 0, "skipped_changed": 0,
        "skipped_stale": 0, "skipped_newer": 0, "skipped_missing": 0, "errors": [],
    }
    if review_doc is None:
        return output, metadata
    try:
        document = validate_reviews(review_doc)
    except ReviewsError as error:
        metadata.update({"status": "invalid", "errors": [str(error)]})
        return output, metadata
    reviewed_at = _time(document["reviewed_at"])
    metadata.update({"total": len(document["reviews"]), "reviewer": "ChatGPT",
                     "source_snapshot_id": document["source_snapshot_id"],
                     "reviewed_at": document["reviewed_at"], "expires_at": _iso(reviewed_at + MAX_REVIEW_AGE)})
    if reviewed_at > at:
        metadata.update({"status": "future", "errors": ["Bewertungszeitpunkt liegt in der Zukunft."]})
        return output, metadata
    if at - reviewed_at > MAX_REVIEW_AGE:
        metadata.update({"status": "expired", "skipped_stale": metadata["total"]})
        return output, metadata
    by_id: dict[str, int] = {}
    duplicate_ids = set()
    for index, item in enumerate(output):
        item_id = item.get("id")
        if isinstance(item_id, str):
            if item_id in by_id:
                duplicate_ids.add(item_id)
            else:
                by_id[item_id] = index
    for review in document["reviews"]:
        item_id = review["id"]
        if item_id not in by_id:
            metadata["skipped_missing"] += 1
            continue
        item = output[by_id[item_id]]
        seen = _time(item.get("last_seen_at"))
        end = _time(item.get("ends_at"))
        if seen is None or seen > at or at - seen > MAX_SOURCE_AGE or end is None or end <= at:
            metadata["skipped_stale"] += 1
            continue
        try:
            matching = content_fingerprint(item) == review["content_fingerprint"]
        except ReviewsError:
            matching = False
        if not matching or item_id in duplicate_ids:
            metadata["skipped_changed"] += 1
            continue
        previous = _time(item.get("semantic_assessed_at"))
        if item.get("assessment_basis") == "llm" and previous is not None and reviewed_at < previous <= at:
            metadata["skipped_newer"] += 1
            continue
        enriched = apply_semantic(item, review["assessment"])
        result = dict(item)
        result.update({key: enriched[key] for key in ANNOTATION_FIELDS if key in enriched})
        result.update({"semantic_review_mode": "interactive", "semantic_assessed_at": document["reviewed_at"]})
        output[by_id[item_id]] = result
        metadata["applied"] += 1
    metadata["status"] = "ok" if metadata["applied"] == metadata["total"] else "partial"
    return output, metadata
