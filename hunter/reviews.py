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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hunter.ranking import apply_semantic
from hunter.semantic import ANNOTATION_FIELDS, SemanticError, _validate_assessment

MAX_REVIEW_BYTES = 16 * 1024 * 1024
MAX_REVIEWS = 5_000
HEADER_FIELDS = {"schema_version", "source_snapshot_id", "reviewed_at", "reviewer"}
STORE_FIELDS = {"schema_version", "reviewer", "reviews"}
LEGACY_ENTRY_FIELDS = {"id", "content_fingerprint", "assessment"}
ENTRY_FIELDS = LEGACY_ENTRY_FIELDS | {"score_version", "reviewed_at", "source_snapshot_id", "ends_at"}
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


def _snapshot_id(value: Any) -> str:
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise ReviewsError("Bewertungsdokument enthält keine gültige Quell-Snapshot-ID.")
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


def _assessment(value: Any, score_version: int) -> dict[str, Any]:
    try:
        result = _validate_assessment(value, score_version=score_version)
    except (SemanticError, TypeError):
        raise ReviewsError("Bewertung entspricht nicht dem begrenzten semantischen Schema.") from None
    texts = [result["value_reason"], *result["reasons"], *result["risks"], *result["next_checks"]]
    if score_version == 2:
        texts.extend([result["useability_reason"], *result["dependencies"]])
    if any(UNSAFE_TEXT.search(text) or "```" in text for text in texts):
        raise ReviewsError("Bewertungen dürfen keine URLs, HTML-Tags oder Codeblöcke enthalten.")
    return result


def _header(document: Any, payload_name: str) -> dict[str, Any]:
    _bounded_json(document)
    if not isinstance(document, dict) or set(document) != HEADER_FIELDS | {payload_name}:
        raise ReviewsError("Bewertungsdokument enthält fehlende oder unerwartete Felder.")
    if type(document["schema_version"]) is not int or document["schema_version"] not in {1, 2}:
        raise ReviewsError("Unbekannte Bewertungsschema-Version.")
    snapshot_id = _snapshot_id(document["source_snapshot_id"])
    reviewed_at = _time(document["reviewed_at"])
    if reviewed_at is None or document["reviewer"] != "ChatGPT":
        raise ReviewsError("Bewertungszeitpunkt oder Urheberangabe ist ungültig.")
    return {"schema_version": document["schema_version"], "source_snapshot_id": snapshot_id,
            "reviewed_at": _iso(reviewed_at), "reviewer": "ChatGPT"}


def validate_reviews(document: Any) -> dict[str, Any]:
    """Validate a legacy batch or per-item V3 store before applying any entry.

    Store version and score version are separate: V3 can faithfully carry a
    legacy V1 assessment without inventing its missing Useability score.
    """
    _bounded_json(document)
    if not isinstance(document, dict):
        raise ReviewsError("Bewertungsdokument enthält fehlende oder unerwartete Felder.")
    version = document.get("schema_version")
    if type(version) is not int or version not in {1, 2, 3}:
        raise ReviewsError("Unbekannte Bewertungsschema-Version.")
    if version == 3:
        if set(document) != STORE_FIELDS or document["reviewer"] != "ChatGPT":
            raise ReviewsError("Bewertungsspeicher enthält fehlende oder unerwartete Felder.")
        header = {"schema_version": 3, "reviewer": "ChatGPT"}
    else:
        header = _header(document, "reviews")
    reviews = document["reviews"]
    if not isinstance(reviews, list) or len(reviews) > MAX_REVIEWS:
        raise ReviewsError("Bewertungsdokument enthält zu viele oder ungültige Einträge.")
    result = []
    ids = set()
    for entry in reviews:
        if not isinstance(entry, dict) or set(entry) != (ENTRY_FIELDS if version == 3 else LEGACY_ENTRY_FIELDS):
            raise ReviewsError("Bewertungseintrag enthält fehlende oder unerwartete Felder.")
        item_id = _item_id(entry["id"])
        fingerprint = entry["content_fingerprint"]
        if item_id in ids:
            raise ReviewsError("Bewertungsdokument enthält eine doppelte Angebots-ID.")
        if not isinstance(fingerprint, str) or not HASH_PATTERN.fullmatch(fingerprint):
            raise ReviewsError("Bewertungseintrag enthält keinen gültigen Inhaltsfingerprint.")
        ids.add(item_id)
        score_version = entry["score_version"] if version == 3 else version
        if type(score_version) is not int or score_version not in {1, 2}:
            raise ReviewsError("Bewertungseintrag enthält eine unbekannte Score-Version.")
        normalized = {"id": item_id, "content_fingerprint": fingerprint,
                      "assessment": _assessment(entry["assessment"], score_version)}
        if version == 3:
            reviewed_at = _time(entry["reviewed_at"])
            end = _time(entry["ends_at"])
            if reviewed_at is None or (entry["ends_at"] is not None and end is None):
                raise ReviewsError("Bewertungseintrag enthält einen ungültigen Zeitpunkt.")
            normalized.update({"score_version": score_version, "reviewed_at": _iso(reviewed_at),
                               "source_snapshot_id": _snapshot_id(entry["source_snapshot_id"]),
                               "ends_at": _iso(end) if end is not None else None})
        result.append(normalized)
    return {**header, "reviews": result}


def _feed_end_hints(feed: Any, at: datetime) -> dict[str, tuple[str, datetime]]:
    """Read bounded ends and conservative observation times, never infer absence."""
    if not isinstance(feed, dict):
        raise ReviewsError("Quell-Snapshot enthält keine gültige begrenzte Angebotsliste.")
    _snapshot_id(feed.get("snapshot_id"))
    generated = _time(feed.get("generated_at"))
    if generated is None or generated > at:
        raise ReviewsError("Quell-Snapshot enthält einen ungültigen oder zukünftigen Zeitpunkt.")
    items = feed.get("deals")
    if not isinstance(items, list) or len(items) > 10_000:
        raise ReviewsError("Quell-Snapshot enthält keine gültige begrenzte Angebotsliste.")
    result = {}
    seen_ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ReviewsError("Quell-Snapshot enthält einen ungültigen Angebotseintrag.")
        item_id = _item_id(item.get("id"))
        if item_id in seen_ids:
            raise ReviewsError("Quell-Snapshot enthält doppelte Angebots-IDs.")
        seen_ids.add(item_id)
        seen = _time(item.get("last_seen_at"))
        observed = _time(item.get("source_observed_at"))
        end = _time(item.get("ends_at"))
        if ((item.get("last_seen_at") is not None and seen is None)
                or (item.get("source_observed_at") is not None and observed is None)
                or (item.get("ends_at") is not None and end is None)
                or (seen is not None and seen > at) or (observed is not None and observed > at)):
            raise ReviewsError("Quell-Snapshot enthält einen ungültigen oder zukünftigen Quellenzeitpunkt.")
        # Missing source observation time is not evidence for a lifecycle update.
        if seen is not None and end is not None:
            # A snapshot cannot establish an observation newer than itself.
            # If both source times are present, use the older evidence bound.
            observed_at = min(seen, generated, observed if observed is not None else seen)
            result[item_id] = (_iso(end), observed_at)
    return result


def _latest_end(first: str | None, second: str | None) -> str | None:
    """Without per-hint provenance, only extending retention is lossless."""
    known = [value for value in (first, second) if value is not None]
    return max(known, key=_time) if known else None


def _attach_end_hints(
    entries: list[dict[str, Any]], hints: dict[str, tuple[str, datetime]], at: datetime,
) -> None:
    """Conservatively update lifecycle hints, not source facts or review dates.

    V3 does not record an observation time for an existing end hint, so a
    later import cannot prove that a shorter end is newer. Never shorten a
    known end. When no end is known, an already-ended hint may establish a
    pruning deadline only if its observation does not predate the review.
    This can retain uncertain legacy residue; it must not erase newer reviews
    merely because a stale feed says their auctions have already ended.
    """
    for entry in entries:
        hint = hints.get(entry["id"])
        if hint is None:
            continue
        end, observed_at = hint
        if (entry["ends_at"] is None and _time(end) <= at
                and observed_at < _time(entry["reviewed_at"])):
            continue
        entry["ends_at"] = _latest_end(entry["ends_at"], end)


def upgrade_reviews(
    document: Any, *, feed: dict[str, Any] | None = None, at: datetime | None = None,
) -> dict[str, Any]:
    """Migrate to V3 without a new assessment or renewed review timestamps.

    A matching feed may supply auction-end hints for later explicit cleanup.
    These hints are never used as source facts when applying an assessment.
    Known ends can only be extended; stale already-ended hints cannot bind
    an unknown end on a newer review. Uncertain legacy residue is retained.
    """
    normalized = validate_reviews(document)
    if normalized["schema_version"] == 3:
        result = normalized
    else:
        result = {"schema_version": 3, "reviewer": "ChatGPT", "reviews": [
            {**entry, "score_version": normalized["schema_version"],
             "reviewed_at": normalized["reviewed_at"],
             "source_snapshot_id": normalized["source_snapshot_id"], "ends_at": None}
            for entry in normalized["reviews"]
        ]}
    if feed is not None:
        at = _utc(at if at is not None else datetime.now(timezone.utc))
        _attach_end_hints(result["reviews"], _feed_end_hints(feed, at), at)
    return validate_reviews(result)


def merge_reviews(
    existing: Any, incoming: Any, at: datetime, *, feed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge genuine assessments by ID, retaining unrelated original timestamps.

    Older imports cannot displace newer assessments. An ambiguous equal-time
    assessment conflict is an error, not a reason to pick whichever file was
    read last. End hints are separate: retain the latest known end, never
    shorten it without observation provenance. Apply feed hints before pruning;
    an absent ID alone proves nothing. Stale already-ended hints cannot impose
    a pruning deadline on a newer review whose end is unknown.
    """
    at = _utc(at)
    old = upgrade_reviews(existing) if existing is not None else {"schema_version": 3, "reviewer": "ChatGPT", "reviews": []}
    new = upgrade_reviews(incoming)
    hints = _feed_end_hints(feed, at) if feed is not None else {}
    entries = {}
    for document in (old, new):
        for entry in document["reviews"]:
            if _time(entry["reviewed_at"]) > at:
                raise ReviewsError("Bewertungszeitpunkt liegt in der Zukunft; Zusammenführung abgebrochen.")
            previous = entries.get(entry["id"])
            if previous is None:
                entries[entry["id"]] = entry
                continue
            if _time(entry["reviewed_at"]) == _time(previous["reviewed_at"]):
                # A changed lifecycle hint is not a newly authored assessment.
                compared = ENTRY_FIELDS - {"ends_at"}
                if any(entry[key] != previous[key] for key in compared):
                    raise ReviewsError("Gleichzeitige Bewertungen derselben Angebots-ID widersprechen sich.")
            if _time(entry["reviewed_at"]) > _time(previous["reviewed_at"]):
                winner, other = entry, previous
            else:
                winner, other = previous, entry
            # Even an older assessment can contribute a later known end, but
            # its past end must not newly bind a newer, undated lifecycle.
            if not (winner["ends_at"] is None and other["ends_at"] is not None
                    and _time(other["ends_at"]) <= at
                    and _time(other["reviewed_at"]) < _time(winner["reviewed_at"])):
                winner["ends_at"] = _latest_end(winner["ends_at"], other["ends_at"])
            entries[entry["id"]] = winner
    _attach_end_hints(list(entries.values()), hints, at)
    kept = [entries[item_id] for item_id in sorted(entries)
            if entries[item_id]["ends_at"] is None or _time(entries[item_id]["ends_at"]) > at]
    return validate_reviews({"schema_version": 3, "reviewer": "ChatGPT", "reviews": kept})


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
        answer = _assessment(group["assessment"], header["schema_version"])
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
    """Bounded local JSON read; reject symbolic links in the entire path."""
    target = Path(path).absolute()
    if any(part.is_symlink() for part in (target, *target.parents)) or not target.is_file():
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
    """Apply per-item content-matched reviews without renewing source facts.

    Semantic assessments have no wall-clock TTL: the same product text does
    not become less interesting because another batch was rated. Auction
    lifetime and current observation validity remain independent checks.
    """
    at = _utc(at)
    output = [dict(item) for item in items]
    metadata: dict[str, Any] = {
        "status": "absent", "applied": 0, "total": 0, "skipped_changed": 0,
        "skipped_stale": 0, "skipped_newer": 0, "skipped_missing": 0,
        "skipped_future": 0, "reviewer": "ChatGPT", "reviewed_at": None,
        "oldest_reviewed_at": None, "latest_reviewed_at": None,
        "source_snapshot_id": None, "expires_at": None,
        "retention_basis": "content_and_auction", "errors": [],
    }
    if review_doc is None:
        return output, metadata
    try:
        document = upgrade_reviews(review_doc)
    except ReviewsError as error:
        metadata.update({"status": "invalid", "errors": [str(error)]})
        return output, metadata
    metadata["total"] = len(document["reviews"])
    past = [entry for entry in document["reviews"] if _time(entry["reviewed_at"]) <= at]
    if past:
        times = [_time(entry["reviewed_at"]) for entry in past]
        snapshots = {entry["source_snapshot_id"] for entry in past}
        metadata.update({"reviewed_at": _iso(max(times)), "latest_reviewed_at": _iso(max(times)),
                         "oldest_reviewed_at": _iso(min(times)),
                         "source_snapshot_id": next(iter(snapshots)) if len(snapshots) == 1 else None})
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
        reviewed_at = _time(review["reviewed_at"])
        if reviewed_at > at:
            metadata["skipped_future"] += 1
            continue
        item_id = review["id"]
        if item_id not in by_id:
            metadata["skipped_missing"] += 1
            continue
        item = output[by_id[item_id]]
        seen = _time(item.get("last_seen_at"))
        end = _time(item.get("ends_at"))
        # Semantic content age and auction lifetime are independent from the
        # observation age of price/bids. A matching review never renews facts.
        if seen is None or seen > at or end is None or end <= at:
            metadata["skipped_stale"] += 1
            continue
        try:
            matching = content_fingerprint(item) == review["content_fingerprint"]
        except ReviewsError:
            matching = False
        if not matching or item_id in duplicate_ids or item.get("description_truncated") is True:
            metadata["skipped_changed"] += 1
            continue
        previous = _time(item.get("semantic_assessed_at"))
        if item.get("assessment_basis") == "llm" and previous is not None and reviewed_at < previous <= at:
            metadata["skipped_newer"] += 1
            continue
        enriched = apply_semantic(item, review["assessment"], score_version=review["score_version"])
        result = dict(item)
        result.update({key: enriched[key] for key in ANNOTATION_FIELDS if key in enriched})
        result.update({"semantic_review_mode": "interactive", "semantic_assessed_at": review["reviewed_at"]})
        output[by_id[item_id]] = result
        metadata["applied"] += 1
    metadata["status"] = "ok" if metadata["applied"] == metadata["total"] else "partial"
    if metadata["skipped_future"]:
        metadata["errors"].append("Zukünftig datierte Einzelbewertungen wurden nicht angewendet.")
        if metadata["skipped_future"] == metadata["total"]:
            metadata["status"] = "future"
    return output, metadata
