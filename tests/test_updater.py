import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from netlab.updater import (CHUNK_SIZE, DOWNLOAD_LIMIT, MANIFEST_LIMIT,
                           NETWORK_TIMEOUT, UpdateCancelled, UpdateClient,
                           UpdateError, UpdateInfo, _GitHubRedirectHandler)


class Response(io.BytesIO):
    def __init__(self, data, *, url="https://release-assets.githubusercontent.com/asset",
                 headers=None, status=200):
        super().__init__(data)
        self.url = url
        self.headers = headers or {}
        self.status = status
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.client = UpdateClient("example/MotuNet", "0.4.1")
        self.payload = b"MZ verified installer test data"
        self.info = UpdateInfo("0.4.2",
            "https://github.com/example/MotuNet/releases/download/v0.4.2/MotuNet-0.4.2-Setup-x64.exe",
            hashlib.sha256(self.payload).hexdigest(), len(self.payload), "修复循环断网")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.cancel = threading.Event()
        self.progress = []

    def manifest(self, **changes):
        data = {"schema_version": 1, "platform": "windows-x64", **vars(self.info)}
        data.update(changes)
        return data

    def install_response(self, response):
        self.client._opener = Opener(response)
        return self.client._opener

    def check_manifest(self, data):
        self.install_response(Response(json.dumps(data).encode("utf-8")))
        return self.client.check()

    def download(self, info=None, progress=None):
        return self.client.download(info or self.info, self.directory,
            progress or (lambda received, total: self.progress.append((received, total))), self.cancel)

    def assert_no_download_files(self):
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_newer_manifest_and_bounded_https_request(self):
        self.assertEqual(self.check_manifest(self.manifest()), self.info)
        request, timeout = self.client._opener.requests[0]
        self.assertEqual(request.full_url,
            "https://github.com/example/MotuNet/releases/latest/download/latest.json")
        self.assertEqual(timeout, NETWORK_TIMEOUT)
        self.assertEqual(request.get_header("Accept-encoding"), "identity")

    def test_numeric_versions_equal_older_and_newer(self):
        for current, latest, update in (("0.4.2", "0.4.2", False),
                                      ("0.4.2", "0.4.1", False),
                                      ("0.4.9", "0.4.10", True),
                                      ("0.4.10", "0.4.9", False),
                                      ("0.9.99", "1.0.0", True)):
            with self.subTest(current=current, latest=latest):
                self.client = UpdateClient("example/MotuNet", current)
                result = self.check_manifest(self.manifest(version=latest,
                    url=self.client._installer_url(latest)))
                self.assertEqual(result is not None, update)

    def test_invalid_repository_and_current_version(self):
        for repository in ("", "owner", "../repo", "owner/..", "owner/.",
                           "owner/repo/path", "owner/repo?x", "owner/repo#x",
                           "https://github.com/a/b", "a\\b", "a/b\n"):
            with self.subTest(repository=repository), self.assertRaises(UpdateError):
                UpdateClient(repository, "0.4.1")
        for version in (None, 1, "v1.0.0", "1.0", "1.0.0-beta", "01.0.0", "1.0.0\n"):
            with self.subTest(version=version), self.assertRaises(UpdateError):
                UpdateClient("example/repo", version)

    def test_invalid_manifest_fields(self):
        changes = ({"schema_version": 2}, {"schema_version": True}, {"schema_version": 1.0},
                   {"platform": "linux-x64"}, {"version": "0.5.0-rc1"},
                   {"version": "../1"}, {"version": None}, {"size": True},
                   {"size": 0}, {"size": -1}, {"size": 1.2},
                   {"size": DOWNLOAD_LIMIT + 1}, {"sha256": "x" * 64},
                   {"sha256": "a" * 63}, {"sha256": None}, {"notes": []},
                   {"notes": "\ud800"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(UpdateError):
                self.check_manifest(self.manifest(**change))
        for data in (None, [], "manifest"):
            with self.subTest(data=data), self.assertRaises(UpdateError):
                self.check_manifest(data)

    def test_manifest_must_match_exact_release_asset(self):
        urls = (self.info.url.replace("https:", "http:"),
                self.info.url.replace("github.com/", "github.com.evil.test/"),
                self.info.url.replace("example/MotuNet", "other/MotuNet"),
                self.info.url.replace("v0.4.2", "v0.4.3"),
                self.info.url.replace("Setup-x64.exe", "Setup-x86.exe"),
                self.info.url + "?download=1", "file:///C:/installer.exe",
                self.info.url.replace("example/", "example/%2e%2e/"),
                self.info.url.replace("github.com", "evil@github.com"))
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UpdateError):
                self.check_manifest(self.manifest(url=url))

    def test_malformed_json_utf8_and_oversized_manifest(self):
        for data in (b"{broken", b"\xff", b"x" * (MANIFEST_LIMIT + 1)):
            with self.subTest(length=len(data)), self.assertRaises(UpdateError):
                self.install_response(Response(data))
                self.client.check()
        self.install_response(Response(b"{}", headers={"Content-Length": str(MANIFEST_LIMIT + 1)}))
        with self.assertRaisesRegex(UpdateError, "128 KiB"):
            self.client.check()

    def test_manifest_truncated_or_invalid_content_length(self):
        for value in ("100", "-1", "invalid", "999999999999999"):
            self.install_response(Response(b"{}", headers={"Content-Length": value}))
            with self.subTest(value=value), self.assertRaises(UpdateError):
                self.client.check()

    def test_http_network_and_incomplete_response_errors_are_readable(self):
        failures = (urllib.error.HTTPError(self.client.manifest_url, 404, "missing", {}, None),
                    urllib.error.URLError("unreachable"), TimeoutError("timed out"),
                    http.client.IncompleteRead(b"short", 10))
        for failure in failures:
            with self.subTest(failure=failure), self.assertRaises(UpdateError):
                self.install_response(failure)
                self.client.check()

    def test_redirects_accept_only_expected_github_https_hosts(self):
        handler = _GitHubRedirectHandler()
        request = urllib.request.Request(self.info.url)
        for host in ("github.com", "release-assets.githubusercontent.com",
                     "objects.githubusercontent.com", "github-releases.githubusercontent.com"):
            redirected = handler.redirect_request(request, None, 302, "redirect", {},
                                                   f"https://{host}/asset?signature=ok")
            self.assertEqual(redirected.host, host)
        for url in ("http://github.com/asset", "https://evil.test/asset", "file:///tmp/asset",
                    "https://github.com.evil.test/asset", "https://user@github.com/asset",
                    "https://github.com:444/asset", "https://raw.githubusercontent.com/asset",
                    "https://github.com/asset#fragment", "https://github.com/asset\n"):
            with self.subTest(url=url), self.assertRaises(UpdateError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)

    def test_final_url_status_and_encoding_checked(self):
        for kwargs in ({"url": "http://github.com/asset"}, {"status": 206},
                       {"headers": {"Content-Encoding": "gzip"}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(UpdateError):
                response = Response(b"{}", **kwargs)
                self.install_response(response)
                self.client.check()
            self.assertTrue(response.closed)

    def test_success_renames_only_verified_file_and_reports_progress(self):
        response = Response(self.payload, headers={"Content-Length": str(self.info.size)})
        self.install_response(response)
        with patch("netlab.updater.os.replace", wraps=__import__("os").replace) as rename:
            destination = self.download()
        self.assertEqual(destination.read_bytes(), self.payload)
        self.assertEqual(destination.name, "MotuNet-0.4.2-Setup-x64.exe")
        self.assertEqual(destination.parent.parent, self.directory)
        self.assertEqual(list(destination.parent.iterdir()), [destination])
        self.assertEqual(self.progress, [(0, self.info.size), (self.info.size, self.info.size)])
        self.assertTrue(all(0 < size <= CHUNK_SIZE for size in response.read_sizes))
        self.assertTrue(response.closed)
        rename.assert_called_once()
        self.assertTrue(str(rename.call_args.args[0]).endswith(".exe.part"))

    def test_unique_attempt_directory_does_not_overwrite_existing_download(self):
        self.install_response(Response(self.payload))
        first = self.download()
        self.install_response(Response(self.payload))
        second = self.download()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_download_revalidates_info_before_opening(self):
        self.install_response(Response(self.payload))
        for info in (replace(self.info, url="https://evil.test/setup.exe"),
                     replace(self.info, version="../../escape"), replace(self.info, size=0),
                     replace(self.info, sha256="bad")):
            with self.subTest(info=info), self.assertRaises(UpdateError):
                self.download(info)
        self.assertEqual(self.client._opener.requests, [])
        self.assert_no_download_files()

    def test_hash_mismatch_truncation_and_oversize_cleanup(self):
        for payload in (b"x" * self.info.size, self.payload[:-1], self.payload + b"extra"):
            with self.subTest(payload=payload), self.assertRaises(UpdateError):
                self.install_response(Response(payload))
                self.download()
            self.assert_no_download_files()

    def test_content_length_mismatch_cleanup(self):
        self.install_response(Response(self.payload, headers={"Content-Length": str(self.info.size + 1)}))
        with self.assertRaisesRegex(UpdateError, "长度"):
            self.download()
        self.assert_no_download_files()

    def test_download_http_and_read_failure_cleanup(self):
        class BrokenResponse(Response):
            def read(self, size=-1):
                raise http.client.IncompleteRead(b"short", 100)
        for failure in (urllib.error.HTTPError(self.info.url, 500, "error", {}, None),
                        urllib.error.URLError("offline"), BrokenResponse(self.payload)):
            self.install_response(failure)
            with self.subTest(failure=failure), self.assertRaises(UpdateError):
                self.download()
            self.assert_no_download_files()

    def test_cancel_before_request_creates_no_files(self):
        self.install_response(Response(self.payload))
        self.cancel.set()
        with self.assertRaises(UpdateCancelled):
            self.download()
        self.assertEqual(self.client._opener.requests, [])
        self.assert_no_download_files()

    def test_cancel_during_download_cleans_attempt_and_preserves_other_files(self):
        keep = self.directory / "existing.txt"
        keep.write_text("keep")
        self.install_response(Response(self.payload))
        def progress(received, total):
            if received:
                self.cancel.set()
        with self.assertRaises(UpdateCancelled):
            self.download(progress=progress)
        self.assertEqual(list(self.directory.iterdir()), [keep])

    def test_cancel_while_read_returns_no_success(self):
        cancel = self.cancel
        class CancellingResponse(Response):
            def read(self, size=-1):
                chunk = super().read(size)
                cancel.set()
                return chunk
        self.install_response(CancellingResponse(self.payload))
        with self.assertRaises(UpdateCancelled):
            self.download()
        self.assert_no_download_files()

    def test_cancel_after_atomic_rename_removes_verified_attempt(self):
        import os
        original = os.replace
        def cancel_after_rename(source, destination):
            original(source, destination)
            self.cancel.set()
        self.install_response(Response(self.payload))
        with patch("netlab.updater.os.replace", side_effect=cancel_after_rename):
            with self.assertRaises(UpdateCancelled):
                self.download()
        self.assert_no_download_files()

    def test_callback_failure_cleanup(self):
        self.install_response(Response(self.payload))
        def fail_progress(received, total):
            raise RuntimeError("progress failed")
        with self.assertRaisesRegex(UpdateError, "下载"):
            self.download(progress=fail_progress)
        self.assert_no_download_files()


if __name__ == "__main__":
    unittest.main()
