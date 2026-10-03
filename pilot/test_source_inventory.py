import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from source_inventory import UPSTREAMS, main, parse_head, resolve_source


class SourceInventoryTests(unittest.TestCase):
    def test_official_upstreams_are_explicit(self):
        self.assertEqual(
            set(UPSTREAMS),
            {"immich", "digikam", "ai_file_sorter"},
        )
        self.assertEqual(
            UPSTREAMS["digikam"]["repo_url"],
            "https://invent.kde.org/graphics/digikam.git",
        )
        self.assertTrue(all(item["repo_url"].startswith("https://") for item in UPSTREAMS.values()))

    def test_parse_head_accepts_one_complete_sha(self):
        self.assertEqual(parse_head("a" * 40 + "\tHEAD\n"), "a" * 40)

    def test_parse_head_rejects_malformed_or_ambiguous_results(self):
        for output in ("not-a-sha\tHEAD\n", "a" * 39 + "\tHEAD\n", ""):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    parse_head(output)

    def test_github_api_recovers_from_git_timeout(self):
        def git_runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        def http_get(url, timeout):
            self.assertLessEqual(timeout, 10)
            if url == "https://api.github.com/repos/immich-app/immich":
                return json.dumps({"default_branch": "main"}).encode()
            if url == "https://api.github.com/repos/immich-app/immich/commits/main":
                return json.dumps({"sha": "b" * 40}).encode()
            raise AssertionError("unexpected URL: " + url)

        result = resolve_source("immich", git_runner=git_runner, http_get=http_get)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["commit"], "b" * 40)
        self.assertEqual(result["method"], "github_api")

    def test_official_github_page_recovers_when_api_is_unavailable(self):
        def git_runner(args, **kwargs):
            raise OSError("git transport unavailable")

        def http_get(url, timeout):
            if url.startswith("https://api.github.com/"):
                raise OSError("API unavailable")
            if url == "https://github.com/hyperfield/ai-file-sorter":
                return b'<html><script>{"defaultBranch":"main"}</script></html>'
            if url == "https://github.com/hyperfield/ai-file-sorter/commits/main":
                return (
                    b'<a href="/hyperfield/ai-file-sorter/commit/'
                    + (b"c" * 40)
                    + b'">Latest commit</a>'
                )
            raise AssertionError("unexpected URL: " + url)

        result = resolve_source("ai_file_sorter", git_runner=git_runner, http_get=http_get)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["commit"], "c" * 40)
        self.assertEqual(result["method"], "official_web")
        self.assertIn("github.com", result["evidence_url"])

    def test_unavailable_sources_are_reported_without_fabricating_a_sha(self):
        def git_runner(args, **kwargs):
            raise OSError("Git unreachable")

        def http_get(url, timeout):
            raise OSError("official endpoint unreachable")

        result = resolve_source("digikam", git_runner=git_runner, http_get=http_get)
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["commit"])
        self.assertIn("invent.kde.org", result["manual_check_url"])

    def test_nonzero_git_exit_is_preserved_as_evidence(self):
        def git_runner(args, **kwargs):
            return SimpleNamespace(returncode=128, stdout="", stderr="permission denied")

        def http_get(url, timeout):
            raise OSError("official page unavailable")

        result = resolve_source("digikam", git_runner=git_runner, http_get=http_get)
        self.assertEqual(result["status"], "unverified")
        self.assertIn("permission denied", result["error"])

    def test_main_returns_failure_when_any_source_is_unverified(self):
        report = {"sources": {"immich": {"status": "unverified"}}}
        output = StringIO()
        with patch("source_inventory.inventory", return_value=report):
            with redirect_stdout(output):
                exit_code = main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_invalid_github_api_sha_falls_back_to_official_page(self):
        def git_runner(args, **kwargs):
            return SimpleNamespace(returncode=128, stdout="", stderr="GitHub timeout")

        sha = "d" * 40

        def http_get(url, timeout):
            if url.endswith("/repos/immich-app/immich"):
                return b'{"default_branch":"main"}'
            if url.startswith("https://api.github.com/") and url.endswith("/commits/main"):
                return b'{"sha":"short"}'
            if url == "https://github.com/immich-app/immich":
                return b'{"defaultBranch":"main"}'
            if url == "https://github.com/immich-app/immich/commits/main":
                return (b'href="/immich-app/immich/commit/' + sha.encode() + b'"')
            raise AssertionError("unexpected URL: " + url)

        result = resolve_source("immich", git_runner=git_runner, http_get=http_get)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["commit"], sha)
        self.assertEqual(result["method"], "official_web")


if __name__ == "__main__":
    unittest.main()
