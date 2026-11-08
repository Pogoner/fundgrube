"""Persist generated, credential-free snapshots on the dedicated ``data`` branch.

Standard library only. The GitHub token is used only with api.github.com; HTTP
redirects are refused. A restore marker records the exact head from which the
scan started. Publishing never force-pushes and refuses concurrent changes.
This script deliberately has no repository-settings or Pages-enablement API.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DATA_BRANCH = "data"
MARKER = ".github-snapshot-head.json"
# Bounded storage for up to 5,000 observed offers, not an increased crawl quota.
# Every offer has one detail document plus a share of the paginated/full feed.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 6000
# A maximal restored state arrives as base64 (4/3 of its decoded byte size).
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PATH_PATTERN = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_./-]*")
SECRET_KEYS = {
    "accesstoken", "refreshtoken", "clientsecret", "ebayclientsecret",
    "ebayclientid", "openaikey", "openaiapikey", "apikey", "githubtoken",
    "authorization", "password", "privatekey",
}


class SnapshotError(RuntimeError):
    """Safe-to-log deployment error, without server response bodies or tokens."""


class GitHubError(SnapshotError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub API returned HTTP {status}; check permissions, rules and rate limits.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to an alternate host (including renames).
        return None


def sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise SnapshotError("GitHub returned an invalid commit or object identifier.")
    return value


class GitHub:
    def __init__(self, repository: str, token: str):
        if not REPO_PATTERN.fullmatch(repository) or any(part in {".", ".."} for part in repository.split("/")):
            raise SnapshotError("GITHUB_REPOSITORY must have the form owner/repository.")
        if not token:
            raise SnapshotError("GITHUB_TOKEN is required for restore and publish.")
        self.repository = repository
        self.token = token
        self.base = f"https://api.github.com/repos/{repository}"
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise SnapshotError("Refusing an unexpected GitHub API path.")
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "Fundgrube-Snapshot/1.0",
            },
        )
        try:
            with self.opener.open(request, timeout=40) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise SnapshotError("GitHub response exceeded the safe size limit.")
            result = json.loads(content)
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Underlying errors can contain a URL or an echoed credential.
            raise SnapshotError("GitHub connection failed; no unsafe automatic write retry was attempted.") from None
        except (ValueError, UnicodeDecodeError):
            raise SnapshotError("GitHub returned malformed JSON.") from None
        if not isinstance(result, dict):
            raise SnapshotError("GitHub returned an unexpected response shape.")
        return result

    def head(self, branch: str) -> str | None:
        if branch not in {"main", DATA_BRANCH}:
            raise SnapshotError("Only main and data may be read as branch references.")
        try:
            result = self.request("GET", f"/git/ref/heads/{branch}")
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        if result.get("ref") != f"refs/heads/{branch}" or result.get("object", {}).get("type") != "commit":
            raise SnapshotError("GitHub returned an unexpected branch reference.")
        return sha(result["object"].get("sha"))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise SnapshotError("Refusing to write state through a symbolic link.")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def json_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (ValueError, RecursionError):
        raise SnapshotError("A generated JSON document is invalid.") from None
    if not isinstance(value, dict):
        raise SnapshotError("State and manifest must be JSON objects.")
    return value


def reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in SECRET_KEYS:
                raise SnapshotError("Refusing to publish a credential-like JSON field.")
            reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            reject_credentials(child)


def restore(api: GitHub, state_dir: Path) -> dict[str, Any]:
    head = api.head(DATA_BRANCH)
    base = head or api.head("main")
    if not base:
        raise SnapshotError("Initialize the main branch before creating a data snapshot.")
    if head:
        # Read from the immutable commit, not a branch that could move mid-run.
        result = api.request("GET", f"/contents/state.json?ref={head}")
        if result.get("type") != "file":
            raise SnapshotError("Snapshot state must be a regular file.")
        if result.get("encoding") == "none":
            # Contents API omits inline content above 1 MB; Git blobs still work.
            result = api.request("GET", f"/git/blobs/{sha(result.get('sha'))}")
        if result.get("encoding") != "base64" or not isinstance(result.get("content"), str):
            raise SnapshotError("Snapshot state is missing, too large, or not a regular file.")
        try:
            raw = base64.b64decode("".join(result["content"].split()), validate=True)
            if len(raw) > MAX_FILE_BYTES:
                raise SnapshotError("Snapshot state exceeded the safe size limit.")
            content = raw.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise SnapshotError("Snapshot state could not be decoded.") from None
        reject_credentials(json_object(content))
        atomic_write(state_dir / "state.json", content)
    elif (state_dir / "state.json").exists():
        raise SnapshotError("The remote data branch is absent but local state exists; use a fresh state directory.")
    marker = {"repository": api.repository, "branch": DATA_BRANCH, "head": head, "base": base}
    atomic_write(state_dir / MARKER, json.dumps(marker) + "\n")
    return {"restored": bool(head), "branch": DATA_BRANCH, "head": head}


def read_generated(path: Path, root: Path) -> str:
    if path.is_symlink() or root.is_symlink() or not path.is_file():
        raise SnapshotError("Generated snapshot files must be regular files, not symbolic links.")
    relative = path.relative_to(root)
    for parent in relative.parents:
        if (root / parent).is_symlink():
            raise SnapshotError("Generated snapshot directories must not be symbolic links.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SnapshotError("A generated file exceeded the safe size limit.")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise SnapshotError("A generated file could not be read as UTF-8.") from None


def collect_files(site_dir: Path, state_dir: Path, token: str = "") -> list[dict[str, str]]:
    required = [(site_dir / "api" / "manifest.json", site_dir, "api/manifest.json"),
                (site_dir / "data" / "deals.json", site_dir, "data/deals.json"),
                (state_dir / "state.json", state_dir, "state.json")]
    for path, _, _ in required:
        if not path.is_file():
            raise SnapshotError("Required manifest, feed or state is missing; the pipeline must finish before publication.")
    files = required[1:]
    for path in sorted((site_dir / "api").rglob("*")):
        if path.is_symlink():
            raise SnapshotError("Refusing a symbolic link in the generated API directory.")
        if not path.is_file():
            continue
        relative = path.relative_to(site_dir).as_posix()
        if not PATH_PATTERN.fullmatch(relative) or any(part in {".", ".."} or part.startswith(".") for part in relative.split("/")):
            raise SnapshotError("Refusing an unexpected generated path.")
        if path.suffix not in {".json", ".jsonl", ".md", ".txt"}:
            raise SnapshotError("Only JSON, JSONL, Markdown and text API documents may be published.")
        files.append((path, site_dir, relative))
    if len(files) > MAX_FILES:
        raise SnapshotError("Generated snapshot contains too many files.")
    total = 0
    entries = []
    for path, root, relative in sorted(files, key=lambda row: row[2]):
        content = read_generated(path, root)
        total += len(content.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise SnapshotError("Generated snapshot exceeded the total safe size limit.")
        if token and token in content:
            raise SnapshotError("Refusing to publish a GitHub credential.")
        try:
            if path.suffix == ".json":
                value = json.loads(content)
                reject_credentials(value)
                if relative in {"state.json", "api/manifest.json"} and not isinstance(value, dict):
                    raise SnapshotError("State and manifest must be JSON objects.")
            elif path.suffix == ".jsonl":
                for line in content.splitlines():
                    if line.strip():
                        reject_credentials(json.loads(line))
        except (ValueError, RecursionError):
            raise SnapshotError("Generated snapshot contains invalid JSON.") from None
        entries.append({"path": relative, "mode": "100644", "type": "blob", "content": content})
    return entries


def publish(api: GitHub, state_dir: Path, site_dir: Path) -> dict[str, Any]:
    marker_path = state_dir / MARKER
    if not marker_path.is_file():
        raise SnapshotError("Run restore before publish so concurrent updates can be detected.")
    marker = json_object(read_generated(marker_path, state_dir))
    if marker.get("repository") != api.repository or marker.get("branch") != DATA_BRANCH:
        raise SnapshotError("Restore marker belongs to a different repository or branch.")
    expected = marker.get("head")
    if expected is not None:
        sha(expected)
    base = sha(marker.get("base"))
    if expected and base != expected:
        raise SnapshotError("Restore marker has an inconsistent parent commit.")
    entries = collect_files(site_dir, state_dir, api.token)
    if api.head(DATA_BRANCH) != expected:
        raise SnapshotError("The data branch changed after restore. Nothing was overwritten; start a fresh run.")
    # Deliberately no base_tree: old generated pages disappear from the current
    # snapshot, and source files/secrets can never be copied from main.
    tree = api.request("POST", "/git/trees", {"tree": entries})
    tree_sha = sha(tree.get("sha"))
    commit = api.request("POST", "/git/commits", {
        "message": "Update generated Fundgrube snapshot",
        "tree": tree_sha,
        "parents": [base],
    })
    commit_sha = sha(commit.get("sha"))
    if api.head(DATA_BRANCH) != expected:
        raise SnapshotError("The data branch changed during publication. Nothing was overwritten; start a fresh run.")
    if expected:
        result = api.request("PATCH", "/git/refs/heads/data", {"sha": commit_sha, "force": False})
    else:
        # A concurrent creation returns 422. It is never converted to a force update.
        result = api.request("POST", "/git/refs", {"ref": "refs/heads/data", "sha": commit_sha})
    if result.get("object", {}).get("sha") != commit_sha:
        raise SnapshotError("GitHub did not confirm the new data branch head; inspect it before retrying.")
    return {"published": True, "branch": DATA_BRANCH, "head": commit_sha, "files": len(entries)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["restore", "publish", "validate"])
    parser.add_argument("--state-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--site-dir", type=Path, default=Path("site"))
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = {"valid": True, "files": len(collect_files(args.site_dir, args.state_dir))}
        else:
            api = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""))
            result = restore(api, args.state_dir) if args.command == "restore" else publish(api, args.state_dir, args.site_dir)
        print(json.dumps(result))
        return 0
    except (SnapshotError, OSError) as error:
        # OSError messages can include path contents. Keep them generic.
        message = str(error) if isinstance(error, SnapshotError) else "Local snapshot file operation failed."
        print(f"Snapshot error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
