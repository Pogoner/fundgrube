import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
import warnings
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("private_search_setup", Path(__file__).resolve().parents[1] / "scripts" / "configure_private_search.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class PrivateSearchSetupTests(unittest.TestCase):
    def test_profile_is_explicit_and_has_a_new_random_key(self):
        one, two = setup.make_profile("12345"), setup.make_profile("12345")
        self.assertEqual(set(one), {"postcode", "profile_key"})
        self.assertRegex(one["profile_key"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(one["profile_key"], two["profile_key"])
        for value in ("", "1234", "１２３４５", "12345\n", None):
            with self.assertRaises(setup.SetupError):
                setup.make_profile(value)

    def test_secret_is_sent_only_on_stdin_and_never_printed(self):
        profile = setup.make_profile("12345")
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "[]", "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            setup.configure("owner/repository", profile, run=run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[1][1]["input"]), profile)
        for value in profile.values():
            self.assertNotIn(value, repr([call[0] for call in calls]))
            self.assertNotIn(value, out.getvalue())

    def test_existing_secret_is_not_rotated_by_accident(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps([{"name": setup.SECRET_NAME}]), "")
        with self.assertRaisesRegex(setup.SetupError, "bereits vorhanden"):
            setup.configure("owner/repository", setup.make_profile("12345"), run=run)
        self.assertEqual(len(calls), 1)

    def test_provider_error_does_not_echo_the_profile(self):
        profile = setup.make_profile("12345")
        def run(args, **kwargs):
            if "list" in args:
                return subprocess.CompletedProcess(args, 0, "[]", "")
            return subprocess.CompletedProcess(args, 1, "", json.dumps(profile))
        with self.assertRaises(setup.SetupError) as caught:
            setup.configure("owner/repository", profile, run=run)
        for value in profile.values():
            self.assertNotIn(value, str(caught.exception))

    def test_getpass_echo_fallback_aborts_before_read_or_secret_write(self):
        echoed_reads = []

        def fallback(prompt):
            warnings.warn("cannot disable terminal echo", setup.getpass.GetPassWarning)
            echoed_reads.append(True)
            return "12345"

        output = io.StringIO()
        with patch.object(setup.shutil, "which", return_value="/usr/bin/gh"), \
                patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.getpass, "getpass", side_effect=fallback), \
                patch.object(setup, "configure") as configure, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(setup.main([]), 1)
        self.assertEqual(echoed_reads, [])
        configure.assert_not_called()
        self.assertIn("Verdeckte Eingabe", output.getvalue())
        self.assertNotIn("12345", output.getvalue())


if __name__ == "__main__":
    unittest.main()
