#!/usr/bin/env python3
"""Pack an explicitly submitted interactive review request inside GitHub Actions.

The request is data, never a command. Only fixed files in this repository are
read, and the feed is pinned to an immutable commit. Validation happens in a
temporary directory before any remote or local review store is changed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunter.reviews import MAX_REVIEW_BYTES, ReviewsError, read_json_file, validate_reviews
from scripts.github_snapshot import GitHub, SnapshotError, _WritePacer, atomic_write, sha
from scripts.pack_reviews import main as pack_reviews

PENDING_PATH = "reviews/pending.json"
STORE_PATH = "reviews/current.json"
RECEIPT_PATH = "reviews/last-request-result.json"
EMPTY_REQUEST = {"schema_version": 1, "request": None}
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
MAX_FEED_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


def _encoded(document: Any) -> str:
    try:
        # Compact encoding keeps even malformed, deeply nested inputs bounded
        # before the stricter existing review validator examines their schema.
        content = json.dumps(document, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False) + "\n"
        if len(content.encode("utf-8")) > MAX_REVIEW_BYTES:
            raise ReviewsError("Review-Auftragsdokument überschreitet die erlaubte Größe.")
        return content
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ReviewsError("Review-Auftragsdokument enthält ungültige JSON-Daten.") from None


def _safe_target(path: Path) -> None:
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ReviewsError("Review-Auftragsdateien dürfen keine symbolischen Links enthalten.")
    if absolute.exists() and not absolute.is_file():
        raise ReviewsError("Review-Auftragsdateien müssen reguläre Dateien sein.")


def read_request(root: Path) -> dict[str, Any] | None:
    document = read_json_file(root / PENDING_PATH)
    if (not isinstance(document, dict) or set(document) != {"schema_version", "request"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1):
        raise ReviewsError("Unbekanntes Review-Auftragsformat.")
    request = document["request"]
    if request is None:
        return None
    if not isinstance(request, dict) or set(request) != {"id", "source_commit", "ratings"}:
        raise ReviewsError("Review-Auftrag enthält fehlende oder unerwartete Felder.")
    if not isinstance(request["id"], str) or not REQUEST_ID.fullmatch(request["id"]):
        raise ReviewsError("Review-Auftragskennung ist ungültig.")
    sha(request["source_commit"])
    if (not isinstance(request["ratings"], dict)
            or type(request["ratings"].get("schema_version")) is not int
            or request["ratings"]["schema_version"] != 2):
        raise ReviewsError("Neue Review-Aufträge benötigen vollständige V2-Bewertungen.")
    if not isinstance(request["ratings"].get("groups"), list) or not request["ratings"]["groups"]:
        raise ReviewsError("Review-Aufträge müssen tatsächlich erarbeitete Bewertungen enthalten.")
    return request


def _download(api: GitHub, path: str, commit: str, limit: int) -> bytes:
    if path not in {"api/manifest.json", "data/deals.json", STORE_PATH}:
        raise SnapshotError("Only the fixed manifest, feed and review store may be read.")
    result = api.request("GET", f"/contents/{path}?ref={sha(commit)}")
    if result.get("type") != "file":
        raise SnapshotError("The pinned source file is not a regular file.")
    if result.get("encoding") == "none":
        result = api.request("GET", f"/git/blobs/{sha(result.get('sha'))}")
    if result.get("encoding") != "base64" or not isinstance(result.get("content"), str):
        raise SnapshotError("The pinned source file cannot be decoded.")
    try:
        content = base64.b64decode("".join(result["content"].split()), validate=True)
    except (ValueError, binascii.Error):
        raise SnapshotError("The pinned source file has invalid encoding.") from None
    if len(content) > limit:
        raise SnapshotError("The pinned source file exceeded the safe size limit.")
    return content


def sync_current_store(api: GitHub, root: Path) -> dict[str, Any]:
    """Read the current main store independently of an older queued checkout.

    A successful pack uses GITHUB_TOKEN and does not trigger another run. A
    queued event can therefore still have the previous checkout and store.
    Pin main once, validate the full remote store, and preserve its exact text.
    Any fetch/validation failure must stop publication of a stale local store.
    """
    target = root / STORE_PATH
    _safe_target(target)
    head = sha(api.head("main"))
    content = _download(api, STORE_PATH, head, MAX_REVIEW_BYTES)
    with tempfile.TemporaryDirectory(prefix="fundgrube-review-sync-") as temporary:
        staged = Path(temporary) / "current.json"
        staged.write_bytes(content)
        document = validate_reviews(read_json_file(staged, max_bytes=MAX_REVIEW_BYTES))
    _safe_target(target)
    atomic_write(target, content.decode("utf-8"))
    return {"status": "synced", "store_commit": head, "stored_reviews": len(document["reviews"])}


def process_request(api: GitHub, root: Path, expected_head: str, *, sleep=None, monotonic=None) -> dict[str, Any]:
    request = read_request(root)
    if request is None:
        return {"status": "idle"}
    expected_head = sha(expected_head)
    if api.head("main") != expected_head:
        raise SnapshotError("Main changed after checkout. The pending request was not overwritten; start a fresh run.")
    for path in (PENDING_PATH, STORE_PATH, RECEIPT_PATH):
        _safe_target(root / path)
    # The durable store must already be present. A failed/incomplete checkout
    # must not silently turn a merge into a replacement of all prior reviews.
    read_json_file(root / STORE_PATH)
    request_digest = "sha256:" + hashlib.sha256(_encoded(request).encode("utf-8")).hexdigest()
    if (root / RECEIPT_PATH).exists():
        previous = read_json_file(root / RECEIPT_PATH)
        if isinstance(previous, dict) and previous.get("request_id") == request["id"]:
            raise ReviewsError("Diese Review-Auftragskennung wurde bereits verarbeitet; verwende eine neue Kennung.")

    with tempfile.TemporaryDirectory(prefix="fundgrube-review-request-") as temporary:
        directory = Path(temporary)
        manifest_path, feed_path = directory / "manifest.json", directory / "feed.json"
        ratings_path, output_path = directory / "ratings.json", directory / "current.json"
        manifest_path.write_bytes(_download(api, "api/manifest.json", request["source_commit"], MAX_MANIFEST_BYTES))
        feed_path.write_bytes(_download(api, "data/deals.json", request["source_commit"], MAX_FEED_BYTES))
        manifest = read_json_file(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
        feed = read_json_file(feed_path, max_bytes=MAX_FEED_BYTES)
        snapshot_id = request["ratings"].get("source_snapshot_id")
        if (not isinstance(manifest, dict) or not isinstance(feed, dict)
                or manifest.get("snapshot_id") != snapshot_id or feed.get("snapshot_id") != snapshot_id):
            raise ReviewsError("Manifest, vollständiger Feed und Bewertungen gehören nicht zum selben Snapshot.")
        count = manifest.get("counts", {}).get("total") if isinstance(manifest.get("counts"), dict) else None
        if type(count) is not int or not isinstance(feed.get("deals"), list) or count != len(feed["deals"]):
            raise ReviewsError("Der vollständige Feed stimmt nicht mit der Bestandszahl des Manifests überein.")
        ratings_path.write_text(_encoded(request["ratings"]), encoding="utf-8")
        if pack_reviews(["--feed", str(feed_path), "--ratings", str(ratings_path),
                         "--existing", str(root / STORE_PATH), "--output", str(output_path)]) != 0:
            raise ReviewsError("Der Review-Auftrag hat die unveränderte Pack-Validierung nicht bestanden.")
        document = read_json_file(output_path)
        store_content = output_path.read_text(encoding="utf-8")

    receipt = {
        "schema_version": 1, "status": "packed", "request_id": request["id"],
        "request_digest": request_digest, "source_commit": request["source_commit"],
        "source_snapshot_id": snapshot_id, "request_commit": expected_head,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "submitted_reviews": sum(len(group["ids"]) for group in request["ratings"]["groups"]),
        "stored_reviews": len(document["reviews"]),
    }
    contents = {STORE_PATH: store_content, PENDING_PATH: _encoded(EMPTY_REQUEST), RECEIPT_PATH: _encoded(receipt)}
    if any(api.token and api.token in content for content in contents.values()):
        raise SnapshotError("Refusing to publish a GitHub credential.")
    if api.head("main") != expected_head:
        raise SnapshotError("Main changed while validating reviews. The pending request was not overwritten.")
    base = api.request("GET", f"/git/commits/{expected_head}")
    base_tree = sha(base.get("tree", {}).get("sha"))
    writer = _WritePacer(api, sleep=sleep, monotonic=monotonic)
    # A durable store can exceed the inline-tree request size. Upload it as
    # one bounded blob; only the small receipt and cleared request are inline.
    blob = writer.request("POST", "/git/blobs", {
        "content": base64.b64encode(store_content.encode("utf-8")).decode("ascii"), "encoding": "base64",
    })
    entries = [{"path": STORE_PATH, "type": "blob", "mode": "100644", "sha": sha(blob.get("sha"))}]
    entries.extend({"path": path, "type": "blob", "mode": "100644", "content": content}
                   for path, content in contents.items() if path != STORE_PATH)
    tree = writer.request("POST", "/git/trees", {"base_tree": base_tree, "tree": entries})
    commit = writer.request("POST", "/git/commits", {
        "message": f"Pack authorized Fundgrube review request {request['id']}",
        "tree": sha(tree.get("sha")), "parents": [expected_head],
    })
    commit_sha = sha(commit.get("sha"))
    if api.head("main") != expected_head:
        raise SnapshotError("Main changed during review publication. Nothing was overwritten; start a fresh run.")
    result = writer.request("PATCH", "/git/refs/heads/main", {"sha": commit_sha, "force": False})
    if result.get("object", {}).get("sha") != commit_sha:
        raise SnapshotError("GitHub did not confirm the review commit; inspect main before retrying.")
    # Only a confirmed atomic remote commit permits replacing local files.
    # The same job now applies these packed reviews before publishing data.
    for path, content in contents.items():
        atomic_write(root / path, content)
    return {"status": "packed", "request_id": request["id"], "commit": commit_sha,
            "submitted_reviews": receipt["submitted_reviews"], "stored_reviews": receipt["stored_reviews"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync-current-store", action="store_true",
                        help="Read and validate the current main review store without processing a request.")
    args = parser.parse_args(argv)
    try:
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise SnapshotError("Review requests may only be processed on main.")
        root = Path(__file__).resolve().parents[1]
        if args.sync_current_store:
            api = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""))
            result = sync_current_store(api, root)
        elif read_request(root) is None:
            result = {"status": "idle"}
        else:
            api = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""))
            result = process_request(api, root, os.environ.get("GITHUB_SHA", ""))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (SnapshotError, ReviewsError, OSError) as error:
        message = str(error) if isinstance(error, (SnapshotError, ReviewsError)) else "Review-Auftragsdatei konnte nicht verarbeitet werden."
        print(f"Review-Verarbeitung fehlgeschlagen: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
