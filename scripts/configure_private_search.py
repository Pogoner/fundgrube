"""Configure the private search secret through an already authenticated gh CLI.

The postcode is read without terminal echo. Neither value enters argv, a file,
stdout or the public repository. This helper is an explicit administrator step,
not part of CI or of a scheduled crawl.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import shutil
import subprocess
import sys
import warnings


SECRET_NAME = "HUNTER_PRIVATE_SEARCH"
DEFAULT_REPOSITORY = "Pogoner/fundgrube"


class SetupError(ValueError):
    """A redacted setup failure that is safe to show in the terminal."""


def make_profile(postcode: str) -> dict[str, str]:
    if not isinstance(postcode, str) or re.fullmatch(r"[0-9]{5}", postcode) is None:
        raise SetupError("Die Such-PLZ muss aus genau fünf ASCII-Ziffern bestehen.")
    return {"postcode": postcode, "profile_key": secrets.token_hex(32)}


def configure(repository: str, profile: dict[str, str], *, replace: bool = False,
              run=subprocess.run) -> None:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise SetupError("Das Ziel muss owner/repository sein.")
    if (set(profile) != {"postcode", "profile_key"}
            or re.fullmatch(r"[0-9]{5}", profile.get("postcode", "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", profile.get("profile_key", "")) is None):
        raise SetupError("Ungültiges privates Suchprofil.")
    listed = run(["gh", "secret", "list", "--repo", repository, "--json", "name"],
                 text=True, capture_output=True, check=False)
    if listed.returncode != 0:
        raise SetupError("GitHub-Secrets konnten nicht geprüft werden. Anmeldung und Repository-Rechte prüfen.")
    try:
        entries = json.loads(listed.stdout)
        if not isinstance(entries, list) or any(not isinstance(x, dict) for x in entries):
            raise ValueError
    except (ValueError, TypeError):
        raise SetupError("GitHub lieferte keine gültige Secret-Liste.") from None
    if any(x.get("name") == SECRET_NAME for x in entries) and not replace:
        raise SetupError("Das Secret ist bereits vorhanden. Nur für einen bewussten Profilwechsel --replace verwenden.")
    saved = run(["gh", "secret", "set", SECRET_NAME, "--repo", repository],
                input=json.dumps(profile, separators=(",", ":")), text=True,
                capture_output=True, check=False)
    if saved.returncode != 0:
        # Do not forward provider output: it must not echo a secret on failure.
        raise SetupError("GitHub hat das Speichern des privaten Suchprofils nicht bestätigt.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--replace", action="store_true",
                        help="Vorhandenes Secret bewusst ersetzen; alte Abholzuordnungen werden neu geprüft.")
    args = parser.parse_args(argv)
    try:
        if shutil.which("gh") is None:
            raise SetupError("GitHub CLI (gh) fehlt. Zuerst installieren und mit gh auth login anmelden.")
        if not sys.stdin.isatty():
            raise SetupError("Ein interaktives Terminal wird für die verdeckte Eingabe benötigt.")
        print(f"Ziel: {args.repo} · Secret: {SECRET_NAME}")
        # getpass can otherwise fall back to echoed stdin when terminal echo
        # cannot be disabled, even though stdin.isatty() returned true.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                postcode = getpass.getpass("Private Such-PLZ (wird nicht angezeigt): ")
            except getpass.GetPassWarning:
                raise SetupError("Verdeckte Eingabe ist in diesem Terminal nicht möglich; Einrichtung abgebrochen.") from None
        profile = make_profile(postcode.strip())
        configure(args.repo, profile, replace=args.replace)
    except (SetupError, OSError):
        message = sys.exc_info()[1]
        print(str(message) if isinstance(message, SetupError) else "GitHub CLI konnte nicht ausgeführt werden.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Einrichtung abgebrochen.", file=sys.stderr)
        return 1
    print("Privates Suchprofil gespeichert. Es wurde kein Quellenlauf gestartet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
